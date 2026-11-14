"""Load, validate and enrich the invoice and vendor CSVs.

Hard validation failures raise ``DataValidationError`` so the app refuses to start on bad data.
Soft findings (missing PO / approver, credit notes, ...) are recorded as data-quality notes.

Money is kept exact: every ``amount`` is parsed from its original string into ``Decimal``
(``DataStore.amounts``). The float ``amount`` column exists only for pandas convenience.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

INVOICE_COLUMNS = [
    "invoice_number", "invoice_date", "vendor_id", "vendor_name", "description",
    "amount", "currency", "cost_center", "po_number", "approved_by",
]
VENDOR_COLUMNS = ["vendor_id", "vendor_name", "country", "category", "status"]

INVOICE_REQUIRED = [c for c in INVOICE_COLUMNS if c not in ("po_number", "approved_by")]
VENDOR_REQUIRED = VENDOR_COLUMNS

ALLOWED_CURRENCIES = frozenset({"EUR", "GBP", "USD"})
ALLOWED_STATUSES = frozenset({"ACTIVE", "INACTIVE", "ON_HOLD"})

_AMOUNT_RE = re.compile(r"^-?\d+(\.\d{1,2})?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DataValidationError(Exception):
    def __init__(self, source: str, errors: list[str]):
        self.source = source
        self.errors = errors
        super().__init__(f"{source}: " + "; ".join(errors[:10]) + (" ..." if len(errors) > 10 else ""))


@dataclass
class DataQualityNote:
    code: str
    message: str
    count: int
    invoice_numbers: list[str] = field(default_factory=list)


@dataclass
class DataStore:
    invoices: pd.DataFrame            # invoice columns; invoice_date is datetime64, amount is float
    vendors: pd.DataFrame
    enriched: pd.DataFrame            # invoices joined with vendor attributes (FX columns added in M2)
    amounts: dict[str, Decimal]       # invoice_number -> exact original-currency amount
    file_sha256: dict[str, str]
    notes: list[DataQualityNote]

    @property
    def date_range(self) -> tuple[date, date]:
        d = self.invoices["invoice_date"]
        return d.min().date(), d.max().date()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_csv(path: Path, columns: list[str], required: list[str]) -> pd.DataFrame:
    if not path.exists():
        raise DataValidationError(path.name, [f"file not found: {path}"])
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise DataValidationError(path.name, [f"missing columns: {missing}"])
    df = df[columns].apply(lambda s: s.str.strip())
    # Empty strings -> missing; only optional columns may contain them.
    df = df.replace("", pd.NA)
    errors = [
        f"column '{c}' has {int(df[c].isna().sum())} empty value(s) (rows {_rows(df[c].isna())})"
        for c in required if df[c].isna().any()
    ]
    if errors:
        raise DataValidationError(path.name, errors)
    return df


def _rows(mask: pd.Series, limit: int = 5) -> str:
    # CSV line numbers (header is line 1)
    idx = [str(i + 2) for i in mask[mask].index[:limit]]
    return ", ".join(idx) + (" ..." if int(mask.sum()) > limit else "")


def _validate_vendors(v: pd.DataFrame, name: str) -> None:
    errors = []
    dup = v["vendor_id"].duplicated(keep=False)
    if dup.any():
        errors.append(f"duplicate vendor_id: {sorted(v.loc[dup, 'vendor_id'].unique())}")
    bad = ~v["status"].isin(ALLOWED_STATUSES)
    if bad.any():
        errors.append(f"invalid status {sorted(v.loc[bad, 'status'].unique())} (rows {_rows(bad)})")
    if errors:
        raise DataValidationError(name, errors)


def _validate_invoices(inv: pd.DataFrame, vendors: pd.DataFrame, name: str) -> None:
    errors = []
    dup = inv["invoice_number"].duplicated(keep=False)
    if dup.any():
        errors.append(f"duplicate invoice_number: {sorted(inv.loc[dup, 'invoice_number'].unique())}")

    def bad_date(s: str) -> bool:
        if not _DATE_RE.match(s):
            return True
        try:
            date.fromisoformat(s)
        except ValueError:
            return True
        return False

    bd = inv["invoice_date"].map(bad_date).astype(bool)
    if bd.any():
        errors.append(f"invalid invoice_date (expected YYYY-MM-DD) at rows {_rows(bd)}")

    ba = ~inv["amount"].map(lambda s: bool(_AMOUNT_RE.match(s))).astype(bool)
    if ba.any():
        errors.append(f"invalid amount (expected number with <= 2 decimals) at rows {_rows(ba)}")

    bc = ~inv["currency"].isin(ALLOWED_CURRENCIES)
    if bc.any():
        errors.append(f"invalid currency {sorted(inv.loc[bc, 'currency'].unique())} at rows {_rows(bc)}")

    bv = ~inv["vendor_id"].isin(set(vendors["vendor_id"]))
    if bv.any():
        errors.append(f"vendor_id not in vendor master: {sorted(inv.loc[bv, 'vendor_id'].unique())}")

    if errors:
        raise DataValidationError(name, errors)


def _quality_notes(inv: pd.DataFrame, enriched: pd.DataFrame, amounts: dict[str, Decimal]) -> list[DataQualityNote]:
    notes: list[DataQualityNote] = []

    def add(code: str, message: str, mask: pd.Series) -> None:
        if mask.any():
            nums = inv.loc[mask, "invoice_number"].tolist()
            notes.append(DataQualityNote(code, message, len(nums), nums))

    add("MISSING_PO", "Invoices without a po_number (policy §2.1/§2.2 maverick-spend check)", inv["po_number"].isna())
    add("MISSING_APPROVER", "Invoices without an approved_by record (policy §3.1: must be held)", inv["approved_by"].isna())
    add("CREDIT_NOTE", "Negative amounts (credit notes, policy §6.1); totals are net of these",
        inv["invoice_number"].map(lambda n: amounts[n] < 0).astype(bool))
    add("NON_ACTIVE_VENDOR", "Invoices from vendors not ACTIVE in the vendor master (policy §4.1)",
        enriched["vendor_status"] != "ACTIVE")
    add("VENDOR_NAME_MISMATCH", "Invoice vendor_name differs from vendor master name",
        enriched["vendor_name"] != enriched["vendor_master_name"])
    return notes


def load_datastore(invoices_path: Path, vendors_path: Path) -> DataStore:
    vendors = _read_csv(vendors_path, VENDOR_COLUMNS, VENDOR_REQUIRED)
    _validate_vendors(vendors, vendors_path.name)

    raw = _read_csv(invoices_path, INVOICE_COLUMNS, INVOICE_REQUIRED)
    _validate_invoices(raw, vendors, invoices_path.name)

    amounts = {n: Decimal(a) for n, a in zip(raw["invoice_number"], raw["amount"])}

    inv = raw.copy()
    inv["invoice_date"] = pd.to_datetime(inv["invoice_date"], format="%Y-%m-%d")
    inv["amount"] = inv["amount"].astype(float)

    v = vendors.rename(columns={
        "vendor_name": "vendor_master_name", "country": "vendor_country",
        "category": "vendor_category", "status": "vendor_status",
    })
    enriched = inv.merge(v, on="vendor_id", how="left", validate="many_to_one")
    enriched["is_credit_note"] = enriched["invoice_number"].map(lambda n: amounts[n] < 0).astype(bool)

    notes = _quality_notes(inv, enriched, amounts)
    enriched = enriched.drop(columns=["vendor_master_name"])

    return DataStore(
        invoices=inv,
        vendors=vendors,
        enriched=enriched,
        amounts=amounts,
        file_sha256={invoices_path.name: _sha256(invoices_path), vendors_path.name: _sha256(vendors_path)},
        notes=notes,
    )
