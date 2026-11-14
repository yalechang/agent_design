"""Parse and validate an ECB Data Portal EXR series (SDMX ``csvdata`` format).

A response that fails any check raises ``RateValidationError`` and must never be used.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from app.fx.series import SeriesSpec

SERIES_START = date(1999, 1, 4)     # first ECB reference rate
MIN_ROWS = 100
REQUIRED_COLUMNS = ("KEY", "FREQ", "CURRENCY", "CURRENCY_DENOM", "TIME_PERIOD", "OBS_VALUE", "OBS_STATUS")
_VALUE_RE = re.compile(r"^\d+(\.\d+)?$")


class RateValidationError(Exception):
    def __init__(self, series: str, errors: list[str]):
        self.series = series
        self.errors = errors
        super().__init__(f"ECB {series} series failed validation: " + "; ".join(errors[:5]))


@dataclass
class ParsedSeries:
    series: str
    key: str
    title: str                                   # e.g. "US dollar/Euro ECB reference exchange rate"
    title_long: str | None
    observations: dict[date, Decimal | None]     # None = observation present but not usable (status != A)
    report: dict = field(default_factory=dict)

    @property
    def release_label(self) -> str | None:
        """Latest observation date, as the 'as of' label for this snapshot."""
        valid = [d for d, v in self.observations.items() if v is not None]
        return f"rates up to {max(valid).isoformat()}" if valid else None

    @property
    def data_sha256(self) -> str:
        """Hash of the parsed content only, stable across downloads with identical data."""
        lines = [self.series, self.key, self.title] + [
            f"{d.isoformat()}={'NA' if v is None else v}" for d, v in sorted(self.observations.items())]
        return hashlib.sha256("\n".join(lines).encode()).hexdigest()

    @property
    def first_date(self) -> date:
        return min(self.observations)

    @property
    def last_date(self) -> date:
        return max(self.observations)


def easter_sunday(year: int) -> date:
    """Gregorian Easter (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def target_holidays(year: int) -> set[date]:
    """TARGET closing days, on which the ECB publishes no reference rates."""
    easter = easter_sunday(year)
    return {date(year, 1, 1), easter - timedelta(days=2), easter + timedelta(days=1), date(year, 5, 1),
            date(year, 12, 25), date(year, 12, 26)}


def is_publication_day(d: date) -> bool:
    return d.weekday() < 5 and d not in target_holidays(d.year)


def parse_ecb_csv(text: str, spec: SeriesSpec, fetched_on: date,
                  required_coverage: tuple[date, date] | None = None) -> ParsedSeries:
    errors: list[str] = []
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    cols = reader.fieldnames or []
    missing_cols = [c for c in REQUIRED_COLUMNS if c not in cols]
    if missing_cols:
        raise RateValidationError(spec.code, [f"not an ECB EXR csvdata response (missing columns {missing_cols})"])
    rows = list(reader)
    if len(rows) < MIN_ROWS:
        errors.append(f"only {len(rows)} rows (expected >= {MIN_ROWS})")

    obs: dict[date, Decimal | None] = {}
    prev: date | None = None
    not_final = 0
    for i, r in enumerate(rows, start=2):
        if r["KEY"] != spec.key or r["FREQ"] != "D" or r["CURRENCY"] != spec.code or r["CURRENCY_DENOM"] != "EUR":
            errors.append(f"line {i}: series {r['KEY']!r} is not {spec.key}")
            continue
        try:
            d = date.fromisoformat(r["TIME_PERIOD"])
        except ValueError:
            errors.append(f"line {i}: unrecognised date {r['TIME_PERIOD']!r}")
            continue
        if d < SERIES_START or d > fetched_on:
            errors.append(f"date {d} outside {SERIES_START}..{fetched_on}")
        if d.weekday() >= 5:
            errors.append(f"date {d} is a weekend; ECB reference rates are not published then")
        if d in obs:
            errors.append(f"duplicate date {d}")
        if prev is not None and d <= prev:
            errors.append(f"dates not increasing at {d}")
        prev = d
        v = r["OBS_VALUE"].strip()
        if not _VALUE_RE.match(v) or len(v.split(".")[1] if "." in v else "") > spec.max_decimals:
            errors.append(f"invalid rate {v!r} on {d} (expected a number with <= {spec.max_decimals} decimals)")
            continue
        try:
            val = Decimal(v)
        except InvalidOperation:
            errors.append(f"invalid rate {v!r} on {d}")
            continue
        if not (spec.plausible_min <= val <= spec.plausible_max):
            errors.append(f"implausible rate {val} on {d} (band {spec.plausible_min}–{spec.plausible_max})")
        if r["OBS_STATUS"] != "A":   # A = normal value; anything else is not a final reference rate
            obs[d] = None
            not_final += 1
        else:
            obs[d] = val

    if required_coverage and obs:
        lo, hi = required_coverage
        gaps = [lo + timedelta(n) for n in range((hi - lo).days + 1)
                if is_publication_day(lo + timedelta(n)) and (lo + timedelta(n)) not in obs]
        if gaps:
            errors.append(f"{len(gaps)} ECB publication day(s) in required window {lo}..{hi} have no rate "
                          f"(first: {gaps[0]})")

    if errors:
        raise RateValidationError(spec.code, errors)

    first = rows[0]
    return ParsedSeries(
        series=spec.code,
        key=spec.key,
        title=first.get("TITLE") or spec.name,
        title_long=first.get("TITLE_COMPL"),
        observations=obs,
        report={
            "rows": len(rows),
            "valid_observations": len(obs) - not_final,
            "not_final_status": not_final,
            "first_date": min(obs).isoformat(),
            "last_date": max(obs).isoformat(),
            "required_coverage": [d.isoformat() for d in required_coverage] if required_coverage else None,
            "checks": ["csvdata_columns", "series_key", "min_rows", "iso_dates", "no_weekends",
                       "increasing_unique_dates", "numeric_max_decimals", "plausible_band", "obs_status"]
                      + (["publication_day_coverage"] if required_coverage else []),
        },
    )
