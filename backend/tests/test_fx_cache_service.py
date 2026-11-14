import json
from datetime import timedelta

import httpx
import pytest

from app.fx.cache import SnapshotCache, SnapshotError
from app.fx.client import RateFetchError, fetch
from app.fx.series import SERIES
from app.fx.service import FxService
from tests.conftest import COVERAGE_2025, FakeFetcher, fixture_bytes


def service(tmp_path, fetcher=None):
    return FxService(SnapshotCache(tmp_path / "fx_cache"), COVERAGE_2025, fetcher or FakeFetcher())


def test_refresh_saves_and_activates(tmp_path):
    fx = service(tmp_path)
    st = fx.refresh()
    assert st["available"] and fx.version == 1
    for code in SERIES:
        sid = st["series"][code]["snapshot_id"]
        d = tmp_path / "fx_cache" / code
        assert (d / f"{sid}.csv").read_bytes() == fixture_bytes(code)   # raw response kept for audit
        meta = json.loads((d / f"{sid}.json").read_text())
        assert meta["source_url"] == SERIES[code].url and meta["series_key"] == SERIES[code].key
        assert meta["release_label"] == "rates up to 2026-01-30"
        assert sid.startswith("20261113T120000Z_") and sid.endswith(meta["raw_sha256"][:12])
    assert json.loads((tmp_path / "fx_cache" / "active.json").read_text()).keys() == {"USD", "GBP"}


def test_identical_response_reuses_snapshot(tmp_path):
    fx = service(tmp_path)
    first = fx.refresh()["series"]["USD"]["snapshot_id"]
    assert fx.refresh()["series"]["USD"]["snapshot_id"] == first and fx.version == 1
    assert len(list((tmp_path / "fx_cache" / "USD").glob("*.csv"))) == 1


def test_same_rates_different_bytes_reuse_snapshot(tmp_path):
    base = fixture_bytes("USD")
    fx = service(tmp_path)
    first = fx.refresh()["series"]["USD"]["snapshot_id"]
    fx.fetcher = FakeFetcher({"USD": base.replace(b"2.15 pm", b"2:15 pm"), "GBP": fixture_bytes("GBP")})
    assert fx.refresh()["series"]["USD"]["snapshot_id"] == first
    assert len(list((tmp_path / "fx_cache" / "USD").glob("*.csv"))) == 1 and fx.version == 1


def test_changed_rate_creates_new_snapshot(tmp_path):
    fx = service(tmp_path)
    first = fx.refresh()["series"]["USD"]["snapshot_id"]
    fx.fetcher = FakeFetcher({"USD": fixture_bytes("USD").replace(b",1.0857,", b",1.0858,"),
                              "GBP": fixture_bytes("GBP")})
    assert fx.refresh()["series"]["USD"]["snapshot_id"] != first and fx.version == 2


def test_cache_reload_in_new_process(tmp_path):
    service(tmp_path).refresh()
    fx2 = service(tmp_path, FakeFetcher({s: AssertionError("must not fetch") for s in SERIES}))
    fx2.load_cached()
    assert fx2.status()["available"] and fx2.missing_series() == []


def test_tampered_cache_is_rejected(tmp_path):
    sid = service(tmp_path).refresh()["series"]["GBP"]["snapshot_id"]
    raw = tmp_path / "fx_cache" / "GBP" / f"{sid}.csv"
    raw.write_bytes(raw.read_bytes().replace(b"0.84088", b"0.99999"))
    fx2 = service(tmp_path)
    fx2.load_cached()
    st = fx2.status()
    assert st["series"]["GBP"]["state"] == "unavailable" and "integrity" in st["series"]["GBP"]["last_error"]
    assert st["series"]["USD"]["state"] == "ok"
    with pytest.raises(SnapshotError):
        SnapshotCache(tmp_path / "fx_cache").load("GBP", sid)


def test_failed_refresh_keeps_previous_snapshot(tmp_path):
    fx = service(tmp_path)
    good = fx.refresh()["series"]["USD"]["snapshot_id"]
    fx.fetcher = FakeFetcher({"USD": RateFetchError("timeout"), "GBP": b"<html>maintenance</html>"})
    st = fx.refresh()
    assert st["available"] and st["series"]["USD"]["snapshot_id"] == good
    assert "timeout" in st["series"]["USD"]["last_error"]
    assert "validation" in st["series"]["GBP"]["last_error"]


def test_no_cache_and_fetch_fails_means_unavailable(tmp_path):
    fx = service(tmp_path, FakeFetcher({s: RateFetchError("offline") for s in SERIES}))
    fx.load_cached()
    st = fx.refresh()
    assert not st["available"] and fx.missing_series() == ["USD", "GBP"]


def test_invalid_response_never_saved(tmp_path):
    fx = service(tmp_path, FakeFetcher({"USD": fixture_bytes("GBP"), "GBP": fixture_bytes("GBP")}))
    fx.refresh()
    assert fx.status()["series"]["USD"]["state"] == "unavailable"
    assert not (tmp_path / "fx_cache" / "USD").exists()


def test_needs_refresh(tmp_path):
    fx = service(tmp_path)
    assert fx.needs_refresh(timedelta(hours=24))
    fx.refresh()
    assert fx.needs_refresh(timedelta(seconds=1))
    assert not fx.needs_refresh(timedelta(days=36500))


# ---- HTTP client ----------------------------------------------------------------------------

def test_client_allow_list():
    with pytest.raises(RateFetchError, match="allow-list"):
        fetch("https://example.com/rates.csv")
    with pytest.raises(RateFetchError, match="allow-list"):
        fetch("https://www.federalreserve.gov/releases/h10/hist/dat00_eu.htm")   # no longer authorized


def test_client_success_and_retry():
    calls = []

    def handler(request):
        calls.append(request.headers.get("accept"))
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, content=b"ok")

    res = fetch(SERIES["USD"].url, transport=httpx.MockTransport(handler), backoff=0)
    assert res.body == b"ok" and res.status_code == 200 and calls == ["text/csv", "text/csv"]


def test_client_gives_up_after_retries():
    with pytest.raises(RateFetchError, match="HTTP 500"):
        fetch(SERIES["USD"].url, transport=httpx.MockTransport(lambda r: httpx.Response(500)), backoff=0, retries=1)


def test_client_rejects_offsite_redirect():
    def handler(request):
        if request.url.host == "data-api.ecb.europa.eu":
            return httpx.Response(302, headers={"Location": "https://evil.example/x"})
        return httpx.Response(200, content=b"fake")

    with pytest.raises(RateFetchError, match="redirected"):
        fetch(SERIES["GBP"].url, transport=httpx.MockTransport(handler), backoff=0)
