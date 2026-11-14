"""Run the golden question set against the real model and write a report.

    cd backend && .venv/bin/python -m evals.run_golden [--workers 4] [--only id1,id2]

Writes var/evals/golden-<timestamp>.{json,md}. Exit code 1 if the pass rate is below 95%.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app.agent.data_answerer import DataContext
from app.agent.llm import OpenAILLM
from app.agent.orchestrator import Agent
from app.config import get_settings
from app.data.store import load_datastore
from app.fx.cache import SnapshotCache
from app.fx.enrich import FxViews
from app.fx.service import FxService
from app.policy.clauses import load_policy
from app.tracing.trajectory import Tracer
from evals.golden import Case, build_cases
from evals.reference import Reference

PASS_RATE = 0.95


def make_agent() -> Agent:
    s = get_settings()
    store = load_datastore(s.invoices_path, s.vendors_path)
    fx = FxService(SnapshotCache(s.fx_cache_dir))
    fx.load_cached()
    if fx.missing_series():
        sys.exit("ECB rate snapshots are not cached; start the app once (or POST /fx/refresh) first.")
    views = FxViews(store, fx)
    key = s.openai_api_key.get_secret_value() if s.openai_api_key else None
    llm = OpenAILLM(key, s.openai_model, s.openai_reasoning_effort, s.openai_timeout_s)
    return Agent(llm, load_policy(s.policy_path), Tracer(s.traces_dir),
                 lambda rep, ex: DataContext(store, views.get(rep, ex), fx.rates()))


def run_case(agent: Agent, case: Case) -> dict:
    t0 = time.monotonic()
    resp = agent.ask(case.question).model_dump(mode="json")
    secs = round(time.monotonic() - t0, 1)
    failures = []
    if resp["status"] not in case.status:
        failures.append(f"status {resp['status']} not in {sorted(case.status)}")
    sources = (resp.get("route") or {}).get("sources", [])
    missing = [s for s in case.sources_include if s not in sources]
    if missing:
        failures.append(f"route lacks {missing} (got {sources})")
    if resp["status"] in case.status:
        for desc, fn in case.checks:
            try:
                ok = fn(resp)
            except Exception as e:  # a check that crashes is a failure, not a runner error
                ok, desc = False, f"{desc} (check raised {type(e).__name__}: {e})"
            if not ok:
                failures.append(desc)
    return {"id": case.id, "category": case.category, "question": case.question, "passed": not failures,
            "failures": failures, "status": resp["status"], "sources": sources, "seconds": secs,
            "trace_id": resp["trace_id"], "answer": (resp.get("answer") or {}).get("text")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    cases = build_cases(Reference())
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c.id in wanted]
    agent = make_agent()
    t0 = time.monotonic()
    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(lambda c: run_case(agent, c), cases))
    passed = sum(r["passed"] for r in results)
    rate = passed / len(results)

    s = get_settings()
    out = s.var_dir / "evals"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {"run_at": stamp, "model": s.openai_model, "passed": passed, "total": len(results),
              "pass_rate": round(rate, 3), "wall_seconds": round(time.monotonic() - t0, 1), "results": results}
    (out / f"golden-{stamp}.json").write_text(json.dumps(report, indent=1))
    lines = [f"# Golden eval {stamp}", "", f"Model `{s.openai_model}`: **{passed}/{len(results)} passed "
             f"({rate:.0%})**, threshold {PASS_RATE:.0%}.", "",
             "| Case | Category | Result | Status | Seconds | Failure | Trace |", "|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['id']} | {r['category']} | {'pass' if r['passed'] else 'FAIL'} | {r['status']} | "
                     f"{r['seconds']} | {'; '.join(r['failures'])} | {r['trace_id'][:8]} |")
    (out / f"golden-{stamp}.md").write_text("\n".join(lines) + "\n")

    for r in results:
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['id']:18s} {r['status']:13s} {r['seconds']:5.1f}s  "
              f"{'; '.join(r['failures'])}")
    print(f"\n{passed}/{len(results)} passed ({rate:.0%}); report: {out / f'golden-{stamp}.md'}")
    return 0 if rate >= PASS_RATE else 1


if __name__ == "__main__":
    sys.exit(main())
