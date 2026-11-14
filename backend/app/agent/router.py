"""Step 1 — decide which evidence sources a question needs (GPT, structured output)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.agent import prompts
from app.agent.llm import LLM
from app.agent.schema_context import tables_text
from app.policy.clauses import PolicyDocument
from app.tracing.trajectory import Trace

Source = Literal["policy", "records", "fx"]
Currency = Literal["EUR", "USD", "GBP"]
Intent = Literal["policy_rule", "aggregate", "ranking", "lookup", "compliance_check", "fx_rate_lookup", "other"]

PROMPT = prompts.load("router")


class RouterDecision(BaseModel):
    """Schema the model must fill. No defaults: structured outputs require every field."""
    in_scope: bool
    sources: list[Source]
    intent: Intent
    reporting_currency: Currency
    original_currency_filter: Currency | None
    exact_date_fx: bool
    policy_topics: list[str]
    rationale: str = Field(description="one or two sentences")


class Route(BaseModel):
    """Router decision after application rules; this is what the orchestrator acts on."""
    in_scope: bool
    sources: list[Source]
    intent: Intent
    reporting_currency: Currency
    original_currency_filter: Currency | None
    exact_date_fx: bool
    policy_topics: list[str]
    rationale: str
    rules_applied: list[str]


def apply_rules(d: RouterDecision) -> Route:
    rules: list[str] = []
    sources = list(dict.fromkeys(d.sources))   # de-duplicate, keep order
    in_scope = d.in_scope
    if not in_scope and sources:
        rules.append("out of scope: cleared sources")
        sources = []
    if in_scope and not sources:
        rules.append("in scope but no sources chosen: treated as out of scope")
        in_scope = False
    if "records" in sources and "policy" in sources and "fx" not in sources:
        rules.append("policy thresholds are in EUR: added fx to policy+records")
        sources.append("fx")
    if "records" in sources and d.intent in ("aggregate", "ranking") and "fx" not in sources:
        single_currency = d.original_currency_filter and d.original_currency_filter == d.reporting_currency
        if not single_currency:
            rules.append("money aggregate across currencies: added fx")
            sources.append("fx")
    order = {"policy": 0, "records": 1, "fx": 2}
    return Route(in_scope=in_scope, sources=sorted(sources, key=order.get), intent=d.intent,
                 reporting_currency=d.reporting_currency, original_currency_filter=d.original_currency_filter,
                 exact_date_fx=d.exact_date_fx, policy_topics=d.policy_topics if "policy" in sources else [],
                 rationale=d.rationale, rules_applied=rules)


def route(llm: LLM, policy: PolicyDocument, question: str, trace: Trace) -> Route:
    sections = sorted({(c.section_number, c.section_title) for c in policy.clauses}, key=lambda s: int(s[0]))
    instructions = PROMPT.render(
        POLICY_SECTIONS="\n".join(f"   §{n}. {t}" for n, t in sections),
        TABLE_SCHEMAS=tables_text(),
    )
    result = llm.parse(step="router", instructions=instructions, input=question, schema=RouterDecision)
    decided = apply_rules(result.parsed)
    trace.llm_step("router", result, PROMPT.version, question, route=decided)
    return decided
