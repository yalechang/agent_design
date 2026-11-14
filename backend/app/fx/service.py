"""Owns the active ECB reference-rate snapshots: load from cache, refresh from the web, report status.

Readers take an immutable ``RateTable`` via ``rates()``; a refresh swaps in a new table and bumps
``version`` so dependent views (converted invoice columns, summary) are rebuilt.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from app.fx.cache import Snapshot, SnapshotCache, build_snapshot
from app.fx.convert import RateTable
from app.fx.client import FetchResult, fetch
from app.fx.series import SERIES

log = logging.getLogger("app.fx")

Fetcher = Callable[[str], FetchResult]


class RefreshInProgress(Exception):
    pass


class FxService:
    def __init__(self, cache: SnapshotCache, required_coverage: tuple[date, date] | None = None,
                 fetcher: Fetcher = fetch):
        self.cache = cache
        self.required_coverage = required_coverage
        self.fetcher = fetcher
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._snapshots: dict[str, Snapshot] = {}
        self._errors: dict[str, str] = {}
        self._last_refresh: datetime | None = None
        self.refreshing = False
        self.version = 0

    # ---- reading -------------------------------------------------------------
    def rates(self) -> RateTable:
        return RateTable(self._snapshots)

    def current(self) -> tuple[RateTable, int]:
        """Rates and the version they belong to, read together."""
        with self._lock:
            return RateTable(self._snapshots), self.version

    def status(self) -> dict:
        series = {}
        for code, spec in SERIES.items():
            snap = self._snapshots.get(code)
            series[code] = {
                "state": "ok" if snap else "unavailable",
                "source_url": spec.url,
                "snapshot_id": snap.snapshot_id if snap else None,
                "fetched_at": snap.fetched_at.isoformat() if snap else None,
                "release": snap.parsed.release_label if snap else None,
                "last_observation": snap.parsed.last_date.isoformat() if snap else None,
                "last_error": self._errors.get(code),
            }
        return {
            "available": all(s["state"] == "ok" for s in series.values()),
            "refreshing": self.refreshing,
            "last_refresh_attempt": self._last_refresh.isoformat() if self._last_refresh else None,
            "version": self.version,
            "series": series,
        }

    # ---- loading -------------------------------------------------------------
    def load_cached(self) -> None:
        active = self.cache.active_ids()
        loaded: dict[str, Snapshot] = {}
        for code in SERIES:
            sid = active.get(code)
            if not sid:
                continue
            try:
                loaded[code] = self.cache.load(code, sid, self.required_coverage)
            except Exception as e:  # corrupted / invalid cache entry: never used
                self._errors[code] = f"cached snapshot {sid} rejected: {e}"
                log.warning("FX cache %s/%s rejected: %s", code, sid, e)
        with self._lock:
            self._snapshots.update(loaded)
            if loaded:
                self.version += 1

    def missing_series(self) -> list[str]:
        return [c for c in SERIES if c not in self._snapshots]

    def needs_refresh(self, max_age: timedelta) -> bool:
        """True if any series is missing or its active snapshot is older than ``max_age``."""
        now = datetime.now(timezone.utc)
        return bool(self.missing_series()) or any(
            now - s.fetched_at > max_age for s in self._snapshots.values())

    def refresh(self) -> dict:
        """Fetch + validate both series. A failed series keeps its previous snapshot."""
        if not self._refresh_lock.acquire(blocking=False):
            raise RefreshInProgress("an ECB rate refresh is already running")
        self.refreshing = True
        self._last_refresh = datetime.now(timezone.utc)
        changed = False
        try:
            for code, spec in SERIES.items():
                try:
                    res = self.fetcher(spec.url)
                    snap = build_snapshot(code, res.body, res.fetched_at, res.status_code, self.required_coverage)
                    existing = self.cache.find_by_data_sha(code, snap.parsed.data_sha256)
                    if existing:
                        # Same rates as a cached snapshot: keep citing that one, store nothing new.
                        snap = self.cache.load(code, existing, self.required_coverage)
                    else:
                        self.cache.save(snap, res.body)
                    self.cache.set_active(code, snap.snapshot_id)
                    with self._lock:
                        prev = self._snapshots.get(code)
                        if prev is None or prev.snapshot_id != snap.snapshot_id:
                            self._snapshots[code] = snap
                            changed = True
                    self._errors.pop(code, None)
                except Exception as e:
                    self._errors[code] = f"refresh failed: {e}"
                    log.warning("ECB %s refresh failed: %s", code, e)
            if changed:
                with self._lock:
                    self.version += 1
        finally:
            self.refreshing = False
            self._refresh_lock.release()
        return self.status()
