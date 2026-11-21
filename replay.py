"""Fingerprints for sqlglot's slow TPC-H / TPC-DS executor tests.

test_execute_tpch / test_execute_tpcds run every fixture query through sqlglot's Python executor
and compare the rows with DuckDB running the query transpiled to DuckDB SQL. Their outcome is fixed
by, per query:
  - the DuckDB SQL (transpile(sql, write="duckdb")),
  - what the executor runs: the query parsed, pickled to the pool and back, optimized with the
    test's schema, planned, and every plan step's expressions turned into Python code,
  - the executor's runtime code and the data, which a replay only trusts when the patch leaves
    them alone (see RUNTIME_FILES in the selector).
test_optimized_tpch compares DuckDB on the query and on the fixture's optimized SQL, so its
fingerprint is the two DuckDB transpilations.

If every fingerprint equals the one recorded on the pinned commit, the test behaves as it did there,
where it passed. The fingerprint is computed in the child process of verify.py against the patched
tree; `python replay.py record` writes the reference digests.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import sys

TESTS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpch": ("tpch", "execute"),
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": ("tpcds", "execute"),
    "tests/test_executor.py::TestExecutor::test_optimized_tpch": ("tpch", "optimized"),
}


def _expr_repr(e) -> str:
    from sqlglot import exp
    from sqlglot.executor.python import Python

    parts = [type(e).__name__, e.sql()]
    try:
        parts.append(Python().generate(e))
    except Exception as x:  # noqa: BLE001
        parts.append("GENERR " + type(x).__name__ + str(x))
    types = []
    for node in e.walk():
        t = getattr(node, "type", None) if isinstance(node, exp.Expr) else None
        if t is not None:
            types.append(t.sql())
    parts.append(",".join(types))
    return "|".join(parts)


def _val(v, depth=0) -> str:
    from sqlglot import exp

    if depth > 6:
        return "..."
    if isinstance(v, exp.Expr):
        return _expr_repr(v)
    if isinstance(v, (list, tuple, set, frozenset)):
        items = [_val(x, depth + 1) for x in v]
        if isinstance(v, (set, frozenset)):
            items.sort()
        return type(v).__name__ + "[" + ";".join(items) + "]"
    if isinstance(v, dict):
        return "{" + ";".join(f"{_val(k, depth + 1)}:{_val(x, depth + 1)}" for k, x in v.items()) + "}"
    if hasattr(v, "dependencies") and hasattr(v, "name"):  # another plan step: by name
        return "step:" + str(v.name)
    return type(v).__name__ + ":" + repr(v)


def _plan_repr(plan) -> str:
    steps = []
    seen = set()
    stack = [plan.root]
    while stack:
        s = stack.pop()
        if id(s) in seen:
            continue
        seen.add(id(s))
        attrs = []
        for k in sorted(vars(s)):
            if k in ("dependencies", "dependents"):
                attrs.append(k + "=" + ",".join(sorted(str(d.name) for d in getattr(s, k))))
                continue
            attrs.append(k + "=" + _val(getattr(s, k)))
        steps.append(type(s).__name__ + "(" + " ".join(attrs) + ")")
        stack.extend(sorted(s.dependencies, key=lambda d: str(d.name)))
    return "\n".join(sorted(steps))  # traversal order of same-named steps varies


def _digest(parts) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", "surrogatepass"))
        h.update(b"\x00")
    return h.hexdigest()[:24]


def fingerprints(kind: str, which: str) -> list[str]:
    """One digest per fixture query of the given test, computed with whatever sqlglot is imported."""
    return list(iter_fingerprints(kind, which))


def matches(kind: str, which: str, ref: list[str]) -> bool:
    """True when every query's digest equals the reference; stops at the first difference."""
    n = 0
    for n, (got, want) in enumerate(zip(iter_fingerprints(kind, which), ref), start=1):
        if got != want:
            return False
    return n == len(ref)


def iter_fingerprints(kind: str, which: str):
    from sqlglot import parse_one, transpile
    from sqlglot.optimizer import optimize
    from sqlglot.planner import Plan
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs

    schema = TPCH_SCHEMA if kind == "tpch" else TPCDS_SCHEMA
    path = "optimizer/tpc-h/tpc-h.sql" if kind == "tpch" else "optimizer/tpc-ds/tpc-ds.sql"
    for meta, sql, optimized in load_sql_fixture_pairs(path):
        parts = [sql, json.dumps(meta, sort_keys=True)]
        try:
            parts.append(transpile(sql, write="duckdb")[0])
            if which == "optimized":
                parts.append(transpile(optimized, write="duckdb")[0])
            elif meta.get("execute"):
                e = pickle.loads(pickle.dumps(parse_one(sql)))
                from sqlglot.schema import ensure_schema

                o = optimize(e, ensure_schema(schema), leave_tables_isolated=True)
                parts.append(o.sql())
                parts.append(_plan_repr(Plan(o)))
        except Exception as x:  # noqa: BLE001
            parts.append("ERR " + type(x).__name__ + " " + str(x))
        yield _digest(parts)


def all_fingerprints() -> dict[str, list[str]]:
    return {tid: fingerprints(*spec) for tid, spec in TESTS.items()}


if __name__ == "__main__" and len(sys.argv) > 2 and sys.argv[1] == "record":
    data = all_fingerprints()
    data2 = all_fingerprints()
    assert data == data2, "fingerprints are not deterministic"
    json.dump(data, open(sys.argv[2], "w"), indent=0)
    print({k: len(v) for k, v in data.items()})
