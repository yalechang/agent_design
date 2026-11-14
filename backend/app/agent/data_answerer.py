"""Step 3 — answer from the invoice/vendor records (and ECB rate conversions) via verified generated code.

3a  GPT writes a pandas function against the schema (never sees rows)
3b  static validation (sandbox.validate)
3c  isolated execution (sandbox.execute) + probe runs for FX completeness
3d  application verification: every declared aggregation is recomputed in Decimal
3e  one repair round with the exact error; then QUERY_FAILED with an explanation
3f  GPT explanation with {{row:column}} placeholders; the app substitutes verified values
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
from pydantic import BaseModel

from app.agent import prompts, sandbox
from app.agent.llm import LLM
from app.agent.router import Route
from app.agent.schema_context import INVOICE_COLUMNS, VENDOR_COLUMNS
from app.api.schemas import (AggregationCheck, CurrencyTotal, DataEvidence, FxEvidenceBlock, FxLegSource, FxRef,
                             GroupBreakdown, KeyFigure, Limitation, ResultTable, SupportingRows)
from app.data.store import DataStore
from app.fx.convert import METHOD_NOTE, RateTable
from app.fx.enrich import FxView
from app.fx.series import CONVENTION_NOTE, POLICY_NOTE, SERIES
from app.tracing.trajectory import Trace

CODEGEN = prompts.load("codegen")
EXPLAIN = prompts.load("explain")
MAX_ATTEMPTS = 2
MAX_SUPPORTING_ROWS = 500
MAX_ROWS_TO_EXPLAIN = 50
PROBE_HIGH, PROBE_LOW = 1e15, -1e15
AGGS = {"sum", "count", "mean", "min", "max"}
CENT = Decimal("0.01")
ROW_SOURCES = ["invoices_2025_clean.csv", "vendor_master.csv"]

SUPPORTING_COLUMNS = ["invoice_number", "invoice_date", "vendor_id", "vendor_name", "vendor_country",
                      "vendor_status", "description", "cost_center", "amount", "currency", "po_number",
                      "approved_by"]
FX_COLUMNS = ["fx_status", "fx_observation_date", "fx_ref_id"]


class CodeDraft(BaseModel):
    can_answer: bool
    reason: str
    code: str
    result_description: str


class FigureRef(BaseModel):
    label: str
    row: int
    column: str


class Explanation(BaseModel):
    answer_text: str
    key_figures: list[FigureRef]


@dataclass
class DataResult:
    status: str                          # ANSWERED | PARTIAL | QUERY_FAILED | ERROR
    answer_text: str | None = None
    key_figures: list[KeyFigure] = field(default_factory=list)
    data: DataEvidence | None = None
    fx: FxEvidenceBlock | None = None
    limitations: list[Limitation] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    failure_message: str | None = None
    error_kind: str | None = None


@dataclass
class DataContext:
    store: DataStore
    view: FxView
    rates: RateTable


# ---------------------------------------------------------------------------- schema / frames

def rates_frame(rates: RateTable, start: date = date(2024, 1, 1)) -> pd.DataFrame:
    days = sorted({d for s in rates.snapshots.values() for d in s.observations if d >= start})
    data = {"date": pd.to_datetime(days)}
    for code in SERIES:
        col = f"{code.lower()}_per_eur"
        data[col] = [float(v) if (v := rates.value(code, d)) is not None else np.nan for d in days]
    return pd.DataFrame(data)


def schema_text(view: FxView) -> str:
    rep = view.amount_column
    inv = dict(INVOICE_COLUMNS)
    inv.update({
        "invoice_date": "invoice date, pandas datetime64 (all in 2025)",
        "amount": "float, invoice amount in its ORIGINAL currency (see `currency`); negative = credit note",
        "po_number": "string or missing (NaN) when the invoice has no PO",
        "approved_by": "string or missing (NaN) when no approver is recorded",
        "vendor_country": "vendor country from vendor master (e.g. Spain, UK, Germany, France)",
        "vendor_category": "vendor category from vendor master",
        "vendor_status": "vendor status from vendor master: ACTIVE, INACTIVE or ON_HOLD",
        "is_credit_note": "bool, amount < 0",
        rep: f"float, amount converted to {view.reporting_currency} by the application with ECB reference rates "
             f"(rounded to cents); NaN when no acceptable rate exists",
        "fx_status": "IDENTITY (already in reporting currency) | EXACT | FALLBACK (earlier ECB rate date used: weekend or TARGET holiday) | "
                     "UNAVAILABLE",
        "fx_observation_date": "datetime64, ECB reference-rate date used for the conversion (NaT for IDENTITY/UNAVAILABLE)",
        "fx_ref_id": "key into fx_refs (None for IDENTITY/UNAVAILABLE)",
    })
    cols = list(view.frame.columns)
    lines = ["invoices (one row per invoice, 140 rows; vendor attributes already joined):"]
    lines += [f"  - {c} [{view.frame[c].dtype}]: {inv.get(c, '')}" for c in cols]
    lines.append("vendors (10 rows):")
    lines += [f"  - {c}: {d}" for c, d in VENDOR_COLUMNS.items()]
    lines.append("fx_refs (one row per ECB rate observation used by an invoice conversion): "
                 + ", ".join(view.refs.columns))
    lines.append("rates (ECB euro reference rates since 2024-01-01, one row per ECB publication day; no rows on "
                 "weekends or TARGET holidays): date [datetime64], usd_per_eur, gbp_per_eur (units of the currency "
                 "per 1 EUR). Use only to look up rates.")
    return "\n".join(lines)


def known_columns(frames: dict[str, pd.DataFrame], view: FxView) -> set[str]:
    cols = set(view.frame.columns)
    for f in frames.values():
        cols |= set(f.columns)
    return cols | {"result", "supporting_invoice_numbers", "aggregations", "notes"}


# ---------------------------------------------------------------------------- verification

class VerificationError(Exception):
    pass


@dataclass
class Verified:
    checks: list[AggregationCheck]
    exact: dict[tuple[int, str], Decimal | None]   # (row, column) -> verified exact value
    money_columns: dict[str, str]


def _source_values(col: str, rows: pd.DataFrame, ctx: DataContext) -> list[Decimal | None]:
    if col == "amount":
        return [ctx.store.amounts[n] for n in rows["invoice_number"]]
    if col == ctx.view.amount_column:
        return [ctx.view.amounts[n] for n in rows["invoice_number"]]
    return [None if pd.isna(v) else v for v in rows[col]]


def verify(out: dict, ctx: DataContext, affected: set[str]) -> Verified:
    frame, rep_col = ctx.view.frame, ctx.view.amount_column
    cols, rows = out["columns"], out["rows"]
    sup = list(dict.fromkeys(out["supporting_invoice_numbers"]))
    known = set(frame["invoice_number"])
    unknown = [s for s in sup if s not in known]
    if unknown:
        raise VerificationError(f"supporting_invoice_numbers contains unknown invoices: {unknown[:5]}")
    aggs = out["aggregations"]
    if rows and aggs and not sup:
        raise VerificationError("aggregations were declared but supporting_invoice_numbers is empty")
    sup_df = frame[frame["invoice_number"].isin(sup)]

    checks: list[AggregationCheck] = []
    exact: dict[tuple[int, str], Decimal | None] = {}
    money: dict[str, str] = {}

    for a in aggs:
        column, agg, src = a.get("column"), a.get("agg"), a.get("source_column")
        group_by = a.get("group_by") or []
        if isinstance(group_by, str):
            group_by = [group_by]
        if column not in cols:
            raise VerificationError(f"aggregation column '{column}' is not a result column ({cols})")
        if agg not in AGGS:
            raise VerificationError(f"aggregation '{agg}' for '{column}' must be one of {sorted(AGGS)}")
        if src not in frame.columns:
            raise VerificationError(f"source_column '{src}' for '{column}' is not an invoices column")
        is_money = src in ("amount", rep_col)
        if is_money and agg != "count":
            money[column] = ctx.view.reporting_currency if src == rep_col else "ORIGINAL"

        base = dict(column=column, agg=agg, source_column=src, group_by=group_by)
        derived = [g for g in group_by if g not in frame.columns]
        missing_in_result = [g for g in group_by if g not in cols]
        if derived or missing_in_result:
            checks.append(AggregationCheck(**base, status="unverified",
                                           detail=f"group keys {derived or missing_in_result} cannot be matched "
                                                  f"to invoice columns, so this column was not recomputed"))
            continue

        groups: dict[tuple, pd.DataFrame] = {}
        if group_by:
            for key, g in sup_df.groupby(group_by, dropna=False, sort=False):
                key = key if isinstance(key, tuple) else (key,)
                groups[tuple(_keyval(k) for k in key)] = g
        else:
            groups[()] = sup_df

        status, details = "verified", []
        ci = cols.index(column)
        for r, row in enumerate(rows):
            key = tuple(_keyval(row[cols.index(g)]) for g in group_by)
            g = groups.get(key)
            if g is None:
                raise VerificationError(f"result row {r} ({dict(zip(group_by, key))}) has no supporting invoices")
            if src == "amount" and agg != "count" and g["currency"].nunique() > 1:
                raise VerificationError(f"'{column}' aggregates `amount` across currencies "
                                        f"{sorted(g['currency'].unique())}; use {rep_col} or group by currency")
            vals = _source_values(src, g, ctx)
            expected = _recompute(agg, vals)
            # Conservative: if the query would include any invoice without a rate (seen directly or via the
            # probe runs), no converted aggregate is presented as complete.
            uses_unavailable = src == rep_col and (any(v is None for v in vals) or bool(affected))
            got = row[ci]
            if uses_unavailable:
                exact[(r, column)] = None
                status = "unavailable"
                details.append(f"row {r}: includes invoices without an acceptable ECB rate")
                continue
            if not _matches(agg, expected, got, is_money):
                raise VerificationError(f"'{column}' row {r}: code returned {got}, application recomputed "
                                        f"{expected} from the supporting invoices")
            exact[(r, column)] = expected.quantize(CENT) if is_money and agg != "count" and expected is not None \
                else expected
        detail = (f"recomputed in Decimal from {len(sup_df)} supporting invoices"
                  + (f" grouped by {group_by}" if group_by else "")
                  + ("; " + "; ".join(details[:3]) if details else ""))
        checks.append(AggregationCheck(**base, status=status, detail=detail))

    # Row-level money columns in invoice lists.
    if "invoice_number" in cols:
        ni = cols.index("invoice_number")
        for c in ("amount", rep_col):
            if c in cols and c not in money:
                money[c] = ctx.view.reporting_currency if c == rep_col else "ORIGINAL"
                ci = cols.index(c)
                for r, row in enumerate(rows):
                    n = row[ni]
                    if n in ctx.store.amounts:
                        exact[(r, c)] = ctx.store.amounts[n] if c == "amount" else ctx.view.amounts[n]
    return Verified(checks, exact, money)


def _keyval(v) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "<missing>"
    if isinstance(v, pd.Timestamp):
        return v.date().isoformat()
    if isinstance(v, (bool, np.bool_)):
        return str(bool(v))
    if isinstance(v, (int, np.integer)) or (isinstance(v, float) and v.is_integer()):
        return str(int(v))
    return str(v)


def _recompute(agg: str, vals: list) -> Decimal | None:
    present = [v for v in vals if v is not None]
    if agg == "count":
        return Decimal(len(present))
    if any(v is None for v in vals):
        return None
    dec = [v if isinstance(v, Decimal) else Decimal(str(v)) for v in vals]
    if not dec:
        return Decimal(0) if agg == "sum" else None
    if agg == "sum":
        return sum(dec, Decimal(0))
    if agg == "mean":
        return sum(dec, Decimal(0)) / len(dec)
    return min(dec) if agg == "min" else max(dec)


def _matches(agg: str, expected: Decimal | None, got, is_money: bool) -> bool:
    if expected is None:
        return got is None
    if got is None or isinstance(got, str):
        return False
    tol = Decimal("0") if agg == "count" else (Decimal("0.01") if is_money else Decimal("0.000001"))
    return abs(Decimal(str(got)) - expected) <= tol


# ---------------------------------------------------------------------------- formatting

def fmt_money(v: Decimal | None, currency: str | None) -> str:
    if v is None:
        return "unavailable"
    s = f"{v.quantize(CENT):,.2f}"
    return f"{currency} {s}" if currency else s


def display_value(table: ResultTable, row: int, column: str) -> str | None:
    if row < 0 or row >= len(table.rows) or column not in table.columns:
        return None
    v = table.rows[row][table.columns.index(column)]
    cur = table.money_columns.get(column)
    if cur and (v is None or isinstance(v, (int, float, str))):
        if cur == "ORIGINAL":
            cur = table.rows[row][table.columns.index("currency")] if "currency" in table.columns else None
        try:
            return fmt_money(Decimal(str(v)) if v is not None else None, cur)
        except ArithmeticError:
            return str(v)
    if v is None:
        return "unavailable"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int) or (isinstance(v, float) and v.is_integer() and abs(v) < 1e12):
        return f"{int(v):,}"
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    return str(v)


_PLACEHOLDER = re.compile(r"\{\{\s*(\d+)\s*:\s*([^}]+?)\s*\}\}")
_IDS = re.compile(r"\b(?:INV|PO|V|CC)-[A-Z0-9-]+\b|\b\d{4}-\d{2}-\d{2}\b|§\s*\d+(?:\.\w+)?|\b\d+\.T\d\b")
_NUMBER = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d{4,})(?![\w])")


def typed_numbers(text: str, allowed: set[str] = frozenset()) -> list[str]:
    """Numbers the model typed itself. Excluded: placeholders, ids, dates, years, clause ids and any number
    that appears in ``allowed`` (e.g. numbers from the analyst's own question)."""
    t = _IDS.sub(" ", _PLACEHOLDER.sub(" ", text))
    norm = {a.replace(",", "") for a in allowed}
    return [n for n in _NUMBER.findall(t)
            if not re.fullmatch(r"20\d\d", n) and n not in allowed and n.replace(",", "") not in norm]


JARGON = {"amount_eur", "amount_usd", "amount_gbp", "fx_status", "fx_observation_date", "fx_ref_id",
          "vendor_id", "vendor_name", "vendor_country", "vendor_status", "invoice_number", "approved_by",
          "po_number", "cost_center", "is_credit_note", "NaN", "DataFrame", "dataframe", "result set",
          "IDENTITY", "FALLBACK", "UNAVAILABLE", "EXACT"}


def technical_terms(text: str, terms: set[str]) -> list[str]:
    """Code-level identifiers that should not appear in an analyst-facing explanation."""
    return sorted(t for t in terms if re.search(rf"(?<![\w]){re.escape(t)}(?![\w])", text))


def substitute(text: str, table: ResultTable) -> tuple[str, list[str]]:
    bad: list[str] = []

    def rep(m):
        val = display_value(table, int(m.group(1)), m.group(2))
        if val is None:
            bad.append(m.group(0))
            return "[n/a]"
        return val
    return _PLACEHOLDER.sub(rep, text), bad


# ---------------------------------------------------------------------------- evidence builders

def _currency_totals(numbers: list[str], store: DataStore) -> list[CurrencyTotal]:
    tot, cnt, cred = defaultdict(Decimal), defaultdict(int), defaultdict(int)
    cur = store.invoices.set_index("invoice_number")["currency"]
    for n in numbers:
        c = cur[n]
        tot[c] += store.amounts[n]
        cnt[c] += 1
        cred[c] += store.amounts[n] < 0
    return [CurrencyTotal(currency=c, amount=f"{tot[c]:.2f}", invoice_count=cnt[c], credit_note_count=cred[c])
            for c in sorted(tot)]


def _supporting_rows(sup: list[str], ctx: DataContext, include_fx: bool) -> SupportingRows:
    frame = ctx.view.frame
    df = frame[frame["invoice_number"].isin(sup)].sort_values(["invoice_date", "invoice_number"])
    rep = ctx.view.amount_column
    cols = SUPPORTING_COLUMNS + ([rep] + FX_COLUMNS if include_fx else [])
    rows = []
    for rec in df.head(MAX_SUPPORTING_ROWS).to_dict("records"):
        out = []
        for c in cols:
            v = rec[c]
            if c == "amount":
                v = f"{ctx.store.amounts[rec['invoice_number']]:.2f}"
            elif c == rep:
                a = ctx.view.amounts[rec["invoice_number"]]
                v = f"{a:.2f}" if a is not None else None
            elif isinstance(v, pd.Timestamp):
                v = v.date().isoformat()
            elif v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NaT:
                v = None
            elif isinstance(v, (np.bool_, bool)):
                v = bool(v)
            out.append(v)
        rows.append(out)
    return SupportingRows(columns=cols, rows=rows, total_count=len(df), truncated=len(df) > MAX_SUPPORTING_ROWS)


def _fx_block(sup: list[str], ctx: DataContext, convert_calls: list[dict]) -> FxEvidenceBlock:
    view = ctx.view
    coverage = {k: 0 for k in ("IDENTITY", "EXACT", "FALLBACK", "UNAVAILABLE")}
    by_ref: dict[str, FxRef] = {}
    unavailable: list[str] = []
    used_series: set[str] = set()
    any_fallback = False
    for n in sup:
        ev = view.evidence[n]
        coverage[ev["status"]] += 1
        used_series.update(l["currency"] for l in ev["legs"])
        if ev["status"] == "UNAVAILABLE":
            unavailable.append(n)
            key = f"UNAVAILABLE|{n}"
        elif ev["status"] == "IDENTITY":
            continue
        else:
            # One row per (ECB rate date, invoice date), so "n days earlier" is exact for every invoice listed.
            key = f"{ev['fx_ref_id']}|{ev['requested_date']}"
        any_fallback |= ev["status"] == "FALLBACK"
        if key in by_ref:
            by_ref[key].invoice_numbers.append(n)
            continue
        by_ref[key] = FxRef(**{k: ev[k] for k in FxRef.model_fields if k in ev and k != "invoice_numbers"},
                            invoice_numbers=[n])
    refs = sorted(by_ref.values(), key=lambda r: (r.observation_date or "", r.requested_date, r.fx_ref_id or ""))
    for ev in convert_calls:
        used_series.update(l["currency"] for l in ev["legs"])
        any_fallback |= ev["status"] == "FALLBACK"
        refs.append(FxRef(**{k: ev[k] for k in FxRef.model_fields if k in ev}))
    snaps = ctx.rates.snapshots
    sources = [FxLegSource(currency=s, series=SERIES[s].name, url=SERIES[s].url, page=SERIES[s].page,
                           snapshot_id=snaps[s].snapshot_id if s in snaps else None,
                           snapshot_fetched_at=snaps[s].fetched_at.isoformat() if s in snaps else None,
                           release=snaps[s].parsed.release_label if s in snaps else None)
               for s in SERIES if s in used_series]
    return FxEvidenceBlock(reporting_currency=view.reporting_currency, exact_date=view.exact_date,
                           coverage=coverage, refs=refs, sources=sources, method=METHOD_NOTE,
                           convention_note=CONVENTION_NOTE if any_fallback else None,
                           policy_note=POLICY_NOTE, unavailable_invoices=unavailable)


# ---------------------------------------------------------------------------- main entry

def _plan_text(route: Route) -> str:
    return json.dumps({"intent": route.intent, "sources": route.sources,
                       "reporting_currency": route.reporting_currency,
                       "original_currency_filter": route.original_currency_filter,
                       "exact_date_fx": route.exact_date_fx, "rationale": route.rationale}, indent=1)


def _policy_text(citations: list) -> str:
    if not citations:
        return ""
    lines = ["\nRelevant verified policy clauses (apply them exactly; thresholds are in EUR):"]
    for c in citations:
        lines.append(f"[{c.clause.id}] {c.clause.text}" + (f"  (columns: {c.clause.table_header})"
                                                            if c.clause.table_header else ""))
    return "\n".join(lines)


def answer(llm: LLM, ctx: DataContext, question: str, route: Route, policy_citations: list,
           trace: Trace) -> DataResult:
    view = ctx.view
    frames = {"vendors": ctx.store.vendors, "fx_refs": view.refs, "rates": rates_frame(ctx.rates)}
    known = known_columns(frames, view)
    instructions = CODEGEN.render(
        SCHEMA=schema_text(view), PLAN=_plan_text(route), POLICY=_policy_text(policy_citations),
        REP=view.reporting_currency, REP_LOWER=view.reporting_currency.lower(),
        FILTER=(f"only invoices with currency == '{route.original_currency_filter}'"
                if route.original_currency_filter else "none"),
    )
    variants = [view.frame] + probe_variants(view)

    user_input, last_problem, out, draft, exec_res = question, None, None, None, None
    verified = None
    affected: set[str] = set()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = llm.parse(step="codegen", instructions=instructions, input=user_input, schema=CodeDraft)
        draft = result.parsed
        step = trace.llm_step("codegen", result, CODEGEN.version, user_input, attempt=attempt)
        if not draft.can_answer:
            trace.step("codegen_declined", reason=draft.reason)
            return DataResult(status="QUERY_FAILED", failure_message=_declined_message(draft.reason, view),
                              error_kind="UNSUPPORTED_QUERY")
        problems = sandbox.validate(draft.code, known)
        step["static_check"] = problems or "ok"
        if problems:
            last_problem = "The generated query was rejected: " + "; ".join(problems)
        else:
            exec_res = sandbox.execute(draft.code, frames, variants, ctx.rates)
            trace.step("exec", attempt=attempt, ok=exec_res.ok, error=exec_res.describe_error() if not exec_res.ok
                       else None, sandbox_notes=exec_res.sandbox_notes,
                       result_preview=(exec_res.outputs[0]["rows"][:10] if exec_res.ok else None),
                       columns=(exec_res.outputs[0]["columns"] if exec_res.ok else None),
                       supporting_count=(len(exec_res.outputs[0]["supporting_invoice_numbers"])
                                         if exec_res.ok else None))
            if not exec_res.ok:
                last_problem = "The query failed while running: " + exec_res.describe_error()
            else:
                out = exec_res.outputs[0]
                affected = _affected(exec_res.outputs, view)
                try:
                    verified = verify(out, ctx, affected)
                    trace.step("verify", attempt=attempt, ok=True, checks=verified.checks,
                               affected_invoices=sorted(affected))
                    break
                except VerificationError as e:
                    trace.step("verify", attempt=attempt, ok=False, problem=str(e))
                    last_problem = "The query result failed application verification: " + str(e)
                    out = None
        user_input = (f"{question}\n\nYour previous code:\n{draft.code}\n\nProblem: {last_problem}\n"
                      "Return corrected code following every rule.")
    if verified is None:
        trace.step("data_failed", problem=last_problem)
        return DataResult(status="QUERY_FAILED", failure_message=_failure_message(last_problem, view),
                          error_kind="QUERY_FAILED")

    return _finish(llm, ctx, question, route, policy_citations, draft, out, verified, affected, trace)


def probe_variants(view: FxView) -> list[pd.DataFrame]:
    """Copies of the invoices in which rows WITHOUT a rate are disguised as ordinary converted rows
    (extreme amount, fx_status EXACT, plausible observation date). Whatever filter the generated code
    applies, if it would include such an invoice in either probe, that invoice affects the answer."""
    if not view.unavailable:
        return []
    out = []
    mask = view.frame["invoice_number"].isin(view.unavailable)
    for probe in (PROBE_HIGH, PROBE_LOW):
        v = view.frame.copy()
        v.loc[mask, view.amount_column] = probe
        v.loc[mask, "fx_status"] = "EXACT"
        v.loc[mask, "fx_observation_date"] = v.loc[mask, "invoice_date"]
        v.loc[mask, "fx_ref_id"] = "probe"
        out.append(v)
    return out


def _affected(outputs: list[dict], view: FxView) -> set[str]:
    """Invoices without a rate that the query would include (primary run or any probe)."""
    if not view.unavailable:
        return set()
    unavailable = set(view.unavailable)
    hits = set(outputs[0]["supporting_invoice_numbers"]) & unavailable
    for o in outputs[1:]:
        if "probe_error" in o:
            return unavailable        # cannot tell: assume every unavailable invoice could be affected
        hits |= set(o["supporting_invoice_numbers"]) & unavailable
    return hits


def _finish(llm, ctx, question, route, policy_citations, draft, out, verified: Verified, affected, trace):
    view = ctx.view
    cols = out["columns"]
    rows = [list(r) for r in out["rows"]]
    for (r, c), v in verified.exact.items():
        rows[r][cols.index(c)] = f"{v:.2f}" if (v is not None and c in verified.money_columns) else (
            None if v is None else (int(v) if v == v.to_integral_value() and c not in verified.money_columns
                                    else float(v)))
    money_columns = dict(verified.money_columns)
    for c in cols:   # display-only currency for columns named like amount_eur / total_usd
        m = re.search(r"(?:^|_)(eur|usd|gbp)$", c)
        if c not in money_columns and m and c not in ("currency",):
            money_columns[c] = m.group(1).upper()
    table = ResultTable(columns=cols, rows=rows, money_columns=money_columns,
                        row_count=out["row_count"], truncated=out["truncated"])
    sup = list(dict.fromkeys(out["supporting_invoice_numbers"]))
    rep = view.amount_column
    uses_conversion = (any(c.source_column == rep for c in verified.checks) or rep in cols
                       or bool(out.get("convert_calls")))
    limitations: list[Limitation] = []
    caveats: list[str] = []
    status = "ANSWERED"

    fx = _fx_block(sup, ctx, out.get("convert_calls", [])) if (uses_conversion or "fx" in route.sources) else None
    if affected or (fx and fx.unavailable_invoices and uses_conversion):
        miss = sorted(affected | set(fx.unavailable_invoices if fx else []))
        status = "PARTIAL"
        reason = "exact invoice dates were requested" if view.exact_date else "no ECB reference rate was published " \
                                                                             "within 7 days before the invoice date"
        limitations.append(Limitation(
            kind="FX_UNAVAILABLE",
            message=f"{len(miss)} invoice(s) relevant to this answer have no acceptable ECB reference rate ({reason}), so "
                    f"{view.reporting_currency} figures that depend on them are shown as unavailable rather "
                    f"than as an incomplete total.",
            affected_invoices=miss,
            recommendation="Review these invoices or supply the missing ECB reference rates; their original-"
                           "currency amounts are listed in the evidence."))
    failed_calls = [c for c in out.get("convert_calls", []) if c.get("result") is None]
    if failed_calls:
        status = "PARTIAL"
        limitations.append(Limitation(
            kind="FX_UNAVAILABLE",
            message="A requested conversion has no acceptable ECB reference rate: "
                    + "; ".join(m for c in failed_calls for m in c.get("missing_evidence", [])),
            recommendation="Provide a date on which the ECB published reference rates, or review manually."))
    if any(c.status == "unverified" for c in verified.checks):
        caveats.append("Some result columns use derived groupings and were not independently recomputed; "
                       "see the verification list.")
    if out["truncated"]:
        caveats.append("The result table was truncated to 1,000 rows.")
    if fx and uses_conversion:
        if fx.convention_note:
            caveats.append(CONVENTION_NOTE)

    group_breakdowns = []
    gb = next((c.group_by for c in verified.checks if c.group_by and c.status != "unverified"), None)
    if gb and len(gb) <= 2:
        frame = view.frame[view.frame["invoice_number"].isin(sup)]
        for key, g in frame.groupby(gb, sort=False):
            key = key if isinstance(key, tuple) else (key,)
            group_breakdowns.append(GroupBreakdown(group={k: _keyval(v) for k, v in zip(gb, key)},
                                                   totals=_currency_totals(list(g["invoice_number"]), ctx.store)))

    data = DataEvidence(
        result_description=draft.result_description, result_table=table,
        supporting_rows=_supporting_rows(sup, ctx, include_fx=fx is not None),
        original_currency_breakdown=_currency_totals(sup, ctx.store), group_breakdowns=group_breakdowns,
        aggregations=verified.checks, notes=out["notes"], code=draft.code, row_sources=ROW_SOURCES)

    text, figures = _explain(llm, question, route, data, fx, limitations, policy_citations, trace)
    return DataResult(status=status, answer_text=text, key_figures=figures, data=data, fx=fx,
                      limitations=limitations, caveats=caveats)


def _explain(llm, question, route, data: DataEvidence, fx, limitations, policy_citations, trace):
    t = data.result_table
    facts = {
        "question": question,
        "reporting_currency": route.reporting_currency,
        "original_currency_filter": route.original_currency_filter,
        "result_description": data.result_description,
        "result": {"columns": t.columns,
                   "rows": [[display_value(t, r, c) for c in t.columns] for r in range(min(len(t.rows),
                                                                                           MAX_ROWS_TO_EXPLAIN))],
                   "row_count": t.row_count},
        "supporting_invoice_count": data.supporting_rows.total_count,
        "original_currency_breakdown": [x.model_dump() for x in data.original_currency_breakdown],
        "query_notes": data.notes,
        "fx": None if fx is None else {
            "invoices_converted_with_ecb_rates": fx.coverage["EXACT"] + fx.coverage["FALLBACK"],
            "of_which_used_an_earlier_date_because_of_weekend_or_holiday": fx.coverage["FALLBACK"],
            "invoices_already_in_reporting_currency": fx.coverage["IDENTITY"],
            "invoices_without_an_acceptable_rate": fx.coverage["UNAVAILABLE"],
            "exact_invoice_date_rates_only": fx.exact_date},
        "limitations": [l.message for l in limitations],
        "policy_clauses": [{"clause_id": c.clause.id, "quote": c.quote} for c in policy_citations],
    }
    instructions = EXPLAIN.render(FACTS=json.dumps(facts, indent=1, default=str))
    user_input = "Write the explanation."
    # Numbers the analyst typed and ids of the cited clauses (e.g. "3.1") are not model-typed figures.
    allowed = set(_NUMBER.findall(question)) | {c.clause.id for c in policy_citations}
    for c in policy_citations:   # thresholds quoted from the policy (e.g. 50,000) are not invented figures
        allowed |= set(_NUMBER.findall(c.clause.text))
    jargon_terms = {c for c in t.columns if "_" in c} | JARGON
    for attempt in (1, 2):
        result = llm.parse(step="explain", instructions=instructions, input=user_input, schema=Explanation)
        exp: Explanation = result.parsed
        typed = typed_numbers(exp.answer_text, allowed)
        text, bad = substitute(exp.answer_text, t)
        jargon = technical_terms(_PLACEHOLDER.sub(" ", exp.answer_text), jargon_terms)
        trace.llm_step("explain", result, EXPLAIN.version, user_input, attempt=attempt, typed_numbers=typed,
                       bad_placeholders=bad, jargon=jargon, final_text=text)
        if not typed and not bad and not jargon:
            break
        if attempt == 2 and not typed and not bad:
            break   # wording only: accept the second draft rather than hide a correct answer
        user_input = ("Write the explanation. Your previous draft "
                      + (f"typed numbers {typed} directly; " if typed else "")
                      + (f"used invalid placeholders {bad}; " if bad else "")
                      + (f"used technical column names {jargon}, write in plain business words instead; "
                         if jargon else "")
                      + "use only {{row:column}} placeholders that exist in result.")
    else:
        text = f"{data.result_description} The verified result table and its evidence are shown below."
        trace.step("explain_fallback", reason="placeholder rules violated twice")
    figures = []
    for f in exp.key_figures[:5]:
        val = display_value(t, f.row, f.column)
        if val is not None:
            cur = t.money_columns.get(f.column)
            figures.append(KeyFigure(label=f.label, value=val, currency=cur if cur and cur != "ORIGINAL" else None))
    return text, figures


def _columns_hint(view: FxView) -> str:
    rep = view.reporting_currency
    return ("invoice number, invoice date, vendor, vendor country, vendor category, vendor status (active, on hold, "
            "inactive), description, cost center, amount and original currency, amount in " + rep + ", PO number, "
            "approver, and whether it is a credit note")


PLAIN_NAMES = {
    "invoice_number": "invoice number", "invoice_date": "invoice date", "vendor_id": "vendor id",
    "vendor_name": "vendor name", "vendor_country": "vendor country", "vendor_category": "vendor category",
    "vendor_status": "vendor status", "cost_center": "cost center", "po_number": "PO number",
    "approved_by": "approver", "is_credit_note": "credit note flag", "amount_eur": "amount in EUR",
    "amount_usd": "amount in USD", "amount_gbp": "amount in GBP", "fx_status": "exchange-rate status",
    "fx_observation_date": "exchange-rate date",
}


def plain_language(text: str) -> str:
    """Replace code-level column names with the words an analyst would use."""
    for col, words in sorted(PLAIN_NAMES.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(rf"(?<![\w]){re.escape(col)}(?![\w])", words, text)
    return text


def _declined_message(reason: str, view: FxView) -> str:
    reason = plain_language(reason)
    return (f"This question could not be turned into a query on the available data. {reason.strip()} "
            f"Available invoice fields: {_columns_hint(view)}. Please rephrase using these fields.")


def _failure_message(problem: str | None, view: FxView) -> str:
    return (f"The question could not be answered from the data because the generated query did not run "
            f"successfully after a retry. {problem or ''} Available invoice fields: {_columns_hint(view)}. "
            f"Try rephrasing the question, e.g. naming the field or filter you mean.")
