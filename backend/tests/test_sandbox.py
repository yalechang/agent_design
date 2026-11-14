import pandas as pd
import pytest

from app.agent.sandbox import execute, validate
from app.fx.convert import RateTable

KNOWN = {"invoice_number", "amount", "currency", "vendor_id", "amount_eur"}
OK = '''
def run(invoices, vendors, fx_refs, rates):
    t = invoices.groupby("currency", as_index=False).agg(total=("amount", "sum"))
    return {"result": t, "supporting_invoice_numbers": invoices["invoice_number"].tolist(),
            "aggregations": [], "notes": []}
'''


def frames():
    inv = pd.DataFrame({"invoice_number": ["A", "B"], "amount": [1.5, 2.0], "currency": ["EUR", "EUR"],
                        "vendor_id": ["V", "V"], "amount_eur": [1.5, 2.0]})
    return {"vendors": pd.DataFrame(), "fx_refs": pd.DataFrame(), "rates": pd.DataFrame()}, inv


def test_valid_code_passes():
    assert validate(OK, KNOWN) == []


@pytest.mark.parametrize("snippet,expect", [
    ("import os", "imports are not allowed"),
    ("from os import path", "imports are not allowed"),
    ("x = open('f')", "name 'open'"),
    ("x = __import__('os')", "name '__import__'"),
    ("x = invoices.__class__", "private attribute"),
    ("x = invoices._data", "private attribute"),
    ("invoices.to_csv('x')", "'.to_csv'"),
    ("x = pd.read_csv('x')", "'.read_csv'"),
    ("x = invoices.query('amount > 1')", "'.query'"),
    ("x = invoices.eval('a+b')", "'.eval'"),
    ("x = pd.io", "'.io'"),
    ("x = getattr(invoices, 'a')", "name 'getattr'"),
    ("while True:\n        pass", "'While'"),
    ("try:\n        x = 1\n    except Exception:\n        pass", "'Try'"),
    ("x = invoices['region']", "column 'region' does not exist"),
    ("x = invoices.groupby('region')", "column 'region' does not exist"),
])
def test_rejected(snippet, expect):
    code = f"def run(invoices, vendors, fx_refs, rates):\n    {snippet}\n    return {{'result': 1}}"
    problems = validate(code, KNOWN)
    assert any(expect in p for p in problems), problems


def test_created_columns_are_allowed():
    code = '''
def run(invoices, vendors, fx_refs, rates):
    invoices["month"] = invoices["invoice_number"]
    g = invoices.groupby("month", as_index=False).agg(total_eur=("amount_eur", "sum"))
    g = g.rename(columns={"total_eur": "spend"})
    return {"result": g[["month", "spend"]], "supporting_invoice_numbers": [], "aggregations": [], "notes": []}
'''
    assert validate(code, KNOWN) == []


@pytest.mark.parametrize("code,expect", [
    ("def run(a, b, c, d):\n    return 1", "run must take exactly"),
    ("def go(invoices, vendors, fx_refs, rates):\n    return 1", "exactly one top-level function named run"),
    ("def run(invoices, vendors, fx_refs, rates)\n    return 1", "syntax error"),
    ("x" * 9000, "longer than"),
])
def test_structure(code, expect):
    assert any(expect in p for p in validate(code, KNOWN))


def test_execute_ok_and_normalised():
    f, inv = frames()
    r = execute(OK, f, [inv], RateTable({}))
    assert r.ok, r.error
    o = r.outputs[0]
    assert o["columns"] == ["currency", "total"] and o["rows"] == [["EUR", 3.5]]
    assert o["supporting_invoice_numbers"] == ["A", "B"]


def test_execute_runtime_error_reports_line():
    f, inv = frames()
    r = execute("def run(invoices, vendors, fx_refs, rates):\n    x = 1\n    return invoices.loc['zz']", f, [inv],
                RateTable({}))
    assert not r.ok and r.error_type == "KeyError" and r.line == 3


def test_execute_timeout():
    f, inv = frames()
    r = execute("def run(invoices, vendors, fx_refs, rates):\n    return {'result': sum(range(10**12))}",
                f, [inv], RateTable({}), timeout_s=2)
    assert not r.ok and r.error_type == "Timeout"


def test_restricted_builtins_at_runtime():
    # Even if static checks were bypassed, the sandbox has no __import__/open.
    f, inv = frames()
    r = execute("def run(invoices, vendors, fx_refs, rates):\n    return {'result': open('/etc/hosts').read()}",
                f, [inv], RateTable({}))
    assert not r.ok and r.error_type == "NameError"


def test_network_blocked_at_runtime():
    f, inv = frames()
    code = ("def run(invoices, vendors, fx_refs, rates):\n"
            "    return {'result': pd.read_csv('https://example.com/x.csv')}")
    r = execute(code, f, [inv], RateTable({}))
    assert not r.ok


def test_bad_return_shape():
    f, inv = frames()
    r = execute("def run(invoices, vendors, fx_refs, rates):\n    return [1, 2]", f, [inv], RateTable({}))
    assert not r.ok and "must return a dict" in r.error


def test_printing_does_not_corrupt_output():
    f, inv = frames()
    code = OK.replace('    t = ', '    print("noise")\n    t = ')
    # print is not in the safe builtins: rejected at runtime with NameError, never corrupts JSON
    r = execute(code, f, [inv], RateTable({}))
    assert not r.ok and r.error_type == "NameError"


def test_probe_failure_is_isolated():
    f, inv = frames()
    probe = inv.copy()
    probe["amount"] = "x"   # makes the sum fail only in the probe
    code = OK.replace('agg(total=("amount", "sum"))', 'agg(total=("amount", "mean"))')
    r = execute(code, f, [inv, probe], RateTable({}))
    assert r.ok and "probe_error" in r.outputs[1]
