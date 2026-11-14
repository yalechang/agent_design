import json

import pytest
from fastapi.testclient import TestClient

from app.agent.llm import LLMError, OpenAILLM, _is_reasoning_model
from app.agent.orchestrator import OUT_OF_SCOPE_MESSAGE, Agent
from app.agent.policy_answerer import NO_EVIDENCE_MESSAGE, Citation, PolicyAnswerDraft
from app.agent.router import apply_rules
from app.main import create_app
from app.policy.clauses import load_policy
from app.tracing.trajectory import Tracer
from tests.conftest import REAL_DATA, make_settings
from tests.fake_llm import FakeLLM, decision

POLICY = load_policy(REAL_DATA / "procurement_policy.md")

Q3 = "Who is allowed to approve an invoice of EUR 60,000?"
Q3_DRAFT = PolicyAnswerDraft(
    supported=True,
    answer="An invoice of EUR 60,000 falls in the 50,000.01 – 250,000 band, so it must be approved by the "
           "Finance Director (§3, row 3.T3). Self-approval is prohibited (7.2).",
    citations=[Citation(clause_id="3.T3", quote="50,000.01 – 250,000 | Finance Director"),
               Citation(clause_id="7.2", quote="Self-approval is prohibited at all thresholds.")],
    caveats=[],
)


def agent(tmp_path, llm):
    return Agent(llm, POLICY, Tracer(tmp_path / "traces"))


# ---- end-to-end orchestration (scripted LLM) ---------------------------------------------

def test_example_q3_answered_with_verified_quote(tmp_path):
    llm = FakeLLM(router=[decision(["policy"], topics=["approval_thresholds"])], policy_answer=[Q3_DRAFT])
    r = agent(tmp_path, llm).ask(Q3)
    assert r.status == "ANSWERED"
    assert "Finance Director" in r.answer.text
    ev = {e.clause_id: e for e in r.evidence.policy}
    assert set(ev) == {"3.T3", "7.2"}
    assert ev["3.T3"].clause_text == "| 50,000.01 – 250,000 | Finance Director |"
    assert ev["3.T3"].table_header == "| Invoice / PO Value (EUR) | Required Approver |"
    assert ev["3.T3"].section == "§3 Approval Thresholds" and ev["3.T3"].document == "FIN-POL-014 v3.2"
    assert r.limitations == [] and r.error is None
    assert r.route.sources == ["policy"]
    assert llm.steps() == ["router", "policy_answer"]


def test_policy_prompt_contains_all_clauses_verbatim(tmp_path):
    llm = FakeLLM(router=[decision(["policy"])], policy_answer=[Q3_DRAFT])
    agent(tmp_path, llm).ask(Q3)
    instr = llm.calls[1]["instructions"]
    for c in POLICY.clauses:
        if c.kind != "table":
            assert f"[{c.id}]" in instr and c.text in instr


def test_out_of_scope(tmp_path):
    llm = FakeLLM(router=[decision([], in_scope=False, intent="other")])
    r = agent(tmp_path, llm).ask("What is today's weather?")
    assert r.status == "OUT_OF_SCOPE"
    assert r.answer.text == OUT_OF_SCOPE_MESSAGE
    assert r.evidence.policy == [] and llm.steps() == ["router"]


def test_fabricated_quote_repaired(tmp_path):
    bad = PolicyAnswerDraft(supported=True, answer="Finance Director (3.T3).",
                            citations=[Citation(clause_id="3.T3", quote="Finance Director approves up to 300k")],
                            caveats=[])
    llm = FakeLLM(router=[decision(["policy"])], policy_answer=[bad, Q3_DRAFT])
    r = agent(tmp_path, llm).ask(Q3)
    assert r.status == "ANSWERED" and {e.clause_id for e in r.evidence.policy} == {"3.T3", "7.2"}
    assert "failed verification" in llm.calls[2]["input"]
    assert not [l for l in r.limitations if l.kind == "CITATION_REMOVED"]


def test_partially_bad_citations_are_removed(tmp_path):
    mixed = PolicyAnswerDraft(supported=True, answer="Finance Director.", caveats=[], citations=[
        Citation(clause_id="3.T3", quote="Finance Director"),
        Citation(clause_id="9.9", quote="made up")])
    llm = FakeLLM(router=[decision(["policy"])], policy_answer=[mixed, mixed])
    r = agent(tmp_path, llm).ask(Q3)
    assert r.status == "ANSWERED"
    assert [e.clause_id for e in r.evidence.policy] == ["3.T3"]
    assert [l.kind for l in r.limitations] == ["CITATION_REMOVED"]


def test_no_verifiable_citation_means_no_evidence(tmp_path):
    bad = PolicyAnswerDraft(supported=True, answer="The CFO.", caveats=[],
                            citations=[Citation(clause_id="3.T4", quote="CFO approves everything")])
    llm = FakeLLM(router=[decision(["policy"])], policy_answer=[bad, bad])
    r = agent(tmp_path, llm).ask(Q3)
    assert r.status == "NO_EVIDENCE" and r.answer.text == NO_EVIDENCE_MESSAGE
    assert r.evidence.policy == []


def test_supported_without_citations_means_no_evidence(tmp_path):
    d = PolicyAnswerDraft(supported=True, answer="Yes.", citations=[], caveats=[])
    llm = FakeLLM(router=[decision(["policy"])], policy_answer=[d])
    assert agent(tmp_path, llm).ask("Is a dress code required?").status == "NO_EVIDENCE"


def test_policy_unsupported(tmp_path):
    d = PolicyAnswerDraft(supported=False, answer="", citations=[],
                          caveats=["The policy does not cover travel class."])
    llm = FakeLLM(router=[decision(["policy"])], policy_answer=[d])
    r = agent(tmp_path, llm).ask("Can staff fly business class?")
    assert r.status == "NO_EVIDENCE" and r.answer.text == NO_EVIDENCE_MESSAGE
    assert r.caveats == ["The policy does not cover travel class."]


def test_llm_unavailable(tmp_path):
    llm = FakeLLM(router=[LLMError("LLM_UNAVAILABLE", "timeout")])
    r = agent(tmp_path, llm).ask(Q3)
    assert r.status == "ERROR" and r.error.kind == "LLM_UNAVAILABLE"
    assert "timeout" not in r.error.message and "retry" in r.error.message


def test_llm_not_configured(tmp_path):
    r = agent(tmp_path, OpenAILLM(None, "gpt-5-mini")).ask(Q3)
    assert r.status == "ERROR" and r.error.kind == "LLM_NOT_CONFIGURED"


def test_internal_error_hidden_but_traced(tmp_path):
    llm = FakeLLM(router=[RuntimeError("boom secret detail")])
    a = agent(tmp_path, llm)
    r = a.ask(Q3)
    assert r.status == "ERROR" and r.error.kind == "INTERNAL" and "boom" not in r.error.message
    t = a.tracer.get(r.trace_id)
    assert "boom secret detail" in json.dumps(t) and "Traceback" in json.dumps(t)


# ---- router application rules ---------------------------------------------------------

@pytest.mark.parametrize("d,sources,in_scope", [
    (decision(["policy", "records"], intent="compliance_check"), ["policy", "records", "fx"], True),
    (decision(["records"], intent="aggregate"), ["records", "fx"], True),
    (decision(["records"], intent="aggregate", filt="EUR", reporting="EUR"), ["records"], True),
    (decision(["records"], intent="aggregate", filt="GBP", reporting="EUR"), ["records", "fx"], True),
    (decision(["records"], intent="lookup"), ["records"], True),
    (decision(["policy"], in_scope=False), [], False),
    (decision([], in_scope=True), [], False),
    (decision(["fx", "records", "records"], intent="lookup"), ["records", "fx"], True),
])
def test_router_rules(d, sources, in_scope):
    r = apply_rules(d)
    assert r.sources == sources and r.in_scope == in_scope


def test_policy_topics_dropped_without_policy():
    assert apply_rules(decision(["records"], intent="lookup", topics=["x"])).policy_topics == []


def test_router_prompt_lists_sources(tmp_path):
    llm = FakeLLM(router=[decision([], in_scope=False, intent="other")])
    agent(tmp_path, llm).ask("hi")
    instr = llm.calls[0]["instructions"]
    assert "§3. Approval Thresholds" in instr and "po_number" in instr and "ECB euro foreign exchange reference rates" in instr
    assert "{{" not in instr


@pytest.mark.parametrize("model,expected", [
    ("gpt-5-mini", True), ("gpt-5", True), ("o3", True), ("o4-mini", True), ("gpt-4.1", False), ("omni", False)])
def test_reasoning_model_detection(model, expected):
    assert _is_reasoning_model(model) is expected


# ---- tracing --------------------------------------------------------------------------

def test_trace_written_with_decision_trajectory(tmp_path):
    llm = FakeLLM(router=[decision(["policy"])], policy_answer=[Q3_DRAFT])
    a = agent(tmp_path, llm)
    r = a.ask(Q3)
    lines = list((tmp_path / "traces").glob("*.jsonl"))[0].read_text().splitlines()
    rec = json.loads(lines[-1])
    assert rec["trace_id"] == r.trace_id and rec["status"] == "ANSWERED" and rec["question"] == Q3
    names = [s["step"] for s in rec["steps"]]
    assert names == ["router", "policy_answer", "policy_verdict", "response"]
    router_step = rec["steps"][0]
    assert router_step["prompt_version"].startswith("router@")
    assert router_step["parsed"]["sources"] == ["policy"] and router_step["raw_output"]
    assert rec["steps"][1]["verified"][0]["clause_id"] == "3.T3"
    # Reload from disk in a fresh tracer
    assert Tracer(tmp_path / "traces").get(r.trace_id)["status"] == "ANSWERED"


# ---- HTTP -------------------------------------------------------------------------------

def http_client(tmp_path, llm, **kw):
    return TestClient(create_app(make_settings(REAL_DATA, tmp_path / "var", **kw), llm=llm))


def test_ask_endpoint(tmp_path):
    llm = FakeLLM(router=[decision(["policy"])], policy_answer=[Q3_DRAFT])
    with http_client(tmp_path, llm) as c:
        res = c.post("/ask", json={"question": f"  {Q3}  "})
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "ANSWERED" and body["question"] == Q3
        assert res.headers["X-Trace-Id"] == body["trace_id"]
        trace = c.get(f"/traces/{body['trace_id']}").json()
        assert [s["step"] for s in trace["steps"]][0] == "router"
        assert c.get("/traces/nope").status_code == 404


def test_ask_errors_are_200_with_status(tmp_path):
    llm = FakeLLM(router=[LLMError("LLM_UNAVAILABLE", "x")])
    with http_client(tmp_path, llm) as c:
        res = c.post("/ask", json={"question": Q3})
    assert res.status_code == 200 and res.json()["status"] == "ERROR"


@pytest.mark.parametrize("payload", [{"question": ""}, {"question": "   "}, {"question": "x" * 1001},
                                     {}, {"question": "ok", "reporting_currency": "CHF"}])
def test_ask_validation(tmp_path, payload):
    with http_client(tmp_path, FakeLLM()) as c:
        assert c.post("/ask", json=payload).status_code == 422


def test_traces_hidden_outside_dev(tmp_path):
    with http_client(tmp_path, FakeLLM(), DEV_MODE=False) as c:
        assert c.get("/traces/anything").status_code == 404


def test_ask_concurrency_limit_returns_503(tmp_path):
    with http_client(tmp_path, FakeLLM(), MAX_CONCURRENT_ASKS=1) as c:
        slots = c.app.state.ask_slots
        assert slots.acquire(blocking=False)          # simulate one question in flight
        try:
            res = c.post("/ask", json={"question": "Who approves EUR 60,000?"})
        finally:
            slots.release()
        assert res.status_code == 503 and res.headers["Retry-After"] == "15"


def test_security_headers(tmp_path):
    llm = FakeLLM(router=[decision([], in_scope=False, intent="other")])
    with http_client(tmp_path, llm) as c:
        res = c.post("/ask", json={"question": "weather?"})
        assert res.headers["X-Content-Type-Options"] == "nosniff"
        assert res.headers["X-Frame-Options"] == "DENY"
        assert res.headers["Cache-Control"] == "no-store"
        assert c.get("/health").headers["Referrer-Policy"] == "no-referrer"
