import type { AskResponse, SummaryResponse } from "../api/types";

const ECB = "https://data-api.ecb.europa.eu/service/data/EXR";
const fxStatus = {
  USD: { state: "ok" as const, source_url: `${ECB}/D.USD.EUR.SP00.A?startPeriod=2024-01-01&format=csvdata`, snapshot_id: "s-usd", fetched_at: "2026-11-14T00:00:00Z", release: "rates up to 2026-11-13", last_observation: "2026-11-13", last_error: null },
  GBP: { state: "ok" as const, source_url: `${ECB}/D.GBP.EUR.SP00.A?startPeriod=2024-01-01&format=csvdata`, snapshot_id: "s-gbp", fetched_at: "2026-11-14T00:00:00Z", release: "rates up to 2026-11-13", last_observation: "2026-11-13", last_error: null },
};

export const summaryOk: SummaryResponse = {
  generated_at: "2026-11-14T00:00:00Z",
  dataset: { invoice_count: 140, vendor_count: 10, date_range: ["2025-01-03", "2025-12-26"], file_sha256: {}, policy_document: "FIN-POL-014 v3.2" },
  original_currency_totals: [
    { currency: "EUR", amount: "4493726.12", invoice_count: 89, credit_note_count: 1 },
    { currency: "GBP", amount: "1182251.02", invoice_count: 27, credit_note_count: 0 },
    { currency: "USD", amount: "1150445.53", invoice_count: 24, credit_note_count: 1 },
  ],
  consolidated: {
    available: true, currency: "EUR", total: "6887110.91", invoice_count: 140,
    by_currency: [
      { currency: "EUR", original_amount: "4493726.12", invoice_count: 89, converted_amount: "4493726.12" },
      { currency: "GBP", original_amount: "1182251.02", invoice_count: 27, converted_amount: "1377856.93" },
      { currency: "USD", original_amount: "1150445.53", invoice_count: 24, converted_amount: "1015527.86" },
    ],
    evidence: {
      sources: [{ currency: "USD", series: "ECB euro reference exchange rate — US dollar per euro (EXR.D.USD.EUR.SP00.A)", url: fxStatus.USD.source_url, page: "https://data.ecb.europa.eu/data/datasets/EXR/EXR.D.USD.EUR.SP00.A", snapshot_id: "s-usd", snapshot_fetched_at: null, release: "rates up to 2026-11-13" }],
      coverage: { IDENTITY: 89, EXACT: 35, FALLBACK: 16, UNAVAILABLE: 0 },
      observation_date_range: ["2025-01-31", "2025-12-22"],
      fallback_invoices: Array.from({ length: 16 }, (_, i) => `INV-${i}`),
      max_fallback_days: 3,
      formula: ["GBP→EUR = amount × USD/GBP ÷ USD/EUR"],
      method: "Decimal arithmetic.",
      convention_note: "Prototype convention.",
      policy_note: "Converted at ECB euro foreign exchange reference rates, the rate source that policy §5.1 requires.",
    },
    unavailable_reason: null, missing: [], recommendation: null,
  },
  by_vendor_status: [
    { status: "ACTIVE", vendor_count: 8, invoice_count: 111, original_currency_totals: [], eur_total: "5386845.31", eur_available: true },
    { status: "ON_HOLD", vendor_count: 1, invoice_count: 12, original_currency_totals: [], eur_total: "415667.82", eur_available: true },
  ],
  top_vendors_eur: [
    { rank: 1, vendor_id: "V-1008", vendor_name: "BioReagent Direct", country: "France", category: "Lab", status: "ACTIVE", eur_total: "1131562.07", invoice_count: 19, original_currency_totals: [] },
  ],
  top_vendors_unavailable_reason: null,
  data_quality: [{ code: "MISSING_PO", message: "x", count: 21, invoice_numbers: [] }],
  fx_status: fxStatus,
};

export const summaryNoFx: SummaryResponse = {
  ...summaryOk,
  consolidated: {
    available: false, currency: "EUR", total: null, invoice_count: 140, by_currency: [], evidence: null,
    unavailable_reason: "No validated ECB reference-rate snapshot is loaded for USD, GBP.",
    missing: [{ invoice_number: "INV-1", invoice_date: "2025-03-08", currency: "USD", amount: "10.00", reason: "NO_SNAPSHOT", missing_evidence: [] }],
    recommendation: "Review the listed invoices.",
  },
  by_vendor_status: summaryOk.by_vendor_status.map((s) => ({ ...s, eur_total: null, eur_available: false })),
  top_vendors_eur: null,
  top_vendors_unavailable_reason: "Ranking in EUR is unavailable because some invoices lack an acceptable ECB reference rate.",
  fx_status: { USD: { ...fxStatus.USD, state: "unavailable", snapshot_id: null, release: null }, GBP: { ...fxStatus.GBP, state: "unavailable", snapshot_id: null, release: null } },
};

const route = (sources: ("policy" | "records" | "fx")[]) => ({
  in_scope: sources.length > 0, sources, intent: "x", reporting_currency: "EUR", original_currency_filter: null,
  exact_date_fx: false, policy_topics: [], rationale: "Because.", rules_applied: [],
});

export const policyAnswer: AskResponse = {
  trace_id: "trace-policy-1", status: "ANSWERED", question: "Who approves EUR 60,000?",
  route: route(["policy"]),
  answer: { text: "The Finance Director (§3, row 3.T3).", key_figures: [] },
  evidence: {
    policy: [{ clause_id: "3.T3", section: "§3 Approval Thresholds", kind: "table_row", quote: "50,000.01 – 250,000 | Finance Director",
      clause_text: "| 50,000.01 – 250,000 | Finance Director |", table_header: "| Invoice / PO Value (EUR) | Required Approver |", document: "FIN-POL-014 v3.2" }],
    data: null, fx: null,
  },
  limitations: [], caveats: [], error: null,
};

export const dataAnswer: AskResponse = {
  trace_id: "trace-data-1", status: "PARTIAL", question: "Total spend using exact rates?",
  route: route(["records", "fx"]),
  answer: { text: "Total: unavailable.", key_figures: [{ label: "Total", value: "unavailable", currency: null }] },
  evidence: {
    policy: [],
    data: {
      result_description: "Total spend in EUR.",
      result_table: { columns: ["total_eur"], rows: [[null]], money_columns: { total_eur: "EUR" }, row_count: 1, truncated: false },
      supporting_rows: {
        columns: ["invoice_number", "invoice_date", "vendor_id", "vendor_name", "vendor_country", "vendor_status", "description", "cost_center", "amount", "currency", "po_number", "approved_by", "amount_eur", "fx_status", "fx_observation_date", "fx_ref_id"],
        rows: [
          ["INV-1", "2025-03-08", "V-1", "Acme", "Spain", "ACTIVE", "x", "CC", "100.00", "USD", null, "a.b", null, "UNAVAILABLE", null, null],
          ["INV-2", "2025-03-07", "V-1", "Acme", "Spain", "ACTIVE", "x", "CC", "50.00", "EUR", "PO-1", null, "50.00", "IDENTITY", null, null],
        ],
        total_count: 2, truncated: false,
      },
      original_currency_breakdown: [{ currency: "EUR", amount: "50.00", invoice_count: 1, credit_note_count: 0 }, { currency: "USD", amount: "100.00", invoice_count: 1, credit_note_count: 0 }],
      group_breakdowns: [],
      aggregations: [{ column: "total_eur", agg: "sum", source_column: "amount_eur", group_by: [], status: "unavailable", detail: "includes invoices without a rate" }],
      notes: [], code: "def run(invoices, vendors, fx_refs, rates):\n    return {}", row_sources: ["invoices_2025_clean.csv", "vendor_master.csv"],
    },
    fx: {
      reporting_currency: "EUR", exact_date: true, coverage: { IDENTITY: 1, EXACT: 0, FALLBACK: 0, UNAVAILABLE: 1 },
      refs: [{ fx_ref_id: null, status: "UNAVAILABLE", from_currency: "USD", to_currency: "EUR", requested_date: "2025-03-08", observation_date: null, fallback_days: 0, exact_date_requested: true, legs: [{ currency: "USD", value: null }], formula: "USD→EUR = amount ÷ USD/EUR", reason: "EXACT_DATE_NOT_AVAILABLE", missing_evidence: ["ECB USD has no reference rate on 2025-03-08 (weekend or TARGET holiday: no ECB publication)"], invoice_numbers: ["INV-1"], amount: null, result: null }],
      sources: [], method: "Decimal.", convention_note: null, policy_note: "Converted at ECB euro foreign exchange reference rates, the rate source that policy §5.1 requires.", unavailable_invoices: ["INV-1"],
    },
  },
  limitations: [{ kind: "FX_UNAVAILABLE", message: "1 invoice has no acceptable ECB reference rate.", affected_invoices: ["INV-1"], recommendation: "Review it." }],
  caveats: [], error: null,
};

export const outOfScope: AskResponse = {
  trace_id: "trace-oos", status: "OUT_OF_SCOPE", question: "Weather?", route: route([]),
  answer: { text: "This question cannot be supported by any evidence.", key_figures: [] },
  evidence: { policy: [], data: null, fx: null }, limitations: [], caveats: [], error: null,
};

export const queryFailed: AskResponse = {
  trace_id: "trace-qf", status: "QUERY_FAILED", question: "Spend by region?", route: route(["records", "fx"]),
  answer: { text: "There is no region field. Available invoice fields: vendor_country.", key_figures: [] },
  evidence: { policy: [], data: null, fx: null }, limitations: [], caveats: [],
  error: { kind: "UNSUPPORTED_QUERY", message: "There is no region field." },
};
