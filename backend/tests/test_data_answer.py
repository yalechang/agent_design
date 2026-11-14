import csv
from datetime import date
from decimal import Decimal

import pytest

from app.agent.data_answerer import (CodeDraft, DataContext, Explanation, FigureRef, substitute, typed_numbers)
from app.agent.orchestrator import Agent
from app.agent.policy_answerer import Citation, PolicyAnswerDraft
from app.api.schemas import ResultTable
from app.data.store import load_datastore
from app.fx.enrich import build_fx_view
from app.policy.clauses import load_policy
from app.tracing.trajectory import Tracer
from tests.conftest import FIXTURE_FILES, REAL_DATA
from tests.fake_llm import FakeLLM, decision
from tests.reference_summary import read_rates, reference, to_eur

INV, VEN = REAL_DATA / "invoices_2025_clean.csv", REAL_DATA / "vendor_master.csv"
POLICY = load_policy(REAL_DATA / "procurement_policy.md")

TOP5 = '''
def run(invoices, vendors, fx_refs, rates):
    g = (invoices.groupby(["vendor_id", "vendor_name"], as_index=False)
         .agg(total_eur=("amount_eur", "sum"), invoice_count=("invoice_number", "count")))
    top = g.sort_values(["total_eur", "vendor_id"], ascending=[False, True]).head(5)
    sup = invoices[invoices["vendor_id"].isin(top["vendor_id"])]["invoice_number"].tolist()
    return {"result": top, "supporting_invoice_numbers": sup,
            "aggregations": [
                {"column": "total_eur", "agg": "sum", "source_column": "amount_eur", "group_by": ["vendor_id"]},
                {"column": "invoice_count", "agg": "count", "source_column": "invoice_number", "group_by": ["vendor_id"]}],
            "notes": ["net of credit notes"]}
'''
SPAIN = '''
def run(invoices, vendors, fx_refs, rates):
    es = invoices[invoices["vendor_country"] == "Spain"]
    res = pd.DataFrame({"total_eur": [es["amount_eur"].sum()], "invoice_count": [len(es)]})
    return {"result": res, "supporting_invoice_numbers": es["invoice_number"].tolist(),
            "aggregations": [{"column": "total_eur", "agg": "sum", "source_column": "amount_eur", "group_by": []},
                             {"column": "invoice_count", "agg": "count", "source_column": "invoice_number",
                              "group_by": []}],
            "notes": []}
'''
TOTAL_DROPNA = SPAIN.replace('invoices[invoices["vendor_country"] == "Spain"]',
                             'invoices[invoices["amount_eur"].notna()]')
TOTAL_ALL = SPAIN.replace('invoices[invoices["vendor_country"] == "Spain"]', 'invoices')


def draft(code, desc="result"):
    return CodeDraft(can_answer=True, reason="", code=code, result_description=desc)


def explanation(text="Answer: {{0:total_eur}}.", figs=(("Total", 0, "total_eur"),)):
    return Explanation(answer_text=text, key_figures=[FigureRef(label=l, row=r, column=c) for l, r, c in figs])


@pytest.fixture(scope="module")
def store():
    return load_datastore(INV, VEN)


@pytest.fixture
def make_agent(tmp_path, store, fixture_rates):
    def make(llm):
        def ctx(rep, exact):
            return DataContext(store=store, view=build_fx_view(store, fixture_rates, rep, exact), rates=fixture_rates)
        return Agent(llm, POLICY, Tracer(tmp_path / "traces"), ctx)
    return make


@pytest.fixture(scope="module")
def ref():
    return reference(INV, VEN, FIXTURE_FILES["USD"], FIXTURE_FILES["GBP"])


def spain_reference():
    usd, gbp = read_rates(FIXTURE_FILES["USD"]), read_rates(FIXTURE_FILES["GBP"])
    spain = {r["vendor_id"] for r in csv.DictReader(open(VEN)) if r["country"] == "Spain"}
    rows = [r for r in csv.DictReader(open(INV)) if r["vendor_id"] in spain]
    total = sum(to_eur(Decimal(r["amount"]), r["currency"], date.fromisoformat(r["invoice_date"]), usd, gbp)
                for r in rows)
    return f"{total:.2f}", {r["invoice_number"] for r in rows}


# ---- example questions, to the cent ------------------------------------------------------

def test_q1_top5_vendors(make_agent, ref):
    llm = FakeLLM(router=[decision(["records", "fx"], intent="ranking")], codegen=[draft(TOP5, "Top 5 vendors")],
                  explain=[explanation("The top vendor was {{0:vendor_name}} with {{0:total_eur}}.",
                                       [("Top vendor spend", 0, "total_eur")])])
    r = make_agent(llm).ask("What are our top 5 vendors by total spend in 2025?")
    assert r.status == "ANSWERED", (r.error, r.limitations)
    t = r.evidence.data.result_table
    got = [(row[t.columns.index("vendor_id")], row[t.columns.index("total_eur")]) for row in t.rows]
    assert got == ref["top5"]                                         # exact decimal strings
    assert t.money_columns == {"total_eur": "EUR"}
    assert r.answer.text == "The top vendor was BioReagent Direct with EUR 1,131,552.52."
    assert r.answer.key_figures[0].value == "EUR 1,131,552.52" and r.answer.key_figures[0].currency == "EUR"
    checks = {c.column: c.status for c in r.evidence.data.aggregations}
    assert checks == {"total_eur": "verified", "invoice_count": "verified"}
    assert len(r.evidence.data.group_breakdowns) == 5                # original-currency breakdown per vendor
    sup = r.evidence.data.supporting_rows
    assert sup.total_count == sum(row[t.columns.index("invoice_count")] for row in t.rows)
    assert "amount_eur" in sup.columns and "fx_status" in sup.columns
    fx = r.evidence.fx
    assert fx.reporting_currency == "EUR" and {s.currency for s in fx.sources} == {"USD", "GBP"}
    assert all(ref_.legs and ref_.invoice_numbers for ref_ in fx.refs)
    assert "§5.1" in fx.policy_note and any("Prototype convention" in c for c in r.caveats)
    assert "def run" in r.evidence.data.code


def test_q2_spain(make_agent):
    expected, invoices = spain_reference()
    llm = FakeLLM(router=[decision(["records", "fx"], intent="aggregate")], codegen=[draft(SPAIN)],
                  explain=[explanation("Spend with Spanish vendors was {{0:total_eur}} across {{0:invoice_count}} "
                                       "invoices.")])
    r = make_agent(llm).ask("What is our total spend with vendors based in Spain?")
    assert r.status == "ANSWERED"
    t = r.evidence.data.result_table
    assert t.rows[0][t.columns.index("total_eur")] == expected
    assert set(row[0] for row in r.evidence.data.supporting_rows.rows) == invoices
    assert r.answer.text == f"Spend with Spanish vendors was EUR {Decimal(expected):,.2f} across {len(invoices)} invoices."
    breakdown = {b.currency for b in r.evidence.data.original_currency_breakdown}
    assert breakdown == {"EUR", "GBP", "USD"}


# ---- verification & repair -----------------------------------------------------------------

def test_wrong_number_is_caught_and_repaired(make_agent):
    wrong = SPAIN.replace('es["amount_eur"].sum()', 'es["amount_eur"].sum() + 100')
    llm = FakeLLM(router=[decision(["records", "fx"], intent="aggregate")], codegen=[draft(wrong), draft(SPAIN)],
                  explain=[explanation()])
    a = make_agent(llm)
    r = a.ask("Spain total?")
    assert r.status == "ANSWERED"
    assert "failed application verification" in llm.calls[2]["input"]
    trace = a.tracer.get(r.trace_id)
    assert [s["ok"] for s in trace["steps"] if s["step"] == "verify"] == [False, True]


def test_wrong_number_twice_is_query_failed(make_agent):
    wrong = SPAIN.replace('es["amount_eur"].sum()', 'es["amount_eur"].sum() * 2')
    llm = FakeLLM(router=[decision(["records", "fx"], intent="aggregate")], codegen=[draft(wrong), draft(wrong)])
    r = make_agent(llm).ask("Spain total?")
    assert r.status == "QUERY_FAILED" and r.evidence.data is None
    assert "recomputed" in r.answer.text and r.error.kind == "QUERY_FAILED"


def test_unknown_column_explained(make_agent):
    bad = SPAIN.replace('invoices["vendor_country"] == "Spain"', 'invoices["region"] == "EMEA"')
    llm = FakeLLM(router=[decision(["records"], intent="aggregate")], codegen=[draft(bad), draft(bad)])
    r = make_agent(llm).ask("Total spend in the EMEA region?")
    assert r.status == "QUERY_FAILED"
    assert "column 'region' does not exist" in r.answer.text and "vendor country" in r.answer.text


def test_declined_query(make_agent):
    d = CodeDraft(can_answer=False, reason="There is no budget data; amount and amount_eur are actual spend.",
                  code="", result_description="")
    llm = FakeLLM(router=[decision(["records"], intent="aggregate")], codegen=[d])
    r = make_agent(llm).ask("Are we over budget?")
    assert r.status == "QUERY_FAILED" and r.error.kind == "UNSUPPORTED_QUERY"
    assert "no budget data" in r.answer.text and "Available invoice fields" in r.answer.text


def test_runtime_error_then_fixed(make_agent):
    broken = SPAIN.replace('len(es)', 'es.loc["nope"]')
    llm = FakeLLM(router=[decision(["records", "fx"], intent="aggregate")], codegen=[draft(broken), draft(SPAIN)],
                  explain=[explanation()])
    r = make_agent(llm).ask("Spain total?")
    assert r.status == "ANSWERED" and "KeyError" in llm.calls[2]["input"]


def test_summing_original_amounts_across_currencies_rejected(make_agent):
    mixed = SPAIN.replace('es["amount_eur"].sum()', 'es["amount"].sum()').replace(
        '"source_column": "amount_eur"', '"source_column": "amount"')
    llm = FakeLLM(router=[decision(["records"], intent="aggregate")], codegen=[draft(mixed), draft(mixed)])
    r = make_agent(llm).ask("Spain total?")
    assert r.status == "QUERY_FAILED" and "across currencies" in r.answer.text


def test_eur_denominated_filter_uses_original_amount(make_agent, ref):
    code = '''
def run(invoices, vendors, fx_refs, rates):
    e = invoices[invoices["currency"] == "EUR"]
    res = pd.DataFrame({"currency": ["EUR"], "total_amount": [e["amount"].sum()]})
    return {"result": res, "supporting_invoice_numbers": e["invoice_number"].tolist(),
            "aggregations": [{"column": "total_amount", "agg": "sum", "source_column": "amount", "group_by": []}],
            "notes": ["invoices originally in EUR"]}
'''
    llm = FakeLLM(router=[decision(["records"], intent="aggregate", filt="EUR")], codegen=[draft(code)],
                  explain=[explanation("EUR-denominated invoices total {{0:total_amount}}.")])
    r = make_agent(llm).ask("Total of invoices issued in EUR?")
    assert r.status == "ANSWERED" and r.evidence.fx is None
    assert r.answer.text == f"EUR-denominated invoices total EUR {Decimal(ref['original']['EUR']):,.2f}."
    assert not any("ECB" in c for c in r.caveats)        # no conversion -> no rate notes


# ---- FX completeness ---------------------------------------------------------------------------

def test_exact_date_makes_total_unavailable_not_partial(make_agent, store, fixture_rates):
    exact_view = build_fx_view(store, fixture_rates, "EUR", exact_date=True)
    llm = FakeLLM(router=[decision(["records", "fx"], intent="aggregate", exact=True)], codegen=[draft(TOTAL_ALL)],
                  explain=[explanation("Total: {{0:total_eur}}.")])
    r = make_agent(llm).ask("Total spend using exact invoice-date rates only?")
    assert r.status == "PARTIAL"
    t = r.evidence.data.result_table
    assert t.rows[0][t.columns.index("total_eur")] is None           # never a partial sum
    assert r.answer.text == "Total: unavailable."
    lim = [l for l in r.limitations if l.kind == "FX_UNAVAILABLE"][0]
    assert set(lim.affected_invoices) == set(exact_view.unavailable) and lim.recommendation.startswith("Review")
    assert r.evidence.fx.exact_date and r.evidence.fx.coverage["UNAVAILABLE"] == len(exact_view.unavailable)
    assert sum(b.invoice_count for b in r.evidence.data.original_currency_breakdown) == 140
    agg = {c.column: c.status for c in r.evidence.data.aggregations}
    assert agg["total_eur"] == "unavailable" and agg["invoice_count"] == "verified"


def test_dropping_unavailable_rows_is_detected(make_agent, store, fixture_rates):
    """Code that filters out NaN rates would look complete; the probe runs expose it."""
    exact_view = build_fx_view(store, fixture_rates, "EUR", exact_date=True)
    llm = FakeLLM(router=[decision(["records", "fx"], intent="aggregate", exact=True)],
                  codegen=[draft(TOTAL_DROPNA)], explain=[explanation()])
    r = make_agent(llm).ask("Total spend using exact invoice-date rates only?")
    assert r.status == "PARTIAL"
    t = r.evidence.data.result_table
    assert t.rows[0][t.columns.index("total_eur")] is None
    lim = [l for l in r.limitations if l.kind == "FX_UNAVAILABLE"][0]
    assert set(lim.affected_invoices) == set(exact_view.unavailable)


def test_filtering_on_fx_status_is_detected(make_agent, store, fixture_rates):
    """Live regression: GPT filtered fx_status == 'EXACT', silently dropping invoices without a rate."""
    exact_view = build_fx_view(store, fixture_rates, "EUR", exact_date=True)
    code = SPAIN.replace('invoices[invoices["vendor_country"] == "Spain"]',
                         'invoices[invoices["fx_status"].isin(["EXACT", "IDENTITY"])]')
    llm = FakeLLM(router=[decision(["records", "fx"], intent="aggregate", exact=True)],
                  codegen=[draft(code)], explain=[explanation()])
    r = make_agent(llm).ask("Total spend using only exact invoice-date rates?")
    assert r.status == "PARTIAL"
    t = r.evidence.data.result_table
    assert t.rows[0][t.columns.index("total_eur")] is None
    lim = [l for l in r.limitations if l.kind == "FX_UNAVAILABLE"][0]
    assert set(lim.affected_invoices) == set(exact_view.unavailable)


def test_hybrid_policy_step_told_to_answer_policy_part_only(make_agent):
    pol = PolicyAnswerDraft(supported=True, answer="Finance Director band (3.T3).", caveats=[],
                            citations=[Citation(clause_id="3.T3", quote="50,000.01 – 250,000 | Finance Director")])
    llm = FakeLLM(router=[decision(["policy", "records"], intent="compliance_check")], policy_answer=[pol],
                  codegen=[draft(SPAIN)], explain=[explanation()])
    make_agent(llm).ask("Which invoices exceed the Finance Director threshold?")
    assert "Answer ONLY the policy part" in llm.calls[1]["input"]


def test_convert_amount_helper_cites_ecb(make_agent):
    code = '''
def run(invoices, vendors, fx_refs, rates):
    eur = convert_amount(60000, "USD", "EUR", "2025-03-08")
    return {"result": pd.DataFrame({"amount_usd": [60000.0], "amount_eur": [eur]}),
            "supporting_invoice_numbers": [], "aggregations": [], "notes": ["dated 2025-03-08"]}
'''
    pol = PolicyAnswerDraft(supported=True, answer="Thresholds are in EUR (3.T3).", caveats=[],
                            citations=[Citation(clause_id="3.T3", quote="50,000.01 – 250,000 | Finance Director")])
    llm = FakeLLM(router=[decision(["policy", "fx"], intent="policy_rule")], policy_answer=[pol],
                  codegen=[draft(code)],
                  explain=[explanation("USD 60,000 on 2025-03-08 is {{0:amount_eur}}, so the Finance Director "
                                       "approves (3.T3).", [("EUR equivalent", 0, "amount_eur")])])
    r = make_agent(llm).ask("Who approves a USD 60,000 invoice dated 2025-03-08?")
    assert r.status == "ANSWERED"
    ref_ = r.evidence.fx.refs[0]
    assert (ref_.requested_date, ref_.observation_date, ref_.status) == ("2025-03-08", "2025-03-07", "FALLBACK")
    expected = (Decimal("60000") / Decimal("1.0857")).quantize(Decimal("0.01"))
    assert ref_.result == str(expected)
    assert {e.clause_id for e in r.evidence.policy} == {"3.T3"}
    assert r.answer.text.startswith(f"USD 60,000 on 2025-03-08 is EUR {expected:,.2f}")


def test_hybrid_policy_and_data(make_agent):
    code = '''
def run(invoices, vendors, fx_refs, rates):
    big = invoices[(invoices["amount_eur"] > 50000) & (invoices["approved_by"].isna())]
    res = big[["invoice_number", "vendor_name", "amount", "currency", "amount_eur"]].sort_values("amount_eur", ascending=False)
    return {"result": res, "supporting_invoice_numbers": big["invoice_number"].tolist(), "aggregations": [],
            "notes": ["threshold from 3.T3"]}
'''
    pol = PolicyAnswerDraft(supported=True, answer="Above 50,000 EUR needs the Finance Director (3.T3); "
                            "invoices lacking an approver must be held (3.1).", caveats=[],
                            citations=[Citation(clause_id="3.T3", quote="50,000.01 – 250,000 | Finance Director"),
                                       Citation(clause_id="3.1", quote="Invoices lacking an approver record must be held")])
    llm = FakeLLM(router=[decision(["policy", "records"], intent="compliance_check")], policy_answer=[pol],
                  codegen=[draft(code)],
                  explain=[explanation("{{0:invoice_number}} is the largest such invoice at {{0:amount_eur}} "
                                       "(3.T3, 3.1).", [("Largest", 0, "amount_eur")])])
    r = make_agent(llm).ask("Which invoices above the Finance Director threshold have no approver?")
    assert r.status == "ANSWERED"
    assert "[3.T3]" in llm.calls[2]["instructions"]           # policy clauses passed to codegen
    t = r.evidence.data.result_table
    assert t.money_columns == {"amount": "ORIGINAL", "amount_eur": "EUR"}
    assert all(isinstance(row[t.columns.index("amount_eur")], str) for row in t.rows)
    assert r.evidence.policy and r.evidence.data and r.evidence.fx


def test_typed_numbers_trigger_fallback(make_agent):
    llm = FakeLLM(router=[decision(["records", "fx"], intent="aggregate")], codegen=[draft(SPAIN, "Spain total")],
                  explain=[explanation("Total was 1,234,567.89 EUR."), explanation("Total was 99,999.00.")])
    r = make_agent(llm).ask("Spain total?")
    assert r.status == "ANSWERED"
    assert r.answer.text == "Spain total The verified result table and its evidence are shown below."


def test_llm_failure_in_codegen(make_agent):
    from app.agent.llm import LLMError
    llm = FakeLLM(router=[decision(["records"], intent="lookup")], codegen=[LLMError("LLM_UNAVAILABLE", "x")])
    assert make_agent(llm).ask("List invoices").error.kind == "LLM_UNAVAILABLE"


# ---- helpers -------------------------------------------------------------------------------

def test_typed_number_detection():
    assert typed_numbers("Total {{0:total_eur}} for INV-20001 on 2025-03-07 in 2025 (§3, 3.T3)") == []
    assert typed_numbers("Total 1,234.50 and 60000 and 3.5") == ["1,234.50", "60000", "3.5"]


def test_substitute():
    t = ResultTable(columns=["currency", "amount", "n"], rows=[["GBP", "10.50", 3]],
                    money_columns={"amount": "ORIGINAL"}, row_count=1)
    assert substitute("{{0:amount}} / {{0:n}} / {{3:amount}}", t) == ("GBP 10.50 / 3 / [n/a]", ["{{3:amount}}"])


def test_technical_terms_detected():
    from app.agent.data_answerer import JARGON, technical_terms
    assert technical_terms("filtered by amount_eur > 50,000 and fx_status", JARGON) == ["amount_eur", "fx_status"]
    assert technical_terms("Total spend converted to EUR", JARGON) == []


def test_jargon_triggers_one_rewrite(make_agent):
    llm = FakeLLM(router=[decision(["records", "fx"], intent="aggregate")], codegen=[draft(SPAIN)],
                  explain=[explanation("Sum of amount_eur is {{0:total_eur}}."),
                           explanation("Spend in Spain was {{0:total_eur}}.")])
    r = make_agent(llm).ask("Spain total?")
    assert r.answer.text.startswith("Spend in Spain was EUR ")
    assert "technical column names ['amount_eur']" in llm.calls[-1]["input"]


def test_plain_language_in_decline():
    from app.agent.data_answerer import plain_language
    assert plain_language("closest is vendor_country; use amount_eur") == "closest is vendor country; use amount in EUR"
