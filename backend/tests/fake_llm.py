"""Scripted stand-in for the OpenAI client: returns queued outputs per step."""
from app.agent.llm import LLMResult
from app.agent.router import RouterDecision


class FakeLLM:
    model = "fake-model"

    def __init__(self, **script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[dict] = []

    def parse(self, *, step, instructions, input, schema):
        self.calls.append({"step": step, "instructions": instructions, "input": input})
        queue = self.script.get(step)
        if not queue:
            raise AssertionError(f"unexpected LLM call for step {step!r}")
        out = queue.pop(0)
        if isinstance(out, Exception):
            raise out
        assert isinstance(out, schema), f"{step}: expected {schema.__name__}"
        return LLMResult(out, out.model_dump_json(), self.model, 5, {"total_tokens": 10}, "resp_1")

    def steps(self):
        return [c["step"] for c in self.calls]


def decision(sources, in_scope=True, intent="policy_rule", reporting="EUR", filt=None, exact=False,
             topics=None, rationale="test"):
    return RouterDecision(in_scope=in_scope, sources=sources, intent=intent, reporting_currency=reporting,
                          original_currency_filter=filt, exact_date_fx=exact, policy_topics=topics or [],
                          rationale=rationale)
