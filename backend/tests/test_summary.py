from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.data.store import load_datastore
from app.fx.cache import SnapshotCache, build_snapshot
from app.fx.convert import RateTable
from app.fx.enrich import build_fx_view
from app.fx.series import SERIES
from app.fx.service import FxService
from app.main import create_app
from app.api.schemas import FxSeriesStatus
from app.policy.clauses import load_policy
from app.summary import build_summary
from tests.conftest import (COVERAGE_2025, FETCHED_AT, FIXTURE_FILES, REAL_DATA, FakeFetcher, fixture_bytes,
                            make_settings, synthetic_rates)
from tests.reference_summary import reference

INV, VEN = REAL_DATA / "invoices_2025_clean.csv", REAL_DATA / "vendor_master.csv"


def client_for(tmp_path, fetcher=None, refresh=True):
    settings = make_settings(REAL_DATA, tmp_path / "var")
    fx = FxService(SnapshotCache(settings.fx_cache_dir), COVERAGE_2025, fetcher or FakeFetcher())
    if refresh:
        fx.refresh()
    return TestClient(create_app(settings, fx_service=fx)), fx


@pytest.fixture(scope="module")
def ref():
    return reference(INV, VEN, FIXTURE_FILES["USD"], FIXTURE_FILES["GBP"])


@pytest.fixture
def body(tmp_path):
    c, _ = client_for(tmp_path)
    with c:
        return c.get("/summary").json()


# ---- matches the independent reference -------------------------------------------------

def test_original_currency_totals_match_reference(body, ref):
    got = {t["currency"]: t["amount"] for t in body["original_currency_totals"]}
    assert got == ref["original"]
    assert {t["currency"]: t["invoice_count"] for t in body["original_currency_totals"]} == \
        {"EUR": 89, "GBP": 27, "USD": 24}
    assert sum(t["credit_note_count"] for t in body["original_currency_totals"]) == 2


def test_consolidated_eur_matches_reference(body, ref):
    c = body["consolidated"]
    assert c["available"] and c["currency"] == "EUR"
    assert c["total"] == ref["eur_total"] == "6886631.10"
    assert c["invoice_count"] == 140 and c["missing"] == []
    lines = {l["currency"]: l for l in c["by_currency"]}
    assert lines["EUR"]["converted_amount"] == lines["EUR"]["original_amount"] == ref["original"]["EUR"]
    assert sum(Decimal(l["converted_amount"]) for l in c["by_currency"]) == Decimal(c["total"])


def test_status_breakdown_matches_reference(body, ref):
    got = {s["status"]: s for s in body["by_vendor_status"]}
    assert [s["status"] for s in body["by_vendor_status"]] == ["ACTIVE", "ON_HOLD", "INACTIVE"]
    assert {k: v["eur_total"] for k, v in got.items()} == ref["by_status"]
    assert {k: (v["vendor_count"], v["invoice_count"]) for k, v in got.items()} == \
        {"ACTIVE": (8, 111), "ON_HOLD": (1, 12), "INACTIVE": (1, 17)}


def test_top_vendors_match_reference(body, ref):
    assert [(v["vendor_id"], v["eur_total"]) for v in body["top_vendors_eur"]] == ref["top5"]
    assert [v["rank"] for v in body["top_vendors_eur"]] == [1, 2, 3, 4, 5]
    top = body["top_vendors_eur"][0]
    assert sum(t["invoice_count"] for t in top["original_currency_totals"]) == top["invoice_count"]


def test_fx_evidence_is_cited(body):
    ev = body["consolidated"]["evidence"]
    assert {s["currency"] for s in ev["sources"]} == {"USD", "GBP"}
    assert all(s["url"] == SERIES[s["currency"]].url and s["snapshot_id"] for s in ev["sources"])
    assert ev["coverage"] == {"IDENTITY": 89, "EXACT": 35, "FALLBACK": 16, "UNAVAILABLE": 0}
    assert len(ev["fallback_invoices"]) == 16 and "INV-20133" in ev["fallback_invoices"]
    assert ev["max_fallback_days"] == 2      # Sunday -> Friday; no weekday holiday needs more
    assert ev["formula"] == ["GBP→EUR = amount ÷ GBP per EUR", "USD→EUR = amount ÷ USD per EUR"]
    assert "ECB" in ev["policy_note"] and "§5.1 requires" in ev["policy_note"]
    assert "Prototype convention" in ev["convention_note"]


def test_data_quality_and_dataset(body):
    dq = {d["code"]: d["count"] for d in body["data_quality"]}
    assert dq["MISSING_PO"] == 21 and dq["MISSING_APPROVER"] == 25 and dq["CREDIT_NOTE"] == 2
    assert body["dataset"]["date_range"] == ["2025-01-03", "2025-12-26"]
    assert body["dataset"]["policy_document"] == "FIN-POL-014 v3.2"


def test_money_is_serialised_as_strings(body):
    assert isinstance(body["consolidated"]["total"], str)
    assert all(isinstance(t["amount"], str) for t in body["original_currency_totals"])


# ---- FX unavailable: originals usable, EUR clearly unavailable, never partial ---------------

def test_fx_down_entirely(tmp_path, ref):
    c, _ = client_for(tmp_path, FakeFetcher({s: OSError("offline") for s in SERIES}))
    with c:
        b = c.get("/summary").json()
    assert {t["currency"]: t["amount"] for t in b["original_currency_totals"]} == ref["original"]
    con = b["consolidated"]
    assert con["available"] is False and con["total"] is None and con["evidence"] is None
    assert con["by_currency"] == []
    assert "No validated ECB reference-rate snapshot" in con["unavailable_reason"]
    assert len(con["missing"]) == 51 and {m["currency"] for m in con["missing"]} == {"GBP", "USD"}
    assert con["recommendation"].startswith("Review")
    assert b["top_vendors_eur"] is None and b["top_vendors_unavailable_reason"]
    assert all(s["eur_total"] is None and not s["eur_available"] for s in b["by_vendor_status"])
    assert all(s["original_currency_totals"] for s in b["by_vendor_status"])


def test_one_series_down(tmp_path):
    c, _ = client_for(tmp_path, FakeFetcher({"USD": fixture_bytes("USD"), "GBP": OSError("offline")}))
    with c:
        con = c.get("/summary").json()["consolidated"]
    assert con["total"] is None
    assert {m["currency"] for m in con["missing"]} == {"GBP"} and len(con["missing"]) == 27
    assert "GBP" in con["unavailable_reason"]


def test_single_gap_only_nulls_affected_group():
    """Remove 8 days of rates before one ON_HOLD non-EUR invoice: only that group loses its EUR total."""
    store = load_datastore(INV, VEN)
    snaps = {s: build_snapshot(s, fixture_bytes(s), FETCHED_AT, 200, COVERAGE_2025) for s in SERIES}
    f = store.enriched
    target = f[(f["vendor_status"] == "ON_HOLD") & (f["currency"] == "USD")].iloc[0]
    day = target["invoice_date"].date()
    gap = {day - timedelta(days=k) for k in range(0, 8)}
    others = f[(f["currency"] != "EUR") & (f["invoice_number"] != target["invoice_number"])]
    assert not any(d.date() in gap for d in others["invoice_date"]), "gap would hit other invoices"
    rates = synthetic_rates({s: {d.isoformat(): (None if d in gap else (str(v) if v is not None else None))
                                 for d, v in snaps[s].observations.items() if d.year >= 2024}
                             for s in SERIES})
    view = build_fx_view(store, rates, "EUR")
    status = {s: FxSeriesStatus(state="ok", source_url=SERIES[s].url) for s in SERIES}
    out = build_summary(store, load_policy(REAL_DATA / "procurement_policy.md"), view, status)
    assert not out.consolidated.available and out.consolidated.total is None
    assert [m.invoice_number for m in out.consolidated.missing] == [target["invoice_number"]]
    assert out.consolidated.missing[0].reason == "NO_OBSERVATION_WITHIN_7_DAYS"
    groups = {g.status: g for g in out.by_vendor_status}
    assert groups["ON_HOLD"].eur_total is None
    assert groups["ACTIVE"].eur_total is not None and groups["INACTIVE"].eur_total is not None
    assert out.top_vendors_eur is None


def test_summary_recomputed_when_fx_arrives(tmp_path, ref):
    c, fx = client_for(tmp_path, refresh=False)
    with c:
        assert c.get("/summary").json()["consolidated"]["available"] is False
        first = c.get("/summary").json()["generated_at"]
        assert c.get("/summary").json()["generated_at"] == first       # cached
        fx.refresh()
        b = c.get("/summary").json()
    assert b["consolidated"]["total"] == ref["eur_total"]
