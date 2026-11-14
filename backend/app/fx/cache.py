"""Immutable on-disk snapshots of the ECB reference-rate series.

Layout (per series):
    var/fx_cache/ecb/<SERIES>/<snapshot_id>.csv    raw API response exactly as fetched
    var/fx_cache/ecb/<SERIES>/<snapshot_id>.json   metadata + validation report
    var/fx_cache/ecb/active.json                    {"USD": "<snapshot_id>", "GBP": "..."}

snapshot_id = <fetched_at UTC, YYYYMMDDTHHMMSSZ>_<sha256(raw)[:12]>

Snapshots are re-parsed and re-validated from the raw response on load, and the stored sha256
is checked, so a tampered or corrupted file is rejected rather than trusted.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from app.fx.parser import ParsedSeries, parse_ecb_csv
from app.fx.series import SERIES


class SnapshotError(Exception):
    pass


@dataclass
class Snapshot:
    snapshot_id: str
    series: str
    source_url: str
    fetched_at: datetime
    http_status: int
    raw_sha256: str
    parsed: ParsedSeries

    @property
    def observations(self) -> dict[date, Decimal | None]:
        return self.parsed.observations

    def metadata(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "series": self.series,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at.isoformat(),
            "http_status": self.http_status,
            "raw_sha256": self.raw_sha256,
            "data_sha256": self.parsed.data_sha256,
            "series_key": self.parsed.key,
            "title": self.parsed.title,
            "title_long": self.parsed.title_long,
            "release_label": self.parsed.release_label,
            "validation": self.parsed.report,
        }


def make_snapshot_id(fetched_at: datetime, raw: bytes) -> str:
    return f"{fetched_at.strftime('%Y%m%dT%H%M%SZ')}_{hashlib.sha256(raw).hexdigest()[:12]}"


def build_snapshot(series: str, raw: bytes, fetched_at: datetime, http_status: int = 200,
                   required_coverage: tuple[date, date] | None = None) -> Snapshot:
    """Parse + validate a raw ECB response into a Snapshot (raises RateValidationError)."""
    spec = SERIES[series]
    parsed = parse_ecb_csv(raw.decode("utf-8", errors="strict"), spec, fetched_at.date(), required_coverage)
    return Snapshot(
        snapshot_id=make_snapshot_id(fetched_at, raw),
        series=series,
        source_url=spec.url,
        fetched_at=fetched_at,
        http_status=http_status,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        parsed=parsed,
    )


class SnapshotCache:
    def __init__(self, root: Path):
        self.root = root

    def _dir(self, series: str) -> Path:
        return self.root / series

    @property
    def _active_path(self) -> Path:
        return self.root / "active.json"

    def save(self, snap: Snapshot, raw: bytes) -> None:
        d = self._dir(snap.series)
        d.mkdir(parents=True, exist_ok=True)
        raw_path, meta_path = d / f"{snap.snapshot_id}.csv", d / f"{snap.snapshot_id}.json"
        if not raw_path.exists():
            _atomic_write(raw_path, raw)
        if not meta_path.exists():
            _atomic_write(meta_path, json.dumps(snap.metadata(), indent=2).encode())

    def find_by_data_sha(self, series: str, data_sha256: str) -> str | None:
        """Id of an existing snapshot with identical parsed rates, if any."""
        d = self._dir(series)
        if not d.exists():
            return None
        for meta in sorted(d.glob("*.json")):
            try:
                if json.loads(meta.read_text()).get("data_sha256") == data_sha256:
                    return meta.stem
            except (OSError, json.JSONDecodeError):
                continue
        return None

    def active_ids(self) -> dict[str, str]:
        if not self._active_path.exists():
            return {}
        try:
            data = json.loads(self._active_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return {k: v for k, v in data.items() if k in SERIES and isinstance(v, str)}

    def set_active(self, series: str, snapshot_id: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        ids = self.active_ids()
        ids[series] = snapshot_id
        _atomic_write(self._active_path, json.dumps(ids, indent=2).encode())

    def load(self, series: str, snapshot_id: str,
             required_coverage: tuple[date, date] | None = None) -> Snapshot:
        d = self._dir(series)
        raw_path, meta_path = d / f"{snapshot_id}.csv", d / f"{snapshot_id}.json"
        if not raw_path.exists() or not meta_path.exists():
            raise SnapshotError(f"snapshot {series}/{snapshot_id} is missing from the cache")
        raw = raw_path.read_bytes()
        meta = json.loads(meta_path.read_text())
        sha = hashlib.sha256(raw).hexdigest()
        if sha != meta.get("raw_sha256") or not snapshot_id.endswith(sha[:12]):
            raise SnapshotError(f"snapshot {series}/{snapshot_id} failed its integrity check")
        snap = build_snapshot(series, raw, datetime.fromisoformat(meta["fetched_at"]),
                              meta.get("http_status", 200), required_coverage)
        snap.snapshot_id = snapshot_id
        return snap


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
