"""Independent reference computation of the /summary figures (ECB reference rates).

Shares no code with the app: stdlib csv + Decimal, its own reading of the ECB csvdata files and
its own fallback loop. Used by tests to cross-check the API to the cent.
"""
import csv
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, getcontext


def read_rates(path):
    """date -> units of currency per 1 EUR."""
    return {date.fromisoformat(r["TIME_PERIOD"]): Decimal(r["OBS_VALUE"])
            for r in csv.DictReader(open(path, encoding="utf-8")) if r["OBS_STATUS"] == "A"}


def to_eur(amount, cur, day, usd, gbp):
    if cur == "EUR":
        return amount
    series = usd if cur == "USD" else gbp
    for back in range(0, 8):
        v = series.get(day - timedelta(days=back))
        if v is not None:
            getcontext().prec = 28
            return (amount / v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return None


def reference(invoices_csv, vendors_csv, usd_csv, gbp_csv):
    usd, gbp = read_rates(usd_csv), read_rates(gbp_csv)
    vendors = {r["vendor_id"]: r for r in csv.DictReader(open(vendors_csv, encoding="utf-8"))}
    orig = defaultdict(Decimal)
    by_status = defaultdict(Decimal)
    by_vendor = defaultdict(Decimal)
    total = Decimal(0)
    for r in csv.DictReader(open(invoices_csv, encoding="utf-8")):
        amt = Decimal(r["amount"])
        orig[r["currency"]] += amt
        e = to_eur(amt, r["currency"], date.fromisoformat(r["invoice_date"]), usd, gbp)
        total += e
        by_status[vendors[r["vendor_id"]]["status"]] += e
        by_vendor[r["vendor_id"]] += e
    top = sorted(by_vendor.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    return {
        "original": {k: f"{v:.2f}" for k, v in orig.items()},
        "eur_total": f"{total:.2f}",
        "by_status": {k: f"{v:.2f}" for k, v in by_status.items()},
        "top5": [(vid, f"{v:.2f}") for vid, v in top],
    }
