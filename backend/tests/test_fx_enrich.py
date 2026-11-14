import math
from decimal import Decimal

import pandas as pd
import pytest

from app.data.store import load_datastore
from app.fx.convert import RateTable
from app.fx.enrich import build_fx_view
from tests.conftest import REAL_DATA


@pytest.fixture(scope="module")
def store():
    return load_datastore(REAL_DATA / "invoices_2025_clean.csv", REAL_DATA / "vendor_master.csv")


@pytest.fixture(scope="module")
def eur(store, fixture_rates):
    return build_fx_view(store, fixture_rates, "EUR")


def row(view, n):
    return view.frame.set_index("invoice_number").loc[n]


def test_every_invoice_gets_a_status(eur):
    assert sum(eur.coverage.values()) == 140
    assert eur.coverage["IDENTITY"] == 89
    assert eur.coverage["UNAVAILABLE"] == 0 and eur.complete
    assert eur.frame["amount_eur"].notna().all()


def test_weekend_non_eur_invoices_fall_back(eur):
    f = eur.frame
    weekend = f["invoice_date"].dt.day_of_week >= 5
    non_eur = f["currency"] != "EUR"
    assert (f.loc[weekend & non_eur, "fx_status"] == "FALLBACK").all()
    assert (f.loc[weekend & non_eur, "fx_observation_date"] < f.loc[weekend & non_eur, "invoice_date"]).all()
    assert (f.loc[~non_eur, "fx_status"] == "IDENTITY").all()
    assert f.loc[~non_eur, "fx_observation_date"].isna().all()
    assert (eur.coverage["EXACT"] + eur.coverage["FALLBACK"]) == 51


def test_known_invoices(eur):
    assert eur.amounts["INV-20001"] == Decimal("21344.84")            # EUR: identity
    assert eur.amounts["INV-20002"] == Decimal("25071.57")            # GBP 21,082.18 ÷ 0.84088 (2025-03-07)
    assert eur.amounts["INV-20003"] == Decimal("96816.15")            # GBP 80,539.42 ÷ 0.83188 (2025-02-04)
    r = row(eur, "INV-20002")
    assert (r.fx_status, r.fx_ref_id, r.amount_eur) == ("EXACT", "2025-03-07|GBP", 25071.57)
    assert eur.evidence["INV-20002"]["legs"][0]["value"] == "0.84088"


def test_weekday_holiday_falls_back(eur):
    r = row(eur, "INV-20133")                              # USD, Good Friday 2025-04-18: TARGET holiday
    assert r.fx_status == "FALLBACK"
    assert r.fx_observation_date == pd.Timestamp("2025-04-17")
    assert eur.evidence["INV-20133"]["fallback_days"] == 1
    assert row(eur, "INV-20092").fx_status == "EXACT"     # US holiday 2025-02-17: the ECB publishes


def test_float_column_matches_decimal(eur):
    for n, a in zip(eur.frame["invoice_number"], eur.frame["amount_eur"]):
        assert Decimal(str(a)) == eur.amounts[n]


def test_refs_table(eur):
    refs = eur.refs
    assert set(eur.frame["fx_ref_id"].dropna()) == set(refs["fx_ref_id"])
    gbp = refs[refs["legs"] == "GBP"].iloc[0]
    assert gbp["gbp_per_eur"] > 0 and gbp["gbp_source_url"].startswith("https://data-api.ecb.europa.eu/")
    assert math.isnan(gbp["usd_per_eur"]) or gbp["usd_per_eur"] is None   # GBP→EUR needs only the GBP series
    usd = refs[refs["legs"] == "USD"].iloc[0]
    assert usd["usd_per_eur"] > 0
    assert str(refs["observation_date"].dtype).startswith("datetime64")


def test_exact_date_view_marks_weekends_unavailable(store, fixture_rates):
    v = build_fx_view(store, fixture_rates, "EUR", exact_date=True)
    f = v.frame
    wk = (f["invoice_date"].dt.day_of_week >= 5) & (f["currency"] != "EUR")
    holiday = f["invoice_number"] == "INV-20133"          # Good Friday 2025-04-18, TARGET holiday
    assert (f.loc[wk | holiday, "fx_status"] == "UNAVAILABLE").all()
    assert f.loc[wk | holiday, "amount_eur"].isna().all()
    assert set(v.unavailable) == set(f.loc[wk | holiday, "invoice_number"]) and not v.complete
    assert all(v.evidence[n]["reason"] == "EXACT_DATE_NOT_AVAILABLE" for n in v.unavailable)


def test_no_rates_keeps_eur_rows_only(store):
    v = build_fx_view(store, RateTable({}), "EUR")
    assert v.coverage == {"IDENTITY": 89, "EXACT": 0, "FALLBACK": 0, "UNAVAILABLE": 51}
    assert v.frame.loc[v.frame["currency"] != "EUR", "amount_eur"].isna().all()
    assert v.refs.empty and not v.complete


def test_usd_reporting(store, fixture_rates):
    v = build_fx_view(store, fixture_rates, "USD")
    assert "amount_usd" in v.frame.columns
    assert v.coverage["IDENTITY"] == 24
    f = v.frame.set_index("invoice_number")
    assert f.loc["INV-20007", "amount_usd"] == 85311.57
