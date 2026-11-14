"""The only authorized FX evidence: ECB euro foreign exchange reference rates.

Source: ECB Data Portal, dataset EXR (daily reference rates, published around 16:00 CET on
TARGET business days). Each series quotes UNITS OF THE FOREIGN CURRENCY PER 1 EUR.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ECB_DATA_PORTAL = "https://data.ecb.europa.eu/data/datasets/EXR"
API_BASE = "https://data-api.ecb.europa.eu/service/data/EXR"
HISTORY_START = "2024-01-01"     # fetched window start; covers all 2025 invoices plus fallback days


@dataclass(frozen=True)
class SeriesSpec:
    code: str                 # foreign currency the series quotes against EUR ("USD" / "GBP")
    key: str                  # SDMX series key, e.g. EXR.D.USD.EUR.SP00.A
    url: str                  # exact API URL fetched (CSV)
    page: str                 # human-readable series page on the ECB Data Portal
    name: str                 # label used in evidence
    max_decimals: int         # ECB publishes USD with 4 and GBP with 5 decimals
    plausible_min: Decimal    # sanity band for units per EUR (whole 1999+ history fits)
    plausible_max: Decimal


def _spec(code: str, label: str, decimals: int, lo: str, hi: str) -> SeriesSpec:
    key = f"EXR.D.{code}.EUR.SP00.A"
    return SeriesSpec(
        code=code,
        key=key,
        url=f"{API_BASE}/D.{code}.EUR.SP00.A?startPeriod={HISTORY_START}&format=csvdata",
        page=f"{ECB_DATA_PORTAL}/{key}",
        name=f"ECB euro reference exchange rate — {label} per euro ({key})",
        max_decimals=decimals,
        plausible_min=Decimal(lo),
        plausible_max=Decimal(hi),
    )


SERIES: dict[str, SeriesSpec] = {
    "USD": _spec("USD", "US dollar", 4, "0.8", "1.7"),
    "GBP": _spec("GBP", "pound sterling", 5, "0.55", "1.0"),
}

ALLOWED_URLS = frozenset(s.url for s in SERIES.values())

POLICY_NOTE = (
    "Converted at ECB euro foreign exchange reference rates, the rate source that procurement policy "
    "FIN-POL-014 §5.1 requires for reporting non-EUR invoices in EUR."
)

CONVENTION_NOTE = (
    "Prototype convention (not an ECB or policy rule): the ECB publishes no reference rate on weekends or "
    "TARGET holidays. For such invoice dates the latest earlier ECB reference rate is used, at most 7 calendar "
    "days before the invoice date, with the same date for every currency involved. Policy §5.1 asks for the "
    "rate on the invoice date and does not say how to handle these dates, so these conversions should be "
    "reviewed. Future rates are never used; an explicit exact-date request disables this fallback."
)
