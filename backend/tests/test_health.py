import pytest
from fastapi.testclient import TestClient

from app.data.store import DataValidationError
from app.main import create_app
from tests.conftest import REAL_DATA, make_settings


def test_health_without_key(tmp_path):
    app = create_app(make_settings(REAL_DATA, tmp_path / "var"))
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["data"]["status"] == "ok"
    assert body["data"]["invoice_count"] == 140
    assert body["policy"]["clause_count"] == 21
    assert body["policy"]["document"] == "FIN-POL-014 v3.2"
    assert body["llm"]["status"] == "not_configured"
    assert body["fx"]["status"] == "unavailable"   # no cache, no network in tests
    assert body["status"] == "degraded"
    assert "sk-" not in str(body)


def test_health_with_key_does_not_leak_it(tmp_path):
    app = create_app(make_settings(REAL_DATA, tmp_path / "var", OPENAI_API_KEY="sk-test-secret"))
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["llm"]["status"] == "ok"
    assert "sk-test-secret" not in str(body)


def test_app_refuses_to_start_on_bad_data(data_copy, tmp_path):
    (data_copy / "invoices_2025_clean.csv").write_text("invoice_number\nINV-1\n")
    app = create_app(make_settings(data_copy, tmp_path / "var"))
    with pytest.raises(DataValidationError):
        with TestClient(app):
            pass


from app.fx.cache import SnapshotCache
from app.fx.service import FxService
from tests.conftest import COVERAGE_2025, FakeFetcher


def _app_with_fx(tmp_path, **kw):
    settings = make_settings(REAL_DATA, tmp_path / "var", OPENAI_API_KEY="sk-x", **kw)
    fx = FxService(SnapshotCache(settings.fx_cache_dir), COVERAGE_2025, FakeFetcher())
    return create_app(settings, fx_service=fx), fx


def test_fx_refresh_endpoint_and_status(tmp_path):
    app, fx = _app_with_fx(tmp_path)
    with TestClient(app) as client:
        assert client.get("/fx/status").json()["eur_invoice_coverage"]["counts"]["UNAVAILABLE"] == 51
        body = client.post("/fx/refresh").json()
        assert body["available"]
        cov = body["eur_invoice_coverage"]
        assert cov["complete"] and cov["counts"] == {"IDENTITY": 89, "EXACT": 35, "FALLBACK": 16, "UNAVAILABLE": 0}
        assert body["series"]["USD"]["release"] == "rates up to 2026-01-30"
        health = client.get("/health").json()
        assert health["fx"]["status"] == "ok" and health["status"] == "ok"


def test_fx_loaded_from_cache_on_startup(tmp_path):
    app, fx = _app_with_fx(tmp_path)
    fx.refresh()   # populate cache on disk
    app2, _ = _app_with_fx(tmp_path)   # new service, same cache dir
    with TestClient(app2) as client:
        assert client.get("/fx/status").json()["available"]


def test_refresh_disabled_outside_dev(tmp_path):
    app, _ = _app_with_fx(tmp_path, DEV_MODE=False)
    with TestClient(app) as client:
        assert client.post("/fx/refresh").status_code == 404
