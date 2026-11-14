"""Independent reference answers for the golden eval set.

Shares no code with the app: stdlib csv + Decimal, its own reading of the cached raw ECB csvdata
responses and its own implementation of the invoice-date / 7-day-fallback convention.
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, getcontext
from pathlib import Path

getcontext().prec = 28
ROOT = Path(__file__).resolve().parents[2]
CENT = Decimal("0.01")


def _rates(path: Path) -> dict[date, Decimal]:
    """date -> units of currency per 1 EUR (final observations only)."""
    return {date.fromisoformat(r["TIME_PERIOD"]): Decimal(r["OBS_VALUE"])
            for r in csv.DictReader(open(path, encoding="utf-8")) if r["OBS_STATUS"] == "A"}


class Reference:
    def __init__(self, var_dir: Path = ROOT / "var", data_dir: Path = ROOT / "data"):
        cache = var_dir / "fx_cache" / "ecb"
        active = json.loads((cache / "active.json").read_text())
        self.per_eur = {c: _rates(cache / c / f"{active[c]}.csv") for c in ("USD", "GBP")}
        self.vendors = {r["vendor_id"]: r for r in csv.DictReader(open(data_dir / "vendor_master.csv"))}
        self.invoices = list(csv.DictReader(open(data_dir / "invoices_2025_clean.csv")))
        for r in self.invoices:
            r["amt"] = Decimal(r["amount"])
            r["day"] = date.fromisoformat(r["invoice_date"])
            v = self.vendors[r["vendor_id"]]
            r["country"], r["status"] = v["country"], v["status"]

    # ---- conversion ------------------------------------------------------------------------
    def rate_date(self, src: str, dst: str, day: date, exact: bool = False) -> date | None:
        legs = {src, dst} - {"EUR"}
        for back in range(0, 1 if exact else 8):
            d = day - timedelta(days=back)
            if all(self.per_eur[c].get(d) is not None for c in legs):
                return d
        return None

    def convert(self, amount: Decimal, src: str, dst: str, day: date, exact: bool = False) -> Decimal | None:
        if src == dst:
            return amount
        d = self.rate_date(src, dst, day, exact)
        if d is None:
            return None
        x = lambda c: Decimal(1) if c == "EUR" else self.per_eur[c][d]
        return (amount * x(dst) / x(src)).quantize(CENT, rounding=ROUND_HALF_UP)

    def to(self, r: dict, dst: str = "EUR", exact: bool = False) -> Decimal | None:
        return self.convert(r["amt"], r["currency"], dst, r["day"], exact)

    # ---- answers -----------------------------------------------------------------------------
    def total(self, rows, dst="EUR") -> Decimal:
        return sum((self.to(r, dst) for r in rows), Decimal(0))

    def where(self, **kw):
        return [r for r in self.invoices if all(r[k] == v for k, v in kw.items())]

    def top_vendors(self, n=5) -> list[tuple[str, Decimal]]:
        per = defaultdict(Decimal)
        for r in self.invoices:
            per[r["vendor_id"]] += self.to(r)
        return sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))[:n]

    def quarter_total(self, q: int) -> Decimal:
        return self.total([r for r in self.invoices if (r["day"].month - 1) // 3 + 1 == q])

    def most_invoices(self) -> tuple[str, int]:
        c = Counter(r["vendor_id"] for r in self.invoices)
        top = max(c.values())
        winners = sorted(v for v, k in c.items() if k == top)
        return winners[0], top

    def largest_invoice_eur(self) -> tuple[str, Decimal]:
        best = max(self.invoices, key=lambda r: self.to(r))
        return best["invoice_number"], self.to(best)

    def fd_without_approver(self) -> set[str]:
        return {r["invoice_number"] for r in self.invoices
                if not r["approved_by"] and self.to(r) > Decimal("50000")}

    def exact_date_unavailable(self) -> set[str]:
        return {r["invoice_number"] for r in self.invoices if self.to(r, exact=True) is None}


if __name__ == "__main__":
    ref = Reference()
    print("total EUR", ref.total(ref.invoices))
    print("total USD", ref.total(ref.invoices, "USD"))
    print("top5", ref.top_vendors())
    print("spain", ref.total([r for r in ref.invoices if r["country"] == "Spain"]))
    print("uk", ref.total([r for r in ref.invoices if r["country"] == "UK"]))
    print("on hold", ref.total(ref.where(status="ON_HOLD")), "inactive", ref.total(ref.where(status="INACTIVE")))
    print("Q1", ref.quarter_total(1), "most invoices", ref.most_invoices(), "largest", ref.largest_invoice_eur())
    print("missing po", sum(not r["po_number"] for r in ref.invoices), "missing approver",
          sum(not r["approved_by"] for r in ref.invoices), "credit notes", sum(r["amt"] < 0 for r in ref.invoices))
    print("FD no approver", sorted(ref.fd_without_approver()))
    print("exact unavailable", len(ref.exact_date_unavailable()))
    print("USD 60k 2025-03-08", ref.convert(Decimal("60000"), "USD", "EUR", date(2025, 3, 8)),
          ref.rate_date("USD", "EUR", date(2025, 3, 8)))
