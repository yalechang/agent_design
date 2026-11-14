from decimal import Decimal
from datetime import date

import pytest

from app.data.store import DataValidationError, load_datastore
from tests.conftest import REAL_DATA


def load(d):
    return load_datastore(d / "invoices_2025_clean.csv", d / "vendor_master.csv")


@pytest.fixture(scope="module")
def store():
    return load(REAL_DATA)


def test_loads_real_data(store):
    assert len(store.invoices) == 140
    assert len(store.vendors) == 10
    assert store.date_range == (date(2025, 1, 3), date(2025, 12, 26))
    assert store.invoices["currency"].value_counts().to_dict() == {"EUR": 89, "GBP": 27, "USD": 24}
    assert set(store.file_sha256) == {"invoices_2025_clean.csv", "vendor_master.csv"}


def test_amounts_are_exact_decimals(store):
    assert store.amounts["INV-20001"] == Decimal("21344.84")
    assert all(isinstance(v, Decimal) for v in store.amounts.values())
    assert all(-v.as_tuple().exponent <= 2 for v in store.amounts.values())


def test_enriched_join(store):
    e = store.enriched
    assert len(e) == 140
    assert e["vendor_country"].notna().all()
    row = e.set_index("invoice_number").loc["INV-20002"]
    assert (row.vendor_country, row.vendor_status) == ("UK", "ON_HOLD")
    assert e["is_credit_note"].sum() == 2
    assert str(e["invoice_date"].dtype).startswith("datetime64")


def test_quality_notes(store):
    notes = {n.code: n for n in store.notes}
    assert notes["MISSING_PO"].count == 21
    assert notes["MISSING_APPROVER"].count == 25
    assert notes["CREDIT_NOTE"].count == 2
    non_active = store.enriched["vendor_status"] != "ACTIVE"
    assert notes["NON_ACTIVE_VENDOR"].count == int(non_active.sum())
    assert "VENDOR_NAME_MISMATCH" not in notes


def _edit_invoices(d, old, new):
    p = d / "invoices_2025_clean.csv"
    text = p.read_text()
    assert old in text
    p.write_text(text.replace(old, new, 1))


@pytest.mark.parametrize("old,new,expect", [
    ("INV-20002,", "INV-20001,", "duplicate invoice_number"),
    ("2025-03-09", "09/03/2025", "invalid invoice_date"),
    ("2025-03-09", "2025-02-30", "invalid invoice_date"),
    ("21344.84", "21344.845", "invalid amount"),
    ("21344.84", "abc", "invalid amount"),
    ("21344.84,EUR", "21344.84,CHF", "invalid currency"),
    ("INV-20001,2025-03-09,V-1002", "INV-20001,2025-03-09,V-9999", "not in vendor master"),
    ("INV-20001,2025-03-09,V-1002", "INV-20001,2025-03-09,", "column 'vendor_id' has 1 empty"),
])
def test_invoice_validation_errors(data_copy, old, new, expect):
    _edit_invoices(data_copy, old, new)
    with pytest.raises(DataValidationError) as exc:
        load(data_copy)
    assert expect in str(exc.value)


def test_missing_column(data_copy):
    _edit_invoices(data_copy, "approved_by", "approver")
    with pytest.raises(DataValidationError, match="missing columns"):
        load(data_copy)


def test_vendor_validation(data_copy):
    p = data_copy / "vendor_master.csv"
    p.write_text(p.read_text().replace("ON_HOLD", "SUSPENDED"))
    with pytest.raises(DataValidationError, match="invalid status"):
        load(data_copy)


def test_missing_file(tmp_path):
    with pytest.raises(DataValidationError, match="file not found"):
        load(tmp_path)


def test_name_mismatch_is_soft(data_copy):
    _edit_invoices(data_copy, "ClinSupply Iberia S.L.", "ClinSupply Iberia SL")
    notes = {n.code: n for n in load(data_copy).notes}
    assert notes["VENDOR_NAME_MISMATCH"].count == 1
