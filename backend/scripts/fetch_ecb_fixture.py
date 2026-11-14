"""Refresh the offline ECB test fixtures (a fixed window of the two EXR series).

Usage (from backend/): .venv/bin/python scripts/fetch_ecb_fixture.py
"""
from pathlib import Path

import httpx

from app.fx.series import API_BASE, SERIES

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "ecb"
WINDOW = "startPeriod=2024-12-01&endPeriod=2026-01-31&format=csvdata"

OUT.mkdir(parents=True, exist_ok=True)
for code, spec in SERIES.items():
    url = f"{API_BASE}/D.{code}.EUR.SP00.A?{WINDOW}"
    r = httpx.get(url, timeout=30, headers={"Accept": "text/csv"})
    r.raise_for_status()
    (OUT / f"{spec.key}.csv").write_bytes(r.content)
    print(f"{spec.key}: {len(r.content)} bytes")
