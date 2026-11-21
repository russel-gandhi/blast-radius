"""Hand-made patches for risky kinds: node counting, sqlglotc build, planner, executor codegen, DuckDB generator."""

import difflib
import json
import os
import sys

src, out = sys.argv[1], sys.argv[2]
os.makedirs(f"{out}/commits", exist_ok=True)

EDITS = [
    ("h01", "node_count_ge", "sqlglot/parser.py", "if self._node_count > self.max_nodes:", "if self._node_count >= self.max_nodes:"),
    ("h02", "node_count_step", "sqlglot/parser.py", "self._node_count += 1", "self._node_count += 2"),
    ("h03", "sqlglotc_setup", "sqlglotc/setup.py", 'if f.endswith(".py") and f != "__init__.py"', 'if f.endswith(".py")'),
    ("h04", "planner_distinct", "sqlglot/planner.py", 'if isinstance(expression, exp.Select) and expression.args.get("distinct"):', 'if isinstance(expression, exp.Select) and not expression.args.get("distinct"):'),
    ("h05", "planner_limit", "sqlglot/planner.py", "if limit is not None and not limit.is_limit_all:", "if limit is not None:"),
    ("h06", "duckdb_struct", "sqlglot/generators/duckdb.py", None, None),
    ("h07", "planner_agg_name", "sqlglot/planner.py", "name = intermediate.get(node) or intermediate.get(node.name)", "name = intermediate.get(node.name) or intermediate.get(node)"),
    ("h08", "comment_in_string", None, None, None),
]


def diff(rel, old, new):
    d = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
    return f"diff --git a/{rel} b/{rel}\n" + d


kinds = {}
for cid, kind, rel, a, b in EDITS:
    if kind == "duckdb_struct":
        rel = "sqlglot/generators/duckdb.py"
        old = open(os.path.join(src, rel), encoding="utf-8", newline="").read()
        # break a common DuckDB rendering: date/interval arithmetic and casts used by TPC queries
        i = old.find("def _date_delta_sql")
        if i < 0:
            i = old.find("INTERVAL")
        new = old.replace('"INTERVAL', '"INTERVAL ', 1) if '"INTERVAL' in old else old.replace("INTERVAL", "INTERVAL ", 1)
    elif kind == "comment_in_string":
        # a "#" line inside a multi-line SQL string in a test helper: string content, not a comment
        rel = "tests/helpers.py"
        old = open(os.path.join(src, rel), encoding="utf-8", newline="").read()
        j = old.find('"""', old.find("TPCH_SCHEMA") if "TPCH_SCHEMA" in old else 0)
        new = old
        k = old.find('"""\n')
        if k >= 0:
            new = old[: k + 4] + "# not a comment\n" + old[k + 4:]
    else:
        old = open(os.path.join(src, rel), encoding="utf-8", newline="").read()
        assert a in old, (cid, a)
        new = old.replace(a, b, 1)
    if new == old:
        print("no change", cid)
        continue
    open(f"{out}/commits/{cid}.patch", "w", encoding="utf-8", newline="\n").write(diff(rel, old, new))
    kinds[cid] = {"kind": kind, "file": rel}
json.dump(kinds, open(f"{out}/kinds.json", "w"), indent=1)
print(len(kinds), "patches")
