import type { AskResponse } from "./api/types";
import { plural } from "./format";

export type SlotState = "verified" | "limited" | "unused";
export type SourceSlot = { key: "policy" | "records" | "fx"; name: string; state: SlotState; stateText: string; detail?: string };

const has = (r: AskResponse, kind: string) => r.limitations.some((l) => l.kind === kind);

/** What each of the three evidence sources contributed to this answer, in plain words. */
export function sourceSlots(r: AskResponse): SourceSlot[] {
  const sources = r.route?.sources ?? [];
  return [policySlot(r, sources), recordsSlot(r, sources), fxSlot(r, sources)];
}

function policySlot(r: AskResponse, sources: string[]): SourceSlot {
  const base = { key: "policy" as const, name: "Procurement policy" };
  if (!sources.includes("policy")) return { ...base, state: "unused", stateText: "Not used" };
  const n = r.evidence.policy.length;
  if (n === 0) return { ...base, state: "limited", stateText: "No supporting clause found" };
  const detail = `${plural(n, "clause")} quoted word for word`;
  if (has(r, "CITATION_REMOVED"))
    return { ...base, state: "limited", stateText: "Quoted, some citations removed", detail };
  return { ...base, state: "verified", stateText: "Quoted and checked", detail };
}

function recordsSlot(r: AskResponse, sources: string[]): SourceSlot {
  const base = { key: "records" as const, name: "Invoice and vendor records" };
  const d = r.evidence.data;
  if (!d) {
    if (!sources.includes("records") && !sources.includes("fx")) return { ...base, state: "unused", stateText: "Not used" };
    if (r.status === "QUERY_FAILED" || has(r, "DATA_QUERY_FAILED"))
      return { ...base, state: "limited", stateText: "Query could not run" };
    return { ...base, state: "unused", stateText: "Not used" };
  }
  const n = d.supporting_rows.total_count;
  const detail = n > 0 ? `${plural(n, "invoice")} behind the answer` : "No invoice rows needed";
  const statuses = d.aggregations.map((a) => a.status);
  if (statuses.includes("unavailable")) return { ...base, state: "limited", stateText: "Some figures unavailable", detail };
  if (statuses.includes("unverified")) return { ...base, state: "limited", stateText: "Partly re-checked", detail };
  return {
    ...base,
    state: "verified",
    stateText: statuses.length ? "Figures recalculated and matched" : "Rows taken from the data",
    detail,
  };
}

function fxSlot(r: AskResponse, sources: string[]): SourceSlot {
  const base = { key: "fx" as const, name: "ECB reference rates" };
  const fx = r.evidence.fx;
  if (!fx) {
    if (sources.includes("fx") && (r.status === "QUERY_FAILED" || has(r, "DATA_QUERY_FAILED")))
      return { ...base, state: "unused", stateText: "Not reached" };
    return { ...base, state: "unused", stateText: "Not used" };
  }
  const converted = fx.coverage.EXACT + fx.coverage.FALLBACK + fx.refs.filter((x) => x.amount != null).length;
  const earlier = fx.coverage.FALLBACK + fx.refs.filter((x) => x.amount != null && x.status === "FALLBACK").length;
  const missing = fx.unavailable_invoices.length;
  if (missing > 0 || has(r, "FX_UNAVAILABLE"))
    return {
      ...base,
      state: "limited",
      stateText: "Some rates missing",
      detail: missing ? `${plural(missing, "invoice")} without an acceptable rate` : "A requested conversion has no rate",
    };
  if (converted === 0)
    return { ...base, state: "unused", stateText: "Not needed", detail: `All amounts already in ${fx.reporting_currency}` };
  return {
    ...base,
    state: "verified",
    stateText: "Rates cited",
    detail: `${plural(converted, "conversion")}${earlier ? `, ${earlier} used an earlier date` : ""}`,
  };
}

export const STATUS_TEXT: Record<AskResponse["status"], { text: string; tone: "ok" | "limited" | "none" | "fault"; mark: string }> = {
  ANSWERED: { text: "Answered from the evidence", tone: "ok", mark: "✓" },
  PARTIAL: { text: "Answered with limitations", tone: "limited", mark: "!" },
  NO_EVIDENCE: { text: "No supporting evidence found", tone: "none", mark: "–" },
  OUT_OF_SCOPE: { text: "Outside the available evidence", tone: "none", mark: "–" },
  QUERY_FAILED: { text: "This question could not be run on the data", tone: "fault", mark: "×" },
  ERROR: { text: "Not answered", tone: "fault", mark: "×" },
};
