"""Deterministic overview of the invoice data for GET /summary (no LLM involved).

Rules:
- Original-currency totals are always available (they need no rates).
- A EUR figure is shown only if every invoice it covers has a usable ECB conversion; otherwise
  it is null and the missing evidence is listed. A partial sum is never presented as a total.
- Money is summed as Decimal (EUR totals = sums of per-invoice amounts rounded to the cent).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal

from app.api.schemas import (Consolidated, CurrencyConversion, CurrencyTotal, DataQualityItem, DatasetInfo, FxEvidenceSummary,
                             FxLegSource, FxSeriesStatus, MissingRate, StatusBreakdown, SummaryResponse,
                             VendorSpend)
from app.data.store import ALLOWED_CURRENCIES, DataStore
from app.fx.convert import MAX_FALLBACK_DAYS, METHOD_NOTE, rate_formula
from app.fx.enrich import FxView
from app.fx.series import CONVENTION_NOTE, POLICY_NOTE, SERIES
from app.policy.clauses import PolicyDocument

STATUS_ORDER = ("ACTIVE", "ON_HOLD", "INACTIVE")
TOP_N = 5
REVIEW_RECOMMENDATION = (
    "Review the listed invoices: refresh or supply the missing ECB reference rates before relying on any "
    "consolidated EUR figure. The original-currency totals are complete and unaffected."
)


def money(d: Decimal) -> str:
    return f"{d.quantize(Decimal('0.01')):.2f}"


def _currency_totals(rows: list[dict], amounts: dict[str, Decimal]) -> list[CurrencyTotal]:
    tot: dict[str, Decimal] = defaultdict(Decimal)
    cnt: dict[str, int] = defaultdict(int)
    credits: dict[str, int] = defaultdict(int)
    for r in rows:
        n, c = r["invoice_number"], r["currency"]
        tot[c] += amounts[n]
        cnt[c] += 1
        credits[c] += amounts[n] < 0
    return [CurrencyTotal(currency=c, amount=money(tot[c]), invoice_count=cnt[c], credit_note_count=credits[c])
            for c in sorted(ALLOWED_CURRENCIES) if cnt[c]]


def _eur_total(rows: list[dict], view: FxView) -> Decimal | None:
    vals = [view.amounts[r["invoice_number"]] for r in rows]
    if any(v is None for v in vals):
        return None
    return sum(vals, Decimal(0))


def _evidence_summary(rows: list[dict], view: FxView, fx_status: dict[str, FxSeriesStatus]) -> FxEvidenceSummary:
    obs: list[date] = []
    fallbacks: list[str] = []
    max_fb = 0
    pairs = set()
    used_series = set()
    for r in rows:
        ev = view.evidence[r["invoice_number"]]
        if ev["status"] in ("EXACT", "FALLBACK"):
            obs.append(date.fromisoformat(ev["observation_date"]))
            pairs.add((ev["from_currency"], ev["to_currency"]))
            used_series.update(l["currency"] for l in ev["legs"])
        if ev["status"] == "FALLBACK":
            fallbacks.append(r["invoice_number"])
            max_fb = max(max_fb, ev["fallback_days"])
    sources = [
        FxLegSource(currency=s, series=SERIES[s].name, url=SERIES[s].url, page=SERIES[s].page,
                    snapshot_id=fx_status[s].snapshot_id, snapshot_fetched_at=fx_status[s].fetched_at,
                    release=fx_status[s].release)
        for s in SERIES if s in used_series
    ]
    return FxEvidenceSummary(
        sources=sources,
        coverage=dict(view.coverage),
        observation_date_range=(min(obs), max(obs)) if obs else None,
        fallback_invoices=sorted(fallbacks),
        max_fallback_days=max_fb,
        formula=[rate_formula(a, b) for a, b in sorted(pairs)],
        method=METHOD_NOTE,
        convention_note=CONVENTION_NOTE,
        policy_note=POLICY_NOTE,
    )


def _missing(rows: list[dict], view: FxView, amounts: dict[str, Decimal]) -> list[MissingRate]:
    out = []
    for r in rows:
        ev = view.evidence[r["invoice_number"]]
        if ev["status"] == "UNAVAILABLE":
            out.append(MissingRate(
                invoice_number=r["invoice_number"], invoice_date=r["invoice_date"].date(),
                currency=r["currency"], amount=money(amounts[r["invoice_number"]]),
                reason=ev["reason"], missing_evidence=ev["missing_evidence"]))
    return out


def build_summary(store: DataStore, policy: PolicyDocument, view: FxView,
                  fx_status: dict[str, FxSeriesStatus]) -> SummaryResponse:
    if view.reporting_currency != "EUR" or view.exact_date:
        raise ValueError("summary is defined on the default EUR view (invoice-date convention)")
    rows = view.frame.to_dict("records")
    amounts = store.amounts

    # ---- consolidated EUR ---------------------------------------------------------------
    total = _eur_total(rows, view)
    if total is not None:
        lines = []
        for t in _currency_totals(rows, amounts):
            sub = [r for r in rows if r["currency"] == t.currency]
            lines.append(CurrencyConversion(currency=t.currency, original_amount=t.amount,
                                            invoice_count=t.invoice_count,
                                            converted_amount=money(_eur_total(sub, view))))
        consolidated = Consolidated(available=True, total=money(total), invoice_count=len(rows),
                                    by_currency=lines, evidence=_evidence_summary(rows, view, fx_status))
    else:
        missing = _missing(rows, view, amounts)
        absent = [s for s, st in fx_status.items() if st.state != "ok"]
        reason = (f"No validated ECB reference-rate snapshot is loaded for {', '.join(absent)}." if absent else
                  f"{len(missing)} invoice(s) have no acceptable ECB reference rate under the invoice-date convention.")
        consolidated = Consolidated(available=False, total=None, invoice_count=len(rows),
                                    unavailable_reason=reason, missing=missing,
                                    recommendation=REVIEW_RECOMMENDATION)

    # ---- by vendor status ---------------------------------------------------------------
    by_status = []
    for status in STATUS_ORDER:
        group = [r for r in rows if r["vendor_status"] == status]
        eur = _eur_total(group, view) if group else Decimal(0)
        by_status.append(StatusBreakdown(
            status=status,
            vendor_count=int((store.vendors["status"] == status).sum()),
            invoice_count=len(group),
            original_currency_totals=_currency_totals(group, amounts),
            eur_total=money(eur) if eur is not None else None,
            eur_available=eur is not None,
        ))

    # ---- top vendors (ranking needs every vendor's EUR total) ------------------------------
    top, top_reason = None, None
    if total is None:
        top_reason = "Ranking in EUR is unavailable because some invoices lack an acceptable ECB reference rate."
    else:
        per_vendor: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            per_vendor[r["vendor_id"]].append(r)
        vendors = store.vendors.set_index("vendor_id")
        ranked = sorted(per_vendor.items(), key=lambda kv: (-_eur_total(kv[1], view), kv[0]))
        top = []
        for i, (vid, vrows) in enumerate(ranked[:TOP_N], start=1):
            v = vendors.loc[vid]
            top.append(VendorSpend(
                rank=i, vendor_id=vid, vendor_name=v["vendor_name"], country=v["country"],
                category=v["category"], status=v["status"], eur_total=money(_eur_total(vrows, view)),
                invoice_count=len(vrows), original_currency_totals=_currency_totals(vrows, amounts)))

    lo, hi = store.date_range
    return SummaryResponse(
        generated_at=datetime.now(timezone.utc).isoformat(),
        dataset=DatasetInfo(invoice_count=len(rows), vendor_count=len(store.vendors), date_range=(lo, hi),
                            file_sha256=store.file_sha256, policy_document=policy.doc_label),
        original_currency_totals=_currency_totals(rows, amounts),
        consolidated=consolidated,
        by_vendor_status=by_status,
        top_vendors_eur=top,
        top_vendors_unavailable_reason=top_reason,
        data_quality=[DataQualityItem(code=n.code, message=n.message, count=n.count,
                                      invoice_numbers=n.invoice_numbers) for n in store.notes],
        fx_status=fx_status,
    )
