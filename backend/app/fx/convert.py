"""Deterministic currency conversion with ECB euro reference rates.

Every ECB series quotes units of the foreign currency per 1 EUR, so with
X(EUR) = 1, X(USD) = USD per EUR, X(GBP) = GBP per EUR, all on the same observation date d:

    amount_dst = amount_src × X(dst) / X(src)

e.g. USD→EUR = amount ÷ (USD per EUR);  GBP→EUR = amount ÷ (GBP per EUR);
     EUR→USD = amount × (USD per EUR);  GBP→USD = amount × (USD per EUR) ÷ (GBP per EUR).

Arithmetic is ``Decimal`` with 28 significant digits and is done in one expression (a cross
rate is never rounded on its own). Each converted amount is rounded once, to 0.01 with
ROUND_HALF_UP. Aggregates are sums of the rounded per-invoice amounts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, localcontext
from enum import Enum

from app.fx.cache import Snapshot
from app.fx.parser import is_publication_day
from app.fx.series import CONVENTION_NOTE, POLICY_NOTE, SERIES

CURRENCIES = ("EUR", "GBP", "USD")
MAX_FALLBACK_DAYS = 7
CENT = Decimal("0.01")
PRECISION = 28

METHOD_NOTE = (
    "amount × (reporting-currency units per EUR) ÷ (source-currency units per EUR), using ECB reference "
    "rates from one observation date (EUR counts as 1); Decimal arithmetic at 28 significant digits with no "
    "intermediate rounding; result rounded to 0.01 with ROUND_HALF_UP; totals are sums of rounded invoice "
    "amounts."
)


class FxStatus(str, Enum):
    IDENTITY = "IDENTITY"         # same currency, no rate needed
    EXACT = "EXACT"               # rates observed on the invoice date
    FALLBACK = "FALLBACK"         # earlier observation used under the 7-day convention
    UNAVAILABLE = "UNAVAILABLE"   # no acceptable rate — needs review


USABLE = frozenset({FxStatus.IDENTITY, FxStatus.EXACT, FxStatus.FALLBACK})


class Reason(str, Enum):
    NO_SNAPSHOT = "NO_SNAPSHOT"                                 # series not loaded at all
    AFTER_SNAPSHOT_COVERAGE = "AFTER_SNAPSHOT_COVERAGE"         # date later than the snapshot's last row
    BEFORE_SERIES_START = "BEFORE_SERIES_START"
    EXACT_DATE_NOT_AVAILABLE = "EXACT_DATE_NOT_AVAILABLE"       # exact-date requested; no obs that day
    NO_OBSERVATION_WITHIN_7_DAYS = "NO_OBSERVATION_WITHIN_7_DAYS"


def required_legs(src: str, dst: str) -> tuple[str, ...]:
    if src not in CURRENCIES or dst not in CURRENCIES:
        raise ValueError(f"unsupported currency pair {src}->{dst}")
    if src == dst:
        return ()
    return tuple(sorted({c for c in (src, dst) if c != "EUR"}))


class RateTable:
    """Read-only view over the active snapshots (one per series)."""

    def __init__(self, snapshots: dict[str, Snapshot]):
        self.snapshots = dict(snapshots)

    def has(self, series: str) -> bool:
        return series in self.snapshots

    def value(self, series: str, d: date) -> Decimal | None:
        snap = self.snapshots.get(series)
        return snap.observations.get(d) if snap else None

    def last_date(self, series: str) -> date:
        return self.snapshots[series].parsed.last_date

    def first_date(self, series: str) -> date:
        return self.snapshots[series].parsed.first_date


@dataclass(frozen=True)
class Selection:
    status: FxStatus
    requested_date: date
    legs: tuple[str, ...]
    exact_date_requested: bool
    observation_date: date | None = None
    values: dict[str, Decimal] = field(default_factory=dict)
    reason: Reason | None = None
    missing: tuple[str, ...] = ()        # human-readable description of the missing evidence

    @property
    def fallback_days(self) -> int | None:
        if self.observation_date is None:
            return None
        return (self.requested_date - self.observation_date).days

    @property
    def ref_id(self) -> str | None:
        if self.observation_date is None or not self.legs:
            return None
        return f"{self.observation_date.isoformat()}|{'+'.join(self.legs)}"


def select_observation(rates: RateTable, legs: tuple[str, ...], requested: date,
                       exact_date: bool = False, max_fallback_days: int = MAX_FALLBACK_DAYS) -> Selection:
    base = dict(requested_date=requested, legs=legs, exact_date_requested=exact_date)
    if not legs:
        return Selection(FxStatus.IDENTITY, **base)

    absent = [s for s in legs if not rates.has(s)]
    if absent:
        return Selection(FxStatus.UNAVAILABLE, reason=Reason.NO_SNAPSHOT,
                         missing=tuple(f"no validated ECB {s} reference-rate snapshot is loaded" for s in absent), **base)

    beyond = [s for s in legs if requested > rates.last_date(s)]
    if beyond:
        return Selection(FxStatus.UNAVAILABLE, reason=Reason.AFTER_SNAPSHOT_COVERAGE,
                         missing=tuple(f"ECB {s} snapshot ends {rates.last_date(s)}, before {requested}"
                                       for s in beyond), **base)

    def values_on(d: date) -> dict[str, Decimal] | None:
        vals = {s: rates.value(s, d) for s in legs}
        return vals if all(v is not None for v in vals.values()) else None  # type: ignore[return-value]

    exact = values_on(requested)
    if exact:
        return Selection(FxStatus.EXACT, observation_date=requested, values=exact, **base)

    if exact_date:
        why = "" if is_publication_day(requested) else " (weekend or TARGET holiday: no ECB publication)"
        missing = tuple(f"ECB {s} has no reference rate on {requested}{why}" for s in legs
                        if rates.value(s, requested) is None)
        return Selection(FxStatus.UNAVAILABLE, reason=Reason.EXACT_DATE_NOT_AVAILABLE, missing=missing, **base)

    # Latest earlier date (never later) where every required leg is valid on that same date.
    for back in range(1, max_fallback_days + 1):
        d = requested - timedelta(days=back)
        if any(d < rates.first_date(s) for s in legs):
            return Selection(FxStatus.UNAVAILABLE, reason=Reason.BEFORE_SERIES_START,
                             missing=(f"no ECB reference rates before {requested}",), **base)
        vals = values_on(d)
        if vals:
            return Selection(FxStatus.FALLBACK, observation_date=d, values=vals, **base)

    lo = requested - timedelta(days=max_fallback_days)
    return Selection(FxStatus.UNAVAILABLE, reason=Reason.NO_OBSERVATION_WITHIN_7_DAYS,
                     missing=(f"no date between {lo} and {requested} has ECB reference rates for "
                              f"{' and '.join(legs)}",), **base)


def per_eur(currency: str, values: dict[str, Decimal]) -> Decimal:
    """Units of ``currency`` per 1 EUR (1 for EUR itself)."""
    return Decimal(1) if currency == "EUR" else values[currency]


def convert(amount: Decimal, src: str, dst: str, sel: Selection) -> tuple[Decimal, Decimal] | None:
    """Return (unrounded, rounded-to-cent) amount in ``dst``, or None if the selection is unusable."""
    if sel.status == FxStatus.IDENTITY:
        return amount, amount.quantize(CENT, rounding=ROUND_HALF_UP)
    if sel.status not in USABLE:
        return None
    with localcontext() as ctx:
        ctx.prec = PRECISION
        ctx.rounding = ROUND_HALF_EVEN
        unrounded = amount * per_eur(dst, sel.values) / per_eur(src, sel.values)
        rounded = unrounded.quantize(CENT, rounding=ROUND_HALF_UP)
    return unrounded, rounded


def rate_formula(src: str, dst: str) -> str:
    if src == dst:
        return "identity (no conversion)"
    if src == "EUR":
        return f"EUR→{dst} = amount × {dst} per EUR"
    if dst == "EUR":
        return f"{src}→EUR = amount ÷ {src} per EUR"
    return f"{src}→{dst} = amount × {dst} per EUR ÷ {src} per EUR"


def evidence(sel: Selection, rates: RateTable, src: str, dst: str) -> dict:
    """Citation for one conversion: ECB series, URL, dates, values, method, snapshot."""
    legs = []
    for s in sel.legs:
        snap = rates.snapshots.get(s)
        legs.append({
            "series": SERIES[s].name,
            "currency": s,
            "url": SERIES[s].url,
            "value": str(sel.values[s]) if s in sel.values else None,
            "series_key": SERIES[s].key,
            "series_page": SERIES[s].page,
            "quote": f"{s} per 1 EUR",
            "snapshot_id": snap.snapshot_id if snap else None,
            "snapshot_fetched_at": snap.fetched_at.isoformat() if snap else None,
            "release": snap.parsed.release_label if snap else None,
        })
    return {
        "fx_ref_id": sel.ref_id,
        "status": sel.status.value,
        "from_currency": src,
        "to_currency": dst,
        "requested_date": sel.requested_date.isoformat(),
        "observation_date": sel.observation_date.isoformat() if sel.observation_date else None,
        "fallback_days": sel.fallback_days if sel.status == FxStatus.FALLBACK else 0,
        "exact_date_requested": sel.exact_date_requested,
        "legs": legs,
        "formula": rate_formula(src, dst),
        "method": METHOD_NOTE if sel.legs else "identity (no conversion)",
        "reason": sel.reason.value if sel.reason else None,
        "missing_evidence": list(sel.missing),
        "convention_note": CONVENTION_NOTE if sel.status == FxStatus.FALLBACK else None,
        "policy_note": POLICY_NOTE if sel.legs else None,
    }
