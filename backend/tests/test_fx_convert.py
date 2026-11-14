from datetime import date
from decimal import Decimal

import pytest

from app.fx.convert import (FxStatus, RateTable, Reason, convert, evidence, rate_formula, required_legs,
                            select_observation)
from tests.conftest import synthetic_rates

D = date.fromisoformat


@pytest.mark.parametrize("src,dst,legs", [
    ("EUR", "EUR", ()), ("USD", "EUR", ("USD",)), ("GBP", "EUR", ("GBP",)), ("EUR", "USD", ("USD",)),
    ("GBP", "USD", ("GBP", "USD")), ("USD", "GBP", ("GBP", "USD")), ("USD", "USD", ()),
])
def test_required_legs(src, dst, legs):
    assert required_legs(src, dst) == legs


def test_required_legs_rejects_unknown():
    with pytest.raises(ValueError):
        required_legs("CHF", "EUR")


# ---- observation selection on real ECB fixture rows -----------------------------------------

@pytest.mark.parametrize("requested,obs,status,days", [
    ("2025-03-07", "2025-03-07", FxStatus.EXACT, 0),      # Friday
    ("2025-03-08", "2025-03-07", FxStatus.FALLBACK, 1),   # Saturday -> Friday
    ("2025-03-09", "2025-03-07", FxStatus.FALLBACK, 2),   # Sunday -> Friday (never Monday)
    ("2025-04-18", "2025-04-17", FxStatus.FALLBACK, 1),   # Good Friday (TARGET holiday)
    ("2025-04-21", "2025-04-17", FxStatus.FALLBACK, 4),   # Easter Monday -> Thursday before Easter
    ("2025-12-26", "2025-12-24", FxStatus.FALLBACK, 2),   # 25 and 26 Dec are both TARGET holidays
    ("2025-01-01", "2024-12-31", FxStatus.FALLBACK, 1),
    ("2025-02-17", "2025-02-17", FxStatus.EXACT, 0),      # US holiday; the ECB publishes
])
def test_selection_real_dates(fixture_rates, requested, obs, status, days):
    sel = select_observation(fixture_rates, ("GBP", "USD"), D(requested))
    assert sel.status == status
    assert sel.observation_date == D(obs)
    assert sel.fallback_days == days
    assert set(sel.values) == {"GBP", "USD"}


def test_exact_date_disables_fallback(fixture_rates):
    sel = select_observation(fixture_rates, ("USD",), D("2025-03-08"), exact_date=True)
    assert sel.status == FxStatus.UNAVAILABLE and sel.reason == Reason.EXACT_DATE_NOT_AVAILABLE
    assert sel.missing == ("ECB USD has no reference rate on 2025-03-08 (weekend or TARGET holiday: no ECB publication)",)
    assert select_observation(fixture_rates, ("USD",), D("2025-03-07"), exact_date=True).status == FxStatus.EXACT


def test_identity_needs_no_rates():
    sel = select_observation(RateTable({}), (), D("2025-03-08"))
    assert sel.status == FxStatus.IDENTITY and sel.ref_id is None


def test_cross_rate_uses_same_date_for_both_legs():
    # Monday: USD published but GBP not usable -> GBP->USD falls back to Friday where BOTH exist,
    # while USD->EUR (one leg) can use Monday.
    rates = synthetic_rates({
        "USD": {"2025-03-07": "1.0857", "2025-03-10": "1.0833"},
        "GBP": {"2025-03-07": "0.84088", "2025-03-10": None},
    })
    cross = select_observation(rates, ("GBP", "USD"), D("2025-03-10"))
    assert (cross.status, cross.observation_date) == (FxStatus.FALLBACK, D("2025-03-07"))
    assert cross.values == {"USD": Decimal("1.0857"), "GBP": Decimal("0.84088")}
    single = select_observation(rates, ("USD",), D("2025-03-10"))
    assert (single.status, single.observation_date) == (FxStatus.EXACT, D("2025-03-10"))


def test_no_observation_within_7_days():
    rates = synthetic_rates({"USD": {"2025-03-01": "1.08", "2025-03-20": "1.09"}})
    sel = select_observation(rates, ("USD",), D("2025-03-10"))
    assert sel.status == FxStatus.UNAVAILABLE and sel.reason == Reason.NO_OBSERVATION_WITHIN_7_DAYS
    assert "2025-03-03" in sel.missing[0]


def test_exactly_7_days_back_is_allowed():
    rates = synthetic_rates({"USD": {"2025-03-03": "1.08", "2025-03-20": "1.09"}})
    sel = select_observation(rates, ("USD",), D("2025-03-10"))
    assert (sel.status, sel.fallback_days) == (FxStatus.FALLBACK, 7)


def test_never_uses_future_observation():
    rates = synthetic_rates({"USD": {"2025-02-20": "1.05", "2025-03-10": "1.0833"}})
    assert select_observation(rates, ("USD",), D("2025-03-08")).status == FxStatus.UNAVAILABLE


def test_after_snapshot_coverage():
    rates = synthetic_rates({"USD": {"2025-03-07": "1.0857"}})
    assert select_observation(rates, ("USD",), D("2025-03-08")).reason == Reason.AFTER_SNAPSHOT_COVERAGE


def test_missing_snapshot():
    rates = synthetic_rates({"USD": {"2025-03-07": "1.0857"}})
    sel = select_observation(rates, ("GBP", "USD"), D("2025-03-07"))
    assert sel.reason == Reason.NO_SNAPSHOT and "GBP" in sel.missing[0]


# ---- Decimal arithmetic -----------------------------------------------------------------------

def test_gbp_to_eur_real_invoice(fixture_rates):
    # INV-20002: GBP 21,082.18 on 2025-03-07; ECB 0.84088 GBP per EUR
    sel = select_observation(fixture_rates, ("GBP",), D("2025-03-07"))
    unrounded, rounded = convert(Decimal("21082.18"), "GBP", "EUR", sel)
    assert unrounded == Decimal("21082.18") / Decimal("0.84088")
    assert rounded == Decimal("25071.57")


def test_gbp_to_usd_cross_rate_not_prerounded(fixture_rates):
    sel = select_observation(fixture_rates, ("GBP", "USD"), D("2025-03-07"))
    _, rounded = convert(Decimal("80539.42"), "GBP", "USD", sel)
    exact = (Decimal("80539.42") * Decimal("1.0857") / Decimal("0.84088")).quantize(Decimal("0.01"))
    prerounded = (Decimal("80539.42") * (Decimal("1.0857") / Decimal("0.84088")).quantize(Decimal("0.0001"))
                  ).quantize(Decimal("0.01"))
    assert rounded == exact and rounded != prerounded


@pytest.mark.parametrize("amount,src,dst,expected", [
    ("100.00", "USD", "EUR", "50.00"),     # ÷ USD per EUR
    ("100.00", "EUR", "USD", "200.00"),    # × USD per EUR
    ("100.00", "GBP", "EUR", "125.00"),    # ÷ GBP per EUR
    ("100.00", "EUR", "GBP", "80.00"),     # × GBP per EUR
    ("100.00", "GBP", "USD", "250.00"),    # × USD per EUR ÷ GBP per EUR
    ("100.00", "USD", "GBP", "40.00"),
    ("-52.50", "GBP", "EUR", "-65.63"),    # credit note; half-up rounds away from zero
])
def test_formula_directions(amount, src, dst, expected):
    rates = synthetic_rates({"USD": {"2025-03-07": "2"}, "GBP": {"2025-03-07": "0.8"}})
    sel = select_observation(rates, required_legs(src, dst), D("2025-03-07"))
    assert convert(Decimal(amount), src, dst, sel)[1] == Decimal(expected)


def test_round_half_up_not_bankers():
    rates = synthetic_rates({"USD": {"2025-03-07": "2"}})
    sel = select_observation(rates, ("USD",), D("2025-03-07"))
    assert convert(Decimal("0.05"), "USD", "EUR", sel) == (Decimal("0.025"), Decimal("0.03"))


def test_convert_unavailable_returns_none():
    sel = select_observation(RateTable({}), ("USD",), D("2025-03-07"))
    assert convert(Decimal("1"), "USD", "EUR", sel) is None


def test_identity_conversion_exact():
    sel = select_observation(RateTable({}), (), D("2025-03-07"))
    assert convert(Decimal("21344.84"), "EUR", "EUR", sel) == (Decimal("21344.84"), Decimal("21344.84"))


@pytest.mark.parametrize("src,dst,text", [
    ("GBP", "EUR", "GBP→EUR = amount ÷ GBP per EUR"),
    ("USD", "EUR", "USD→EUR = amount ÷ USD per EUR"),
    ("EUR", "USD", "EUR→USD = amount × USD per EUR"),
    ("GBP", "USD", "GBP→USD = amount × USD per EUR ÷ GBP per EUR"),
])
def test_rate_formula_text(src, dst, text):
    assert rate_formula(src, dst) == text


def test_evidence_has_required_citation_fields(fixture_rates):
    sel = select_observation(fixture_rates, ("GBP",), D("2025-03-09"))
    ev = evidence(sel, fixture_rates, "GBP", "EUR")
    assert ev["requested_date"] == "2025-03-09" and ev["observation_date"] == "2025-03-07"
    assert ev["status"] == "FALLBACK" and ev["fallback_days"] == 2
    assert ev["fx_ref_id"] == "2025-03-07|GBP"
    leg = ev["legs"][0]
    assert (leg["currency"], leg["value"], leg["quote"]) == ("GBP", "0.84088", "GBP per 1 EUR")
    assert leg["series_key"] == "EXR.D.GBP.EUR.SP00.A"
    assert leg["url"].startswith("https://data-api.ecb.europa.eu/service/data/EXR/D.GBP.EUR.SP00.A")
    assert leg["series_page"] == "https://data.ecb.europa.eu/data/datasets/EXR/EXR.D.GBP.EUR.SP00.A"
    assert leg["snapshot_id"]
    assert "ROUND_HALF_UP" in ev["method"]
    assert "ECB" in ev["policy_note"] and "§5.1" in ev["policy_note"]
    assert "Prototype convention" in ev["convention_note"] and "TARGET" in ev["convention_note"]


def test_evidence_unavailable_lists_missing():
    sel = select_observation(RateTable({}), ("USD",), D("2025-03-07"))
    ev = evidence(sel, RateTable({}), "USD", "EUR")
    assert ev["status"] == "UNAVAILABLE" and ev["reason"] == "NO_SNAPSHOT"
    assert ev["missing_evidence"] and ev["legs"][0]["value"] is None
