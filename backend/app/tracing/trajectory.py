"""Per-request decision trajectory, appended as one JSON line to var/traces/YYYY-MM-DD.jsonl.

Each /ask gets a trace_id; every step (router, policy answer, verification, compose, ...) records
its inputs, the raw model output, the parsed decision and timing, so unexpected answers can be
replayed and explained. Secrets are never passed in.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def _default(o: Any):
    if isinstance(o, BaseModel):
        return o.model_dump(mode="json")
    if is_dataclass(o):
        return asdict(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, (Decimal, Path, set, frozenset)):
        return list(o) if isinstance(o, (set, frozenset)) else str(o)
    return repr(o)


class Trace:
    def __init__(self, tracer: "Tracer", question: str, context: dict):
        self.tracer = tracer
        self.trace_id = str(uuid.uuid4())
        self.started_at = datetime.now(timezone.utc)
        self._t0 = time.monotonic()
        self.question = question
        self.context = context
        self.steps: list[dict] = []
        self.status: str | None = None

    def step(self, name: str, **data: Any) -> dict:
        entry = {"step": name, "at_ms": int((time.monotonic() - self._t0) * 1000), **data}
        self.steps.append(entry)
        return entry

    def llm_step(self, name: str, result, prompt_version: str, input_text: str, **extra: Any) -> dict:
        return self.step(name, kind="llm", model=result.model, prompt_version=prompt_version,
                         latency_ms=result.latency_ms, usage=result.usage, attempts=result.attempts,
                         response_id=result.response_id, input=input_text, raw_output=result.raw_text,
                         parsed=result.parsed, **extra)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "started_at": self.started_at.isoformat(),
            "duration_ms": int((time.monotonic() - self._t0) * 1000),
            "question": self.question,
            "status": self.status,
            "context": self.context,
            "steps": self.steps,
        }

    def finish(self, status: str, response: Any = None) -> None:
        self.status = status
        if response is not None:
            self.step("response", response=response)
        self.tracer._write(self)


class Tracer:
    def __init__(self, directory: Path, keep_in_memory: int = 200):
        self.dir = directory
        self._recent: OrderedDict[str, dict] = OrderedDict()
        self._keep = keep_in_memory
        self._lock = threading.Lock()

    def start(self, question: str, **context: Any) -> Trace:
        return Trace(self, question, context)

    def _write(self, trace: Trace) -> None:
        record = json.loads(json.dumps(trace.to_dict(), default=_default))
        line = json.dumps(record, ensure_ascii=False)
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{trace.started_at:%Y-%m-%d}.jsonl"
        with self._lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._recent[trace.trace_id] = record
            while len(self._recent) > self._keep:
                self._recent.popitem(last=False)

    def get(self, trace_id: str) -> dict | None:
        with self._lock:
            if trace_id in self._recent:
                return self._recent[trace_id]
        for path in sorted(self.dir.glob("*.jsonl"), reverse=True):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if trace_id in line:
                        rec = json.loads(line)
                        if rec.get("trace_id") == trace_id:
                            return rec
        return None
