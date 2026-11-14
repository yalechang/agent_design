"""Plan-and-solve orchestration for POST /ask.

1. router decides the sources (or out of scope)
2. policy branch answers from verified policy clauses
3. records/fx branch answers from executed, application-verified pandas code
4. compose merges branches into one response with explicit status and limitations
Every step is written to the trajectory log.
"""
from __future__ import annotations

import logging
import traceback

from typing import Callable

from app.agent import data_answerer, policy_answerer, router
from app.agent.data_answerer import DataContext
from app.agent.llm import LLM, LLMError
from app.api.schemas import (AnswerBlock, AskResponse, ErrorInfo, Evidence, Limitation, PolicyEvidence,
                             RouteInfo)
from app.policy.clauses import PolicyDocument
from app.tracing.trajectory import Tracer

log = logging.getLogger("app.agent")

OUT_OF_SCOPE_MESSAGE = (
    "This question cannot be supported by any evidence in the available sources: the 2025 invoice data, "
    "the vendor master, the procurement policy, and the ECB euro foreign exchange reference rates."
)
LLM_ERROR_MESSAGES = {
    "LLM_NOT_CONFIGURED": "The language model is not configured (OPENAI_API_KEY is missing), so questions "
                          "cannot be answered. The data summary is still available.",
    "LLM_UNAVAILABLE": "The language model could not be reached, so nothing was answered. Please retry.",
    "LLM_REFUSAL": "The language model declined to process this question.",
    "LLM_BAD_OUTPUT": "The language model returned an invalid response twice, so nothing was answered. "
                      "Please retry or rephrase.",
}
class Agent:
    def __init__(self, llm: LLM, policy: PolicyDocument, tracer: Tracer,
                 data_context: Callable[[str, bool], DataContext] | None = None):
        self.llm = llm
        self.policy = policy
        self.tracer = tracer
        self.data_context = data_context   # (reporting_currency, exact_date) -> DataContext

    def ask(self, question: str, reporting_currency: str | None = None) -> AskResponse:
        trace = self.tracer.start(question, model=self.llm.model, policy_sha256=self.policy.sha256,
                                  reporting_currency_override=reporting_currency)
        try:
            resp = self._ask(question, reporting_currency, trace)
        except LLMError as e:
            trace.step("error", kind=e.kind, message=str(e))
            resp = AskResponse(trace_id=trace.trace_id, status="ERROR", question=question,
                               error=ErrorInfo(kind=e.kind, message=LLM_ERROR_MESSAGES.get(e.kind, str(e))))
        except Exception as e:  # never leak a stack trace to the analyst; keep it in the trace
            log.exception("ask failed")
            trace.step("error", kind="INTERNAL", message=str(e), traceback=traceback.format_exc())
            resp = AskResponse(trace_id=trace.trace_id, status="ERROR", question=question,
                               error=ErrorInfo(kind="INTERNAL", message="An internal error occurred; "
                                               "the details were logged under this trace id."))
        trace.finish(resp.status, resp)
        return resp

    def _ask(self, question: str, reporting_currency: str | None, trace) -> AskResponse:
        r = router.route(self.llm, self.policy, question, trace)
        if reporting_currency and reporting_currency != r.reporting_currency:
            r.rules_applied.append(f"reporting currency set to {reporting_currency} by the request")
            r.reporting_currency = reporting_currency
        route_info = RouteInfo(**r.model_dump())
        base = dict(trace_id=trace.trace_id, question=question, route=route_info)

        if not r.in_scope:
            return AskResponse(status="OUT_OF_SCOPE", answer=AnswerBlock(text=OUT_OF_SCOPE_MESSAGE), **base)

        wants_policy = "policy" in r.sources
        wants_data = "records" in r.sources or "fx" in r.sources

        policy_res = (policy_answerer.answer(self.llm, self.policy, question, trace, data_part=wants_data)
                      if wants_policy else None)
        evidence = Evidence(policy=[self._policy_evidence(v) for v in policy_res.citations] if policy_res else [])
        caveats = list(policy_res.caveats) if policy_res else []
        limitations: list[Limitation] = []
        if policy_res and policy_res.rejected:
            limitations.append(Limitation(
                kind="CITATION_REMOVED",
                message=f"{len(policy_res.rejected)} citation(s) proposed by the model could not be found "
                        f"verbatim in the policy and were removed.",
            ))

        if not wants_data:
            if not policy_res.supported:
                return AskResponse(status="NO_EVIDENCE", answer=AnswerBlock(text=policy_res.answer),
                                   limitations=limitations, caveats=caveats, evidence=evidence, **base)
            return AskResponse(status="ANSWERED", answer=AnswerBlock(text=policy_res.answer), evidence=evidence,
                               limitations=limitations, caveats=caveats, **base)

        ctx = self.data_context(r.reporting_currency, r.exact_date_fx)
        trace.step("data_context", reporting_currency=r.reporting_currency, exact_date=r.exact_date_fx,
                   fx_version=ctx.view.fx_version, fx_coverage=ctx.view.coverage,
                   snapshots={k: v.snapshot_id for k, v in ctx.rates.snapshots.items()})
        citations = policy_res.citations if policy_res and policy_res.supported else []
        d = data_answerer.answer(self.llm, ctx, question, r, citations, trace)
        evidence.data, evidence.fx = d.data, d.fx
        limitations += d.limitations
        caveats += [c for c in d.caveats if c not in caveats]

        data_ok = d.status in ("ANSWERED", "PARTIAL")
        if not data_ok:
            if policy_res and policy_res.supported:
                limitations.append(Limitation(kind="DATA_QUERY_FAILED", message=d.failure_message or ""))
                return AskResponse(status="PARTIAL", answer=AnswerBlock(text=policy_res.answer), evidence=evidence,
                                   limitations=limitations, caveats=caveats, **base)
            return AskResponse(status=d.status, answer=AnswerBlock(text=d.failure_message or ""),
                               evidence=evidence, limitations=limitations, caveats=caveats,
                               error=ErrorInfo(kind=d.error_kind or "QUERY_FAILED", message=d.failure_message or ""),
                               **base)

        status = d.status
        if policy_res and not policy_res.supported:
            status = "PARTIAL"
            limitations.append(Limitation(kind="POLICY_NO_EVIDENCE",
                                          message="The policy part of the question is not supported by any "
                                                  "clause; only the data part was answered."))
        return AskResponse(status=status, answer=AnswerBlock(text=d.answer_text or "", key_figures=d.key_figures),
                           evidence=evidence, limitations=limitations, caveats=caveats, **base)

    def _policy_evidence(self, v: policy_answerer.VerifiedCitation) -> PolicyEvidence:
        c = v.clause
        return PolicyEvidence(clause_id=c.id, section=f"§{c.section_number} {c.section_title}", kind=c.kind,
                              quote=v.quote, clause_text=c.text, table_header=c.table_header,
                              document=self.policy.doc_label)
