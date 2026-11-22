"""Test-side patches: test bodies, class setup, class attributes, tests/helpers.py, fixture metadata.

usage: python testmut.py <sqlglot> <out> <seed>
"""

import ast
import difflib
import glob
import json
import os
import random
import re
import sys

src, out, seed = sys.argv[1], sys.argv[2], int(sys.argv[3])
rng = random.Random(seed)
os.makedirs(f"{out}/commits", exist_ok=True)


def read(rel):
    return open(os.path.join(src, rel), encoding="utf-8", newline="").read()


kinds, n = {}, 0


def put(kind, rel, old, new):
    global n
    if new == old:
        return
    try:
        if rel.endswith(".py"):
            ast.parse(new)
    except SyntaxError:
        return
    d = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
    cid = f"t{seed % 100:02d}{n:03d}"
    open(f"{out}/commits/{cid}.patch", "w", encoding="utf-8", newline="\n").write(f"diff --git a/{rel} b/{rel}\n" + d)
    kinds[cid] = {"kind": kind, "file": rel}
    n += 1


def replace_line(text, ln, fn):
    lines = text.split("\n")
    lines[ln - 1] = fn(lines[ln - 1])
    return "\n".join(lines)


tests = sorted(p.replace("\\", "/").split(src.replace("\\", "/") + "/")[-1] for p in glob.glob(os.path.join(src, "tests", "**", "test_*.py"), recursive=True))
tests = [os.path.relpath(p, src).replace("\\", "/") for p in glob.glob(os.path.join(src, "tests", "**", "test_*.py"), recursive=True)]
rng.shuffle(tests)

# 1. string literal inside a test method body (expected SQL)
for rel in tests[:8]:
    text = read(rel)
    cands = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
            for c in ast.walk(node):
                if isinstance(c, ast.Constant) and isinstance(c.value, str) and len(c.value) > 6 and c.lineno == c.end_lineno:
                    cands.append(c)
    if cands:
        c = rng.choice(cands)
        put("test_literal", rel, text, replace_line(text, c.lineno, lambda l: l[:c.col_offset] + repr(c.value + " ") + l[c.end_col_offset:]))

# 2. setUpClass / setUp bodies
for rel in tests:
    text = read(rel)
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef) and node.name in ("setUpClass", "setUp") and len(node.body) > 1:
            st = rng.choice(node.body[1:])
            if st.lineno == st.end_lineno and isinstance(st, ast.Expr):
                put("test_setup", rel, text, replace_line(text, st.lineno, lambda l: re.sub(r"\S.*", "pass", l, count=1)))
                break
    if sum(1 for v in kinds.values() if v["kind"] == "test_setup") >= 4:
        break

# 3. class attributes in test classes
for rel in tests:
    text = read(rel)
    m = re.search(r"^(\s+)maxDiff = None$", text, re.M)
    if m:
        put("test_class_attr", rel, text, text.replace(m.group(0), m.group(1) + "maxDiff = 10", 1))
    if sum(1 for v in kinds.values() if v["kind"] == "test_class_attr") >= 3:
        break

# 4. tests/helpers.py: constants and functions
h = "tests/helpers.py"
text = read(h)
tree = ast.parse(text)
for node in tree.body:
    if isinstance(node, ast.FunctionDef):
        body = [s for s in node.body if s.lineno == s.end_lineno and isinstance(s, (ast.Assign, ast.Return, ast.Expr))]
        if body and rng.random() < 0.8:
            s = rng.choice(body)
            put("helpers_function", h, text, replace_line(text, s.lineno, lambda l: l.replace(".strip()", ".strip(' ')", 1) if ".strip()" in l else l.replace("==", "!=", 1) if "==" in l else l))
for key in ("TPCH_SCHEMA", "TPCDS_SCHEMA"):
    m = re.search(r'"([a-z_]+)": "(INT|DOUBLE|STRING|TEXT|BIGINT|DATE)"', text[text.find(key):])
    if m:
        a = text.find(key) + m.start()
        put("helpers_schema", h, text, text[:a] + m.group(0).replace(m.group(2), "VARCHAR") + text[a + len(m.group(0)):])

# 5. fixture metadata lines
fx = sorted(os.path.relpath(p, src).replace("\\", "/") for p in glob.glob(os.path.join(src, "tests", "fixtures", "optimizer", "*.sql")))
rng.shuffle(fx)
made = 0
for rel in fx:
    text = read(rel)
    m = re.search(r"^# (execute|dialect|leave_tables_isolated|validate_qualify_columns|title): ?(.*)$", text, re.M)
    if not m:
        continue
    k, v = m.group(1), m.group(2)
    nv = {"execute": "false" if v.strip() == "true" else "true", "dialect": "duckdb" if v.strip() != "duckdb" else "snowflake",
          "title": v + " x"}.get(k, "false" if v.strip() == "true" else "true")
    put("fixture_meta", rel, text, text.replace(m.group(0), f"# {k}: {nv}", 1))
    made += 1
    if made >= 6:
        break

json.dump(kinds, open(f"{out}/kinds.json", "w"), indent=1)
import collections
print("made", n, collections.Counter(v["kind"] for v in kinds.values()))
