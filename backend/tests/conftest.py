import shutil
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import REPO_ROOT, Settings
from app.fx.cache import Snapshot, build_snapshot
from app.fx.client import FetchResult
from app.fx.convert import RateTable
from app.fx.parser import ParsedSeries
from app.fx.series import SERIES

REAL_DATA = REPO_ROOT / "data"
FIXTURES = Path(__file__).parent / "fixtures" / "ecb"
FIXTURE_FILES = {s: FIXTURES / f"{spec.key}.csv" for s, spec in SERIES.items()}
FETCHED_AT = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
COVERAGE_2025 = (date(2024, 12, 27), date(2025, 12, 26))


@pytest.fixture
def data_copy(tmp_path: Path) -> Path:
    """A writable copy of the real data directory, for corruption tests."""
    dst = tmp_path / "data"
    shutil.copytree(REAL_DATA, dst)
    return dst


def make_settings(data_dir: Path, var_dir: Path, **kw) -> Settings:
    # _env_file=None: tests never read the developer's .env (or its API key).
    kw.setdefault("FX_REFRESH_ON_STARTUP", False)  # tests never hit the network
    return Settings(_env_file=None, DATA_DIR=data_dir, VAR_DIR=var_dir, **kw)


def fixture_bytes(series: str) -> bytes:
    return FIXTURE_FILES[series].read_bytes()


@pytest.fixture(scope="session")
def fixture_rates() -> RateTable:
    """Real ECB reference rates (Dec 2024 – Jan 2026) captured from the ECB Data Portal API."""
    return RateTable({s: build_snapshot(s, fixture_bytes(s), FETCHED_AT, 200, COVERAGE_2025) for s in SERIES})


class FakeFetcher:
    """Serves fixture bytes (or per-series overrides / exceptions) instead of the network."""

    def __init__(self, bodies: dict[str, bytes | Exception] | None = None):
        self.bodies = bodies or {s: fixture_bytes(s) for s in SERIES}
        self.calls: list[str] = []

    def __call__(self, url: str) -> FetchResult:
        self.calls.append(url)
        series = next(s for s, spec in SERIES.items() if spec.url == url)
        body = self.bodies[series]
        if isinstance(body, Exception):
            raise body
        return FetchResult(url, url, 200, FETCHED_AT, body)


def synthetic_rates(values: dict[str, dict[str, str | None]]) -> RateTable:
    """RateTable from literal values (units per EUR), bypassing the parser, e.g. {"USD": {"2025-03-07": "1.08"}}."""
    snaps = {}
    for s, obs in values.items():
        parsed = ParsedSeries(
            series=s, key=SERIES[s].key, title=f"synthetic {s}", title_long=None,
            observations={date.fromisoformat(d): (Decimal(v) if v is not None else None)
                          for d, v in sorted(obs.items())},
        )
        snaps[s] = Snapshot(f"synthetic_{s}", s, SERIES[s].url, FETCHED_AT, 200, "0" * 64, parsed)
    return RateTable(snaps)


CSV_HEADER = ("KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS,"
              "OBS_CONF,DECIMALS,TITLE,TITLE_COMPL,UNIT")


def ecb_csv(series: str, rows: list[tuple[str, str] | tuple[str, str, str]], key: str | None = None) -> str:
    """Minimal ECB csvdata response with the given (YYYY-MM-DD, value[, status]) rows."""
    spec = SERIES[series]
    key = key or spec.key
    cur = key.split(".")[2]
    lines = [CSV_HEADER]
    for row in rows:
        d, v, st = (row + ("A",))[:3]
        lines.append(f'{key},D,{cur},EUR,SP00,A,{d},{v},{st},F,{spec.max_decimals},{cur}/Euro ECB reference '
                     f'exchange rate,"ECB reference exchange rate, {cur}/Euro, 2.15 pm (C.E.T.)",{cur}')
    return "\n".join(lines) + "\n"
