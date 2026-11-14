"""Attach application-computed reporting amounts and ECB rate references to every invoice.

``FxView.frame`` is the table GPT-generated code will query (M5). It adds:
    amount_<rep>          float of the rounded reporting amount; NaN when UNAVAILABLE
    fx_status             IDENTITY | EXACT | FALLBACK | UNAVAILABLE
    fx_observation_date   ECB reference-rate date actually used (NaT for identity / unavailable)
    fx_ref_id             key into ``FxView.refs`` (None for identity / unavailable)
Exact Decimal values and full evidence objects live alongside, keyed by invoice_number.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd

from app.data.store import DataStore
from app.fx.convert import (CURRENCIES, FxStatus, RateTable, convert, evidence, required_legs,
                            select_observation)
from app.fx.series import SERIES


@dataclass
class FxView:
    reporting_currency: str
    exact_date: bool
    fx_version: int
    frame: pd.DataFrame
    refs: pd.DataFrame
    amounts: dict[str, Decimal | None]          # rounded reporting amount per invoice
    unrounded: dict[str, Decimal | None]
    evidence: dict[str, dict]
    coverage: dict[str, int] = field(default_factory=dict)
    unavailable: list[str] = field(default_factory=list)

    @property
    def amount_column(self) -> str:
        return f"amount_{self.reporting_currency.lower()}"

    @property
    def complete(self) -> bool:
        return not self.unavailable


def build_fx_view(store: DataStore, rates: RateTable, reporting_currency: str = "EUR",
                  exact_date: bool = False, fx_version: int = 0) -> FxView:
    if reporting_currency not in CURRENCIES:
        raise ValueError(f"unsupported reporting currency {reporting_currency}")
    inv = store.enriched
    amounts: dict[str, Decimal | None] = {}
    unrounded: dict[str, Decimal | None] = {}
    evid: dict[str, dict] = {}
    statuses, obs_dates, ref_ids = [], [], []
    refs: dict[str, dict] = {}

    for n, d, cur in zip(inv["invoice_number"], inv["invoice_date"], inv["currency"]):
        legs = required_legs(cur, reporting_currency)
        sel = select_observation(rates, legs, d.date(), exact_date=exact_date)
        out = convert(store.amounts[n], cur, reporting_currency, sel)
        unrounded[n], amounts[n] = out if out else (None, None)
        evid[n] = evidence(sel, rates, cur, reporting_currency)
        statuses.append(sel.status.value)
        obs_dates.append(sel.observation_date)
        ref_ids.append(sel.ref_id)
        if sel.ref_id and sel.ref_id not in refs:
            row = {"fx_ref_id": sel.ref_id, "observation_date": sel.observation_date,
                   "legs": "+".join(sel.legs)}
            for code in SERIES:
                snap = rates.snapshots.get(code)
                row[f"{code.lower()}_per_eur"] = float(sel.values[code]) if code in sel.values else None
                row[f"{code.lower()}_source_url"] = SERIES[code].url if code in sel.legs else None
                row[f"{code.lower()}_snapshot_id"] = snap.snapshot_id if snap and code in sel.legs else None
            refs[sel.ref_id] = row

    col = f"amount_{reporting_currency.lower()}"
    frame = inv.copy()
    frame[col] = [float(amounts[n]) if amounts[n] is not None else float("nan") for n in inv["invoice_number"]]
    frame["fx_status"] = statuses
    frame["fx_observation_date"] = pd.to_datetime(pd.Series(obs_dates, index=frame.index, dtype="object"))
    frame["fx_ref_id"] = ref_ids

    ref_cols = ["fx_ref_id", "observation_date", "legs"] + [
        f"{c.lower()}{s}" for c in SERIES for s in ("_per_eur", "_source_url", "_snapshot_id")
    ]
    refs_df = pd.DataFrame(list(refs.values()), columns=ref_cols)
    if not refs_df.empty:
        refs_df["observation_date"] = pd.to_datetime(refs_df["observation_date"])
        refs_df = refs_df.sort_values("fx_ref_id").reset_index(drop=True)

    coverage = {s.value: 0 for s in FxStatus}
    for s in statuses:
        coverage[s] += 1
    unavailable = [n for n, s in zip(inv["invoice_number"], statuses) if s == FxStatus.UNAVAILABLE.value]

    return FxView(reporting_currency, exact_date, fx_version, frame, refs_df, amounts, unrounded, evid,
                  coverage, unavailable)


class FxViews:
    """Caches one FxView per (reporting currency, exact-date flag), rebuilt when FX snapshots change."""

    def __init__(self, store: DataStore, service):
        self.store = store
        self.service = service
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, bool], FxView] = {}

    def get(self, reporting_currency: str = "EUR", exact_date: bool = False) -> FxView:
        key = (reporting_currency, exact_date)
        rates, version = self.service.current()
        with self._lock:
            view = self._cache.get(key)
            if view is None or view.fx_version != version:
                view = build_fx_view(self.store, rates, reporting_currency, exact_date, version)
                self._cache[key] = view
            return view
