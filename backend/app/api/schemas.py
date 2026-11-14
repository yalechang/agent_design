"""Pydantic response models — the API contract the frontend is built against."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ComponentStatus(BaseModel):
    status: Literal["ok", "unavailable", "not_configured", "error"]
    detail: str | None = None


class DataHealth(ComponentStatus):
    invoice_count: int = 0
    vendor_count: int = 0
    date_range: tuple[date, date] | None = None
    file_sha256: dict[str, str] = {}


class PolicyHealth(ComponentStatus):
    document: str | None = None
    clause_count: int = 0
    sha256: str | None = None


class LLMHealth(ComponentStatus):
    model: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    data: DataHealth
    policy: PolicyHealth
    fx: ComponentStatus
    llm: LLMHealth


class FxSeriesStatus(BaseModel):
    state: Literal["ok", "unavailable"]
    source_url: str
    snapshot_id: str | None = None
    fetched_at: str | None = None
    release: str | None = None
    last_observation: str | None = None
    last_error: str | None = None


class FxCoverage(BaseModel):
    reporting_currency: str
    counts: dict[str, int]          # IDENTITY / EXACT / FALLBACK / UNAVAILABLE
    complete: bool
    unavailable_invoices: list[str]


class FxStatusResponse(BaseModel):
    available: bool
    refreshing: bool
    last_refresh_attempt: str | None
    series: dict[str, FxSeriesStatus]
    eur_invoice_coverage: FxCoverage


# ---- /summary -------------------------------------------------------------------
# Money is always a decimal string with 2 places (e.g. "25079.62") to avoid float drift in JSON.
Money = str


class CurrencyTotal(BaseModel):
    currency: str
    amount: Money                 # net of credit notes
    invoice_count: int
    credit_note_count: int = 0


class FxLegSource(BaseModel):
    currency: str
    series: str
    url: str                      # exact ECB Data Portal API request
    page: str | None = None       # human-readable ECB series page
    snapshot_id: str | None
    snapshot_fetched_at: str | None
    release: str | None


class FxEvidenceSummary(BaseModel):
    """Aggregate citation for a consolidated figure; per-invoice detail is available via /ask."""
    sources: list[FxLegSource]
    coverage: dict[str, int]                    # IDENTITY / EXACT / FALLBACK / UNAVAILABLE
    observation_date_range: tuple[date, date] | None
    fallback_invoices: list[str]
    max_fallback_days: int
    formula: list[str]
    method: str
    convention_note: str
    policy_note: str            # basis of conversion relative to policy §5.1


class MissingRate(BaseModel):
    invoice_number: str
    invoice_date: date
    currency: str
    amount: Money
    reason: str | None
    missing_evidence: list[str]


class CurrencyConversion(BaseModel):
    currency: str                 # original currency
    original_amount: Money
    invoice_count: int
    converted_amount: Money       # in the consolidated currency (sum of rounded invoice amounts)


class Consolidated(BaseModel):
    available: bool
    currency: str = "EUR"
    total: Money | None = None                  # null (never 0, never partial) when unavailable
    by_currency: list[CurrencyConversion] = []  # reconciliation lines; empty when unavailable
    invoice_count: int
    evidence: FxEvidenceSummary | None = None
    unavailable_reason: str | None = None
    missing: list[MissingRate] = []
    recommendation: str | None = None


class StatusBreakdown(BaseModel):
    status: str
    vendor_count: int
    invoice_count: int
    original_currency_totals: list[CurrencyTotal]
    eur_total: Money | None                     # null if any invoice in the group lacks a rate
    eur_available: bool


class VendorSpend(BaseModel):
    rank: int
    vendor_id: str
    vendor_name: str
    country: str
    category: str
    status: str
    eur_total: Money
    invoice_count: int
    original_currency_totals: list[CurrencyTotal]


class DataQualityItem(BaseModel):
    code: str
    message: str
    count: int
    invoice_numbers: list[str]


class DatasetInfo(BaseModel):
    invoice_count: int
    vendor_count: int
    date_range: tuple[date, date]
    file_sha256: dict[str, str]
    policy_document: str


class SummaryResponse(BaseModel):
    generated_at: str
    dataset: DatasetInfo
    original_currency_totals: list[CurrencyTotal]
    consolidated: Consolidated
    by_vendor_status: list[StatusBreakdown]
    top_vendors_eur: list[VendorSpend] | None   # null when consolidation is incomplete
    top_vendors_unavailable_reason: str | None = None
    data_quality: list[DataQualityItem]
    fx_status: dict[str, FxSeriesStatus]


# ---- /ask ---------------------------------------------------------------------------
AskStatus = Literal["ANSWERED", "PARTIAL", "NO_EVIDENCE", "OUT_OF_SCOPE", "QUERY_FAILED", "ERROR"]


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    reporting_currency: Literal["EUR", "USD", "GBP"] | None = None

    @field_validator("question")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question must not be blank")
        return v


class RouteInfo(BaseModel):
    in_scope: bool
    sources: list[Literal["policy", "records", "fx"]]
    intent: str
    reporting_currency: str
    original_currency_filter: str | None
    exact_date_fx: bool
    policy_topics: list[str]
    rationale: str
    rules_applied: list[str]


class KeyFigure(BaseModel):
    label: str
    value: str
    currency: str | None = None


class AnswerBlock(BaseModel):
    text: str
    key_figures: list[KeyFigure] = []


class PolicyEvidence(BaseModel):
    clause_id: str
    section: str                 # e.g. "§3 Approval Thresholds"
    kind: str                    # paragraph | intro | table_row
    quote: str                   # verified verbatim excerpt
    clause_text: str             # full clause text, for context
    table_header: str | None = None
    document: str                # e.g. "FIN-POL-014 v3.2"


class Evidence(BaseModel):
    policy: list[PolicyEvidence] = []
    data: "DataEvidence | None" = None
    fx: "FxEvidenceBlock | None" = None


class Limitation(BaseModel):
    kind: str
    message: str
    affected_invoices: list[str] = []
    recommendation: str | None = None


class ErrorInfo(BaseModel):
    kind: str
    message: str


class AskResponse(BaseModel):
    trace_id: str
    status: AskStatus
    question: str
    route: RouteInfo | None = None
    answer: AnswerBlock | None = None
    evidence: Evidence = Field(default_factory=lambda: Evidence())
    limitations: list[Limitation] = []
    caveats: list[str] = []
    error: ErrorInfo | None = None


# ---- /ask data & FX evidence ------------------------------------------------------------
class ResultTable(BaseModel):
    columns: list[str]
    rows: list[list]                         # money cells are exact decimal strings
    money_columns: dict[str, str] = {}       # column -> currency code, or "ORIGINAL" (row's own currency)
    row_count: int
    truncated: bool = False


class SupportingRows(BaseModel):
    columns: list[str]
    rows: list[list]
    total_count: int
    truncated: bool = False


class GroupBreakdown(BaseModel):
    group: dict[str, str]
    totals: list[CurrencyTotal]


class AggregationCheck(BaseModel):
    column: str
    agg: str
    source_column: str
    group_by: list[str]
    status: Literal["verified", "unverified", "unavailable"]
    detail: str


class DataEvidence(BaseModel):
    result_description: str
    result_table: ResultTable
    supporting_rows: SupportingRows
    original_currency_breakdown: list[CurrencyTotal]
    group_breakdowns: list[GroupBreakdown] = []
    aggregations: list[AggregationCheck] = []
    notes: list[str] = []
    code: str
    row_sources: list[str]


class FxRef(BaseModel):
    fx_ref_id: str | None
    status: str
    from_currency: str
    to_currency: str
    requested_date: str
    observation_date: str | None
    fallback_days: int
    exact_date_requested: bool
    legs: list[dict]
    formula: str
    reason: str | None = None
    missing_evidence: list[str] = []
    invoice_numbers: list[str] = []          # invoices that used this observation
    amount: str | None = None                # for convert_amount() calls on non-invoice amounts
    result: str | None = None


class FxEvidenceBlock(BaseModel):
    reporting_currency: str
    exact_date: bool
    coverage: dict[str, int]
    refs: list[FxRef]
    sources: list[FxLegSource]
    method: str
    convention_note: str | None
    policy_note: str            # basis of conversion relative to policy §5.1
    unavailable_invoices: list[str] = []


Evidence.model_rebuild()
AskResponse.model_rebuild()
