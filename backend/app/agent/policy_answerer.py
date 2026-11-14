"""Step 2 — answer from procurement_policy.md only, with citations verified against the source text."""
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel

from app.agent import prompts
from app.agent.llm import LLM
from app.policy.clauses import Clause, PolicyDocument
from app.tracing.trajectory import Trace

PROMPT = prompts.load("policy")
NO_EVIDENCE_MESSAGE = "No clause in the procurement policy supports an answer to this question."


class Citation(BaseModel):
    clause_id: str
    quote: str


class PolicyAnswerDraft(BaseModel):
    supported: bool
    answer: str
    citations: list[Citation]
    caveats: list[str]


@dataclass
class VerifiedCitation:
    clause: Clause
    quote: str


@dataclass
class PolicyResult:
    supported: bool
    answer: str
    citations: list[VerifiedCitation]
    caveats: list[str]
    rejected: list[dict] = field(default_factory=list)   # citations that failed verification


def clause_context(policy: PolicyDocument) -> str:
    parts = []
    for c in policy.clauses:
        if c.kind == "table":
            continue  # rows are listed individually below; the header travels with each row
        head = f"[{c.id}] (§{c.section_number} {c.section_title}"
        if c.kind == "table_row":
            head += f"; table row; columns: {c.table_header}"
        parts.append(f"{head})\n{c.text}")
    return "\n\n".join(parts)


def _verify(policy: PolicyDocument, draft: PolicyAnswerDraft) -> tuple[list[VerifiedCitation], list[dict]]:
    ok, bad = [], []
    seen = set()
    for c in draft.citations:
        cid = c.clause_id.strip().lstrip("§").strip("[]")
        clause = policy.get(cid)
        if clause is None:
            bad.append({"clause_id": c.clause_id, "quote": c.quote, "problem": "unknown clause id"})
        elif not policy.quote_in_clause(cid, c.quote):
            bad.append({"clause_id": c.clause_id, "quote": c.quote, "problem": "quote not found verbatim in clause"})
        elif (cid, c.quote) not in seen:
            seen.add((cid, c.quote))
            ok.append(VerifiedCitation(clause, c.quote.strip()))
    return ok, bad


HYBRID_NOTE = ("\n\nNote: the invoice and vendor records are queried by a separate step. Answer ONLY the policy "
               "part: identify the rules, thresholds and clauses that apply to this question. Set supported = true "
               "if clauses define the applicable rules, even though you cannot see the invoices. Keep caveats about the "
               "policy itself; do not mention that invoice data or conversions are missing, since that step supplies "
               "them.")


def answer(llm: LLM, policy: PolicyDocument, question: str, trace: Trace, data_part: bool = False) -> PolicyResult:
    instructions = PROMPT.render(DOC_LABEL=f"{policy.title} ({policy.doc_label}, effective "
                                           f"{policy.metadata.get('Effective', 'n/a')})",
                                 CLAUSES=clause_context(policy))
    user_input = question + (HYBRID_NOTE if data_part else "")
    rejected_all: list[dict] = []
    for attempt in (1, 2):
        result = llm.parse(step="policy_answer", instructions=instructions, input=user_input,
                           schema=PolicyAnswerDraft)
        draft: PolicyAnswerDraft = result.parsed
        ok, bad = _verify(policy, draft)
        trace.llm_step("policy_answer", result, PROMPT.version, user_input, attempt=attempt,
                       verified=[{"clause_id": v.clause.id, "quote": v.quote} for v in ok], rejected=bad)
        rejected_all.extend(bad)
        if not draft.supported or not bad:
            break
        # One repair attempt: tell the model exactly which citations failed.
        user_input = (f"{question}{HYBRID_NOTE if data_part else ''}\n\nYour previous citations failed verification:\n"
                      + "\n".join(f"- [{b['clause_id']}] {b['quote']!r}: {b['problem']}" for b in bad)
                      + "\nCopy quotes word-for-word from the clause text, or drop the claim.")

    if not draft.supported or not ok:
        trace.step("policy_verdict", supported=False,
                   reason="model reported unsupported" if not draft.supported else "no citation verified")
        return PolicyResult(False, NO_EVIDENCE_MESSAGE, [], draft.caveats, rejected_all)
    trace.step("policy_verdict", supported=True, citations=len(ok), rejected=len(bad))
    return PolicyResult(True, draft.answer, ok, draft.caveats, bad)
