"""Static validation and isolated execution of GPT-generated pandas code."""
from __future__ import annotations

import ast
import json
import os
import pickle
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

BACKEND_ROOT = Path(__file__).resolve().parents[2]

MAX_CODE_CHARS = 8000
REQUIRED_SIGNATURE = ("invoices", "vendors", "fx_refs", "rates")

FORBIDDEN_NAMES = {
    "eval", "exec", "open", "compile", "__import__", "getattr", "setattr", "delattr", "globals", "locals",
    "vars", "input", "breakpoint", "exit", "quit", "help", "memoryview", "type", "object", "super",
    "classmethod", "staticmethod", "property", "dir", "id", "hash", "os", "sys", "subprocess", "socket",
    "pathlib", "shutil", "importlib", "builtins", "pickle", "requests", "httpx", "urllib",
}
FORBIDDEN_ATTRS = {
    "eval", "query", "to_csv", "to_pickle", "to_parquet", "to_excel", "to_json", "to_sql", "to_hdf",
    "to_feather", "to_stata", "to_clipboard", "to_html", "to_latex", "to_markdown", "to_xml", "to_orc",
    "system", "popen", "remove", "unlink", "rmdir", "mkdir", "write", "load", "loads", "dump", "dumps",
    "io", "api", "options", "plotting", "testing", "util", "compat", "core", "errors", "set_option",
    "lib", "ctypeslib", "f2py", "distutils", "fromfile", "tofile", "save", "savez", "savetxt", "loadtxt",
    "genfromtxt", "memmap",
}
NAME_CREATING_KWARGS = {"name", "names", "value_name", "var_name", "columns", "new_name"}
COLUMN_ARG_METHODS = {"groupby", "sort_values", "nlargest", "nsmallest", "set_index", "drop_duplicates"}


class CodeRejected(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def _str_consts(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [s for e in node.elts for s in _str_consts(e)]
    return []


def validate(code: str, known_columns: set[str]) -> list[str]:
    """Return a list of problems (empty = acceptable)."""
    if len(code) > MAX_CODE_CHARS:
        return [f"code is longer than {MAX_CODE_CHARS} characters"]
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"syntax error on line {e.lineno}: {e.msg}"]

    problems: list[str] = []
    run_defs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run"]
    if len(run_defs) != 1:
        problems.append("define exactly one top-level function named run")
    else:
        args = tuple(a.arg for a in run_defs[0].args.args)
        if args != REQUIRED_SIGNATURE:
            problems.append(f"run must take exactly ({', '.join(REQUIRED_SIGNATURE)}), got ({', '.join(args)})")
    for n in tree.body:
        if not isinstance(n, (ast.FunctionDef, ast.Assign)) and not (
                isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)):
            problems.append(f"line {n.lineno}: only function definitions and constants are allowed at top level")

    created: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            problems.append(f"line {node.lineno}: imports are not allowed (pd and np are provided)")
        elif isinstance(node, (ast.While, ast.Global, ast.Nonlocal, ast.ClassDef, ast.AsyncFunctionDef,
                               ast.Await, ast.Yield, ast.YieldFrom, ast.With, ast.AsyncWith, ast.Try,
                               ast.Raise, ast.Delete)):
            problems.append(f"line {node.lineno}: '{type(node).__name__}' statements are not allowed")
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES or node.id.startswith("__"):
                problems.append(f"line {node.lineno}: name '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                problems.append(f"line {node.lineno}: private attribute '{node.attr}' is not allowed")
            elif node.attr in FORBIDDEN_ATTRS or node.attr.startswith("read_"):
                problems.append(f"line {node.lineno}: '.{node.attr}' is not allowed")
            if isinstance(node.ctx, ast.Store) and node.attr == "columns":
                pass
        # Collect names the code itself creates, so they are not flagged as unknown columns.
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            created.update(_str_consts(node.slice))
        elif isinstance(node, ast.keyword):
            if node.arg:
                created.add(node.arg)
            if node.arg in NAME_CREATING_KWARGS:
                created.update(_str_consts(node.value))
                if isinstance(node.value, ast.Dict):
                    created.update(s for v in node.value.values for s in _str_consts(v))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute) and t.attr == "columns":
                    created.update(_str_consts(node.value))
        elif isinstance(node, ast.Dict):
            created.update(s for v in node.values if v is not None for s in _str_consts(v))

    if problems:
        return sorted(set(problems), key=problems.index)

    allowed = known_columns | created
    unknown: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        refs: list[str] = []
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            is_label_lookup = isinstance(node.value, ast.Attribute) and node.value.attr in ("loc", "iloc", "at", "iat")
            refs = [] if is_label_lookup else _str_consts(node.slice)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in COLUMN_ARG_METHODS:
            refs = [s for a in node.args for s in _str_consts(a)]
            refs += [s for k in node.keywords if k.arg in ("by", "columns", "keys", "subset")
                     for s in _str_consts(k.value)]
        for r in refs:
            if r not in allowed:
                unknown.append((node.lineno, r))
    for line, col in dict.fromkeys(unknown):
        problems.append(f"line {line}: column '{col}' does not exist")
    return problems


@dataclass
class ExecResult:
    ok: bool
    outputs: list[dict] = field(default_factory=list)   # one per invoices variant (primary first)
    error_type: str | None = None
    error: str | None = None
    line: int | None = None
    sandbox_notes: list[str] = field(default_factory=list)

    def describe_error(self) -> str:
        where = f" (line {self.line})" if self.line else ""
        return f"{self.error_type}{where}: {self.error}"


def execute(code: str, frames: dict[str, pd.DataFrame], variants: list[pd.DataFrame], rates,
            timeout_s: float = 15.0, cpu_seconds: int = 10, memory_mb: int = 2048) -> ExecResult:
    """Run ``code`` once per invoices variant in a fresh Python subprocess."""
    job = pickle.dumps({"code": code, "frames": frames, "variants": variants, "rates": rates,
                        "cpu_seconds": cpu_seconds, "memory_mb": memory_mb})
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(BACKEND_ROOT), "PYTHONHASHSEED": "0"}
    try:
        proc = subprocess.run([sys.executable, "-m", "app.agent.sandbox_worker"], input=job,
                              capture_output=True, timeout=timeout_s, cwd=str(BACKEND_ROOT), env=env)
    except subprocess.TimeoutExpired:
        return ExecResult(False, error_type="Timeout", error=f"query did not finish within {timeout_s:.0f} s")
    try:
        msg = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        tail = proc.stderr.decode("utf-8", "replace")[-300:]
        return ExecResult(False, error_type="Crashed",
                          error=f"query process exited with code {proc.returncode}: {tail}")
    if not msg.get("ok"):
        return ExecResult(False, error_type=msg.get("error_type"), error=msg.get("error"), line=msg.get("line"))
    return ExecResult(True, outputs=msg["outputs"], sandbox_notes=msg.get("sandbox_notes", []))
