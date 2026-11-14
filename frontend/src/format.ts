/** Formatting for exact decimal strings from the API (no float rounding). */

export function groupDigits(intPart: string): string {
  return intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

export function formatAmount(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "unavailable";
  const s = typeof value === "number" ? value.toFixed(2) : value;
  const neg = s.startsWith("-");
  const [i, f = ""] = (neg ? s.slice(1) : s).split(".");
  return `${neg ? "−" : ""}${groupDigits(i)}.${(f + "00").slice(0, 2)}`;
}

export function formatMoney(value: string | number | null | undefined, currency?: string | null): string {
  const a = formatAmount(value);
  return a === "unavailable" || !currency ? a : `${currency} ${a}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  if (!y || !m || !d) return iso;
  return `${d} ${MONTHS[m - 1]} ${y}`;
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString("en-GB")} ${n === 1 ? one : many}`;
}

/** Human labels for raw column names coming from generated queries. */
const LABELS: Record<string, string> = {
  invoice_number: "Invoice",
  invoice_date: "Date",
  vendor_id: "Vendor ID",
  vendor_name: "Vendor",
  vendor_country: "Country",
  vendor_status: "Vendor status",
  vendor_category: "Category",
  description: "Description",
  cost_center: "Cost center",
  amount: "Amount",
  currency: "Currency",
  po_number: "PO",
  approved_by: "Approver",
  amount_eur: "Amount (EUR)",
  amount_usd: "Amount (USD)",
  amount_gbp: "Amount (GBP)",
  fx_status: "Rate used",
  fx_observation_date: "Rate date",
  fx_ref_id: "Rate reference",
  is_credit_note: "Credit note",
};

export function columnLabel(col: string): string {
  if (LABELS[col]) return LABELS[col];
  const words = col.replace(/_/g, " ").replace(/\b(eur|usd|gbp)\b/gi, (c) => c.toUpperCase());
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export const FX_STATUS_LABEL: Record<string, string> = {
  IDENTITY: "No conversion",
  EXACT: "Invoice date",
  FALLBACK: "Earlier date",
  UNAVAILABLE: "No rate",
};
