"""Runs GPT-generated pandas code in a separate, spawned process.

Isolation (defence in depth on top of the static checks in sandbox.py):
- fresh interpreter (spawn), restricted builtins (no __import__/open/eval/exec), `pd` and `np` provided
- network disabled (socket creation raises), CPU-time limit, best-effort memory limit
- inputs are copies; outputs are normalised to plain Python values before crossing back
"""
from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal

MAX_RESULT_ROWS = 1000

SAFE_BUILTINS = {
    name: __builtins__[name] if isinstance(__builtins__, dict) else getattr(__builtins__, name)
    for name in ("abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "isinstance",
                 "len", "list", "map", "max", "min", "range", "reversed", "round", "set", "sorted", "str",
                 "sum", "tuple", "zip", "ValueError", "KeyError", "TypeError", "Exception", "True", "False",
                 "None")
    if (name in __builtins__ if isinstance(__builtins__, dict) else hasattr(__builtins__, name))
}


def _lockdown(cpu_seconds: int, memory_mb: int) -> list[str]:
    notes = []
    import socket

    def _no_network(*a, **k):
        raise PermissionError("network access is disabled in the query sandbox")

    socket.socket = _no_network            # type: ignore[assignment]
    socket.create_connection = _no_network  # type: ignore[assignment]
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        try:
            limit = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except (ValueError, OSError):
            notes.append("memory limit not enforceable on this platform")
    except ImportError:
        notes.append("resource limits unavailable on this platform")
    return notes


def _plain(v):
    """Convert a cell to a JSON-friendly Python value."""
    import numpy as np
    import pandas as pd
    if v is None:
        return None
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(v, int):
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else (v.date().isoformat() if v == v.normalize() else v.isoformat())
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if v is pd.NaT:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return str(v)


def _normalise(out) -> dict:
    import pandas as pd
    if not isinstance(out, dict) or "result" not in out:
        raise TypeError("run() must return a dict with keys 'result', 'supporting_invoice_numbers', "
                        "'aggregations', 'notes'")
    res = out["result"]
    if isinstance(res, pd.Series):
        res = res.to_frame(name=res.name or "value")
    elif isinstance(res, dict):
        res = pd.DataFrame([res])
    elif not isinstance(res, pd.DataFrame):
        res = pd.DataFrame({"value": [res]})
    if not isinstance(res.index, pd.RangeIndex):
        # An unnamed integer index is just leftover row positions (e.g. after sort_values): drop it.
        unnamed_int = all(n is None for n in res.index.names) and pd.api.types.is_integer_dtype(res.index)
        res = res.reset_index(drop=unnamed_int)
    res.columns = [str(c) for c in res.columns]
    if len(set(res.columns)) != len(res.columns):
        raise ValueError(f"result has duplicate column names: {list(res.columns)}")
    truncated = len(res) > MAX_RESULT_ROWS
    res = res.head(MAX_RESULT_ROWS)
    rows = [[_plain(v) for v in r] for r in res.itertuples(index=False, name=None)]

    sup = out.get("supporting_invoice_numbers", [])
    if isinstance(sup, (pd.Series, pd.Index)):
        sup = sup.tolist()
    if not isinstance(sup, (list, tuple, set)):
        raise TypeError("'supporting_invoice_numbers' must be a list of invoice numbers")
    aggs = out.get("aggregations", [])
    if not isinstance(aggs, list) or not all(isinstance(a, dict) for a in aggs):
        raise TypeError("'aggregations' must be a list of dicts")
    notes = out.get("notes", [])
    return {
        "columns": list(res.columns),
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "supporting_invoice_numbers": [str(s) for s in sup],
        "aggregations": [{str(k): _plain(v) if not isinstance(v, list) else [str(x) for x in v]
                          for k, v in a.items()} for a in aggs],
        "notes": [str(n) for n in notes] if isinstance(notes, list) else [str(notes)],
    }


def _make_convert(rates, log: list):
    from app.fx.convert import convert, evidence, required_legs, select_observation

    def convert_amount(amount, from_currency, to_currency, on_date, exact_date=False):
        """Application-computed ECB reference-rate conversion. Returns float rounded to cents, or None if no rate."""
        import pandas as pd
        d = pd.Timestamp(on_date).date()
        legs = required_legs(str(from_currency), str(to_currency))
        sel = select_observation(rates, legs, d, exact_date=bool(exact_date))
        out = convert(Decimal(str(amount)), str(from_currency), str(to_currency), sel)
        ev = evidence(sel, rates, str(from_currency), str(to_currency))
        ev["amount"] = str(Decimal(str(amount)))
        ev["result"] = str(out[1]) if out else None
        log.append(ev)
        return float(out[1]) if out else None

    return convert_amount


def run_job(job: dict) -> dict:
    """Run the job in this (child) process and return a JSON-serialisable dict."""
    try:
        notes = _lockdown(job["cpu_seconds"], job["memory_mb"])
        import numpy as np
        import pandas as pd
        outputs = []
        for i, variant in enumerate(job["variants"]):
            convert_log: list = []
            env = {"__builtins__": SAFE_BUILTINS, "pd": pd, "np": np,
                   "convert_amount": _make_convert(job["rates"], convert_log)}
            try:
                exec(compile(job["code"], "<generated>", "exec"), env)
                fn = env.get("run")
                if not callable(fn):
                    raise NameError("the code must define a function named run")
                frames = {k: v.copy() for k, v in job["frames"].items()}
                frames["invoices"] = variant.copy()
                out = _normalise(fn(**frames))
            except Exception as e:
                if i == 0:
                    raise          # the primary run failing is a real error
                outputs.append({"probe_error": f"{type(e).__name__}: {str(e)[:200]}"})
                continue
            out["convert_calls"] = convert_log
            outputs.append(out)
        return {"ok": True, "outputs": outputs, "sandbox_notes": notes}
    except BaseException as e:  # report everything, including KeyError / RecursionError / MemoryError
        import traceback
        tb = traceback.extract_tb(e.__traceback__)
        line = next((f.lineno for f in reversed(tb) if f.filename == "<generated>"), None)
        return {"ok": False, "error_type": type(e).__name__, "error": str(e)[:500], "line": line}


def main() -> None:
    """stdin: pickled job from the (trusted) parent. stdout: one JSON document.

    Output is JSON rather than pickle so nothing the generated code produces can execute in the parent.
    Anything the code prints goes to stderr so it cannot corrupt the result.
    """
    import json
    import pickle
    import sys
    out = sys.stdout
    sys.stdout = sys.stderr
    job = pickle.load(sys.stdin.buffer)
    result = run_job(job)
    out.write(json.dumps(result, default=str))
    out.flush()


if __name__ == "__main__":
    main()
