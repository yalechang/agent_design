import type { components } from "./schema";

type S = components["schemas"];
/** The backend always serialises defaulted fields; OpenAPI marks them optional, so state it here once. */
type Evidence = Omit<S["Evidence"], "policy"> & { policy: S["PolicyEvidence"][] };
export type AskResponse = Omit<S["AskResponse"], "evidence" | "limitations" | "caveats"> & {
  evidence: Evidence;
  limitations: S["Limitation"][];
  caveats: string[];
};
export type AskStatus = AskResponse["status"];
export type SummaryResponse = S["SummaryResponse"];
export type CurrencyTotal = S["CurrencyTotal"];
export type PolicyEvidence = S["PolicyEvidence"];
export type DataEvidence = S["DataEvidence"];
export type FxEvidenceBlock = S["FxEvidenceBlock"];
export type FxRef = S["FxRef"];
export type Limitation = S["Limitation"];
export type ResultTable = S["ResultTable"];
export type SupportingRows = S["SupportingRows"];
export type AggregationCheck = S["AggregationCheck"];
export type RouteInfo = S["RouteInfo"];
export type ReportingCurrency = "EUR" | "USD" | "GBP";
export type CurrencyConversion = S["CurrencyConversion"];
