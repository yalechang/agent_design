from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.fx.parser import RateValidationError, easter_sunday, is_publication_day, parse_ecb_csv, target_holidays
from app.fx.series import SERIES
from tests.conftest import COVERAGE_2025, FIXTURE_FILES, ecb_csv

TODAY = date(2026, 11, 13)
GOOD = [(f"2025-03-{d:02d}", "1.0800") for d in (3, 4, 5, 6, 7)]


@pytest.fixture(autouse=True)
def small_min_rows(monkeypatch):
    monkeypatch.setattr("app.fx.parser.MIN_ROWS", 3)


def parse(text, series="USD", coverage=None):
    return parse_ecb_csv(text, SERIES[series], TODAY, coverage)


@pytest.mark.parametrize("series", ["USD", "GBP"])
def test_parses_real_fixture(series):
    p = parse(FIXTURE_FILES[series].read_text(), series, COVERAGE_2025)
    assert p.key == SERIES[series].key
    assert "ECB reference exchange rate" in p.title
    assert date(2025, 1, 1) not in p.observations         # TARGET holiday: no publication
    assert date(2025, 12, 26) not in p.observations
    assert date(2025, 3, 8) not in p.observations          # Saturday
    assert date(2025, 2, 17) in p.observations             # US holiday, but the ECB publishes
    assert all(isinstance(v, Decimal) for v in p.observations.values())
    assert p.release_label == "rates up to 2026-01-30"


def test_known_values():
    usd = parse(FIXTURE_FILES["USD"].read_text(), "USD")
    gbp = parse(FIXTURE_FILES["GBP"].read_text(), "GBP")
    assert usd.observations[date(2025, 3, 7)] == Decimal("1.0857")
    assert gbp.observations[date(2025, 3, 7)] == Decimal("0.84088")      # 5 decimals for GBP


def test_target_holidays_2025():
    assert easter_sunday(2025) == date(2025, 4, 20)
    assert sorted(target_holidays(2025)) == [date(2025, 1, 1), date(2025, 4, 18), date(2025, 4, 21),
                                             date(2025, 5, 1), date(2025, 12, 25), date(2025, 12, 26)]
    assert not is_publication_day(date(2025, 3, 8)) and is_publication_day(date(2025, 2, 17))


@pytest.mark.parametrize("year,expected", [(2024, date(2024, 3, 31)), (2026, date(2026, 4, 5)), (2000, date(2000, 4, 23))])
def test_easter(year, expected):
    assert easter_sunday(year) == expected


@pytest.mark.parametrize("rows,expect", [
    (GOOD[:2] + [("2025-03-05", "abc")] + GOOD[3:], "invalid rate"),
    (GOOD[:2] + [("2025-03-05", "1.08001")] + GOOD[3:], "invalid rate"),       # > 4 decimals for USD
    (GOOD[:2] + [("2025-03-05", "-1.08")] + GOOD[3:], "invalid rate"),
    (GOOD[:2] + [("2025-03-05", "3.5000")] + GOOD[3:], "implausible rate"),
    (GOOD + [("2025-03-07", "1.0800")], "duplicate date"),
    ([GOOD[1], GOOD[0]] + GOOD[2:], "dates not increasing"),
    (GOOD + [("2026-12-01", "1.0800")], "outside"),                            # future date
    (GOOD + [("2025-03-08", "1.0800")], "is a weekend"),
    (GOOD[:2] + [("05/03/2025", "1.08")] + GOOD[3:], "unrecognised date"),
    (GOOD[:2], "only 2 rows"),
])
def test_validation_errors(rows, expect):
    with pytest.raises(RateValidationError) as exc:
        parse(ecb_csv("USD", rows))
    assert any(expect in e for e in exc.value.errors), exc.value.errors


def test_wrong_series_key():
    with pytest.raises(RateValidationError, match="is not EXR.D.USD.EUR.SP00.A"):
        parse(ecb_csv("USD", GOOD, key="EXR.D.JPY.EUR.SP00.A"))


def test_usd_fixture_rejected_as_gbp():
    with pytest.raises(RateValidationError):
        parse(FIXTURE_FILES["USD"].read_text(), "GBP")


def test_not_csvdata():
    with pytest.raises(RateValidationError, match="not an ECB EXR csvdata response"):
        parse("<html><body><p>Service unavailable</p></body></html>")


def test_gap_on_publication_day_detected():
    rows = [("2025-03-03", "1.08"), ("2025-03-04", "1.08"), ("2025-03-06", "1.08"), ("2025-03-07", "1.08")]
    with pytest.raises(RateValidationError, match="publication day"):
        parse(ecb_csv("USD", rows), coverage=(date(2025, 3, 3), date(2025, 3, 7)))


def test_target_holiday_gap_is_expected():
    # Good Friday and Easter Monday 2025 have no rates; that is not a gap.
    days = [date(2025, 4, 14) + timedelta(n) for n in range(9)]
    rows = [(d.isoformat(), "1.13") for d in days if is_publication_day(d)]
    p = parse(ecb_csv("USD", rows), coverage=(date(2025, 4, 14), date(2025, 4, 22)))
    assert date(2025, 4, 18) not in p.observations and date(2025, 4, 22) in p.observations


def test_non_final_status_is_unusable():
    rows = GOOD[:2] + [("2025-03-05", "1.08", "P")] + GOOD[3:]
    p = parse(ecb_csv("USD", rows))
    assert p.observations[date(2025, 3, 5)] is None and p.report["not_final_status"] == 1


def test_data_hash_stable_across_byte_changes():
    a = parse(ecb_csv("USD", GOOD))
    b = parse(ecb_csv("USD", GOOD).replace("2.15 pm", "2:15 pm"))
    assert a.data_sha256 == b.data_sha256
