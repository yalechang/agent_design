"""Thin wrapper around the OpenAI Responses API with schema-enforced (structured) outputs.

Every call returns an ``LLMResult`` with the parsed object plus raw text, model, token usage and
latency, so the trajectory log can record exactly what the model said.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    def __init__(self, kind: str, message: str):
        self.kind = kind            # LLM_NOT_CONFIGURED | LLM_UNAVAILABLE | LLM_REFUSAL | LLM_BAD_OUTPUT
        super().__init__(message)


@dataclass
class LLMResult:
    parsed: Any
    raw_text: str
    model: str
    latency_ms: int
    usage: dict = field(default_factory=dict)
    response_id: str | None = None
    attempts: int = 1


class LLM(Protocol):
    model: str

    def parse(self, *, step: str, instructions: str, input: str, schema: type[T]) -> LLMResult: ...


def _is_reasoning_model(model: str) -> bool:
    m = model.lower()
    return m.startswith("gpt-5") or (m.startswith("o") and m[1:2].isdigit())


class OpenAILLM:
    def __init__(self, api_key: str | None, model: str, reasoning_effort: str = "low", timeout_s: float = 60):
        self.model = model
        self.reasoning_effort = reasoning_effort
        self._api_key = api_key
        self._timeout = timeout_s
        self._client = None

    def _get_client(self):
        if not self._api_key:
            raise LLMError("LLM_NOT_CONFIGURED", "OPENAI_API_KEY is not set")
        if self._client is None:
            import openai
            self._client = openai.OpenAI(api_key=self._api_key, timeout=self._timeout, max_retries=1)
        return self._client

    def parse(self, *, step: str, instructions: str, input: str, schema: type[T]) -> LLMResult:
        import openai
        client = self._get_client()
        kwargs: dict[str, Any] = dict(model=self.model, instructions=instructions, input=input,
                                      text_format=schema, store=False)
        if self.reasoning_effort and _is_reasoning_model(self.model):
            kwargs["reasoning"] = {"effort": self.reasoning_effort}

        last: Exception | None = None
        for attempt in (1, 2):   # one extra attempt for malformed output
            t0 = time.monotonic()
            try:
                resp = client.responses.parse(**kwargs)
            except openai.APIError as e:   # network, timeout, rate limit, 5xx (SDK already retried once)
                raise LLMError("LLM_UNAVAILABLE", f"{type(e).__name__}: {e}") from e
            except ValidationError as e:
                last = e
                continue
            latency = int((time.monotonic() - t0) * 1000)
            parsed = resp.output_parsed
            raw = getattr(resp, "output_text", "") or ""
            if parsed is None:
                refusal = _refusal_text(resp)
                if refusal:
                    raise LLMError("LLM_REFUSAL", refusal)
                last = ValueError(f"no parsed output; raw={raw[:200]!r}")
                continue
            usage = resp.usage.model_dump() if getattr(resp, "usage", None) else {}
            return LLMResult(parsed, raw, resp.model or self.model, latency, usage, resp.id, attempt)
        raise LLMError("LLM_BAD_OUTPUT", f"{step}: model output did not match the schema ({last})")


def _refusal_text(resp) -> str | None:
    for item in getattr(resp, "output", []) or []:
        for c in getattr(item, "content", []) or []:
            if getattr(c, "type", None) == "refusal":
                return getattr(c, "refusal", "refused")
    return None
