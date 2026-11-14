"""Human-readable descriptions of the local tables, shared by the router and (M5) code generation."""
from __future__ import annotations

INVOICE_COLUMNS = {
    "invoice_number": "unique invoice id, e.g. INV-20001",
    "invoice_date": "invoice date (all in 2025)",
    "vendor_id": "vendor id, joins to vendor_master.vendor_id",
    "vendor_name": "vendor name",
    "description": "what was purchased",
    "amount": "invoice amount in the invoice's original currency, 2 decimals; negative = credit note",
    "currency": "original currency of the invoice: EUR, GBP or USD",
    "cost_center": "cost center code, e.g. CC-IT-550",
    "po_number": "approved purchase order number; missing for some invoices",
    "approved_by": "approver user name; missing for some invoices",
}
VENDOR_COLUMNS = {
    "vendor_id": "unique vendor id",
    "vendor_name": "vendor name",
    "country": "vendor country (e.g. Spain, UK, Germany)",
    "category": "vendor category",
    "status": "ACTIVE, INACTIVE or ON_HOLD",
}


def tables_text() -> str:
    def block(title: str, cols: dict[str, str]) -> str:
        return title + "\n" + "\n".join(f"   - {c}: {d}" for c, d in cols.items())
    return (block("   invoices_2025_clean.csv (140 invoices):", INVOICE_COLUMNS) + "\n"
            + block("   vendor_master.csv (10 vendors):", VENDOR_COLUMNS))
