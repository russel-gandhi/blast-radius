"""Generate varied synthetic patches against pinned sqlglot, to stress the selector beyond the sample kinds.

usage: python mutate.py <sqlglot checkout> <out dir> <count> <seed>
Writes <out>/commits/<id>.patch and <out>/kinds.json.
"""

import ast
import difflib
import json
import os
import random
import re
import sys

src, out, count, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed)
os.makedirs(f"{out}/commits", exist_ok=True)


def read(rel):
    with open(os.path.join(src, rel), encoding="utf-8", newline="") as f:
        return f.read()


def files(prefix, ext):
    res = []
    for root, _, fs in os.walk(os.path.join(src, prefix)):
        for f in fs:
            if f.endswith(ext):
                res.append(os.path.relpath(os.path.join(root, f), src).replace("\\", "/"))
    return sorted(res)


PY = [f for f in files("sqlglot", ".py")]
DIALECTISH = [f for f in PY if re.search(r"/(dialects|parsers|generators|tokenizers|typing)/", f)]
CORE = [f for f in PY if f not in DIALECTISH]
FIXTURES = files("tests/fixtures", ".sql")
TESTS = files("tests", ".py")


def diff(rel, old, new):
    return "".join(
        difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}", n=3)
    ).replace(f"--- a/{rel}", f"diff --git a/{rel} b/{rel}\n--- a/{rel}", 1)


def func_lines(text):
    tree = ast.parse(text)
    res = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for st in ast.walk(node):
                if isinstance(st, ast.stmt) and st is not node:
                    res.append(st.lineno)
    return sorted(set(res))


def class_level_lines(text):
    tree = ast.parse(text)
    res = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for st in node.body:
                if isinstance(st, (ast.Assign, ast.AnnAssign)):
                    res.extend(range(st.lineno, (st.end_lineno or st.lineno) + 1))
    return res


OPS = [
    (r" == ", " != "), (r" != ", " == "), (r" and ", " or "), (r" or ", " and "), (r" < ", " <= "),
    (r" > ", " >= "), (r" <= ", " < "), (r" >= ", " > "), (r" is not ", " is "), (r" is None", " is not None"),
    (r" not ", " "), (r" \+ 1", " + 2"), (r" - 1", " - 2"), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"),
    (r"\[0\]", "[1]"), (r"\[-1\]", "[0]"), (r"return ", "return None or "),
]


def m_op(rel, lines_fn):
    text = read(rel)
    lines = text.split("\n")
    cands = []
    for ln in lines_fn(text):
        l = lines[ln - 1]
        if l.strip().startswith("#"):
            continue
        for a, b in OPS:
            if re.search(a, l):
                cands.append((ln, a, b))
    if not cands:
        return None
    ln, a, b = rng.choice(cands)
    lines[ln - 1] = re.sub(a, b, lines[ln - 1], count=1)
    return "\n".join(lines)


def m_string(rel, lines_fn):
    text = read(rel)
    lines = text.split("\n")
    cands = [ln for ln in lines_fn(text) if re.search(r'"[A-Z_ ]{2,}"', lines[ln - 1])]
    if not cands:
        return None
    ln = rng.choice(cands)
    lines[ln - 1] = re.sub(r'"([A-Z_ ]{2,})"', lambda m: '"' + m.group(1) + 'X"', lines[ln - 1], count=1)
    return "\n".join(lines)


def m_comment(rel):
    text = read(rel)
    lines = text.split("\n")
    ln = rng.choice(func_lines(text) or [1])
    indent = re.match(r"\s*", lines[ln - 1]).group(0)
    lines.insert(ln - 1, indent + rng.choice(["# NOTE: keep in sync", "# TODO revisit", "# fast path"]))
    return "\n".join(lines)


def m_delete_line(rel):
    text = read(rel)
    lines = text.split("\n")
    tree = ast.parse(text)
    cands = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef,)):
            body = node.body
            for st in body[1:]:
                if isinstance(st, (ast.Assign, ast.Expr, ast.AugAssign)) and st.lineno == st.end_lineno:
                    cands.append(st.lineno)
    if not cands:
        return None
    ln = rng.choice(cands)
    indent = re.match(r"\s*", lines[ln - 1]).group(0)
    lines[ln - 1] = indent + "pass"
    return "\n".join(lines)


def m_syntax(rel):
    text = read(rel)
    lines = text.split("\n")
    ln = rng.choice(func_lines(text))
    lines[ln - 1] = lines[ln - 1] + " ("
    return "\n".join(lines)


def m_default(rel):
    text = read(rel)
    m = list(re.finditer(r"(def \w+\([^)]*?=\s*)(True|False|None|\d+)", text))
    if not m:
        return None
    x = rng.choice(m)
    rep = {"True": "False", "False": "True", "None": "0"}.get(x.group(2), str(int(x.group(2) + "0") // 10 + 1) if x.group(2).isdigit() else "1")
    return text[: x.start(2)] + rep + text[x.end(2):]


def m_docstring(rel):
    text = read(rel)
    i = text.find('"""', text.find("def "))
    if i < 0:
        return None
    return text[: i + 3] + "Note: " + text[i + 3:]


def m_fixture(rel, mode):
    text = read(rel)
    lines = text.split("\n")
    idx = [i for i, l in enumerate(lines) if l.strip() and not l.startswith("#") and l.rstrip().endswith(";")]
    if not idx:
        return None
    i = rng.choice(idx)
    if mode == "expected":
        lines[i] = lines[i].replace(" ", "  ", 1) if rng.random() < 0.3 else lines[i][:-1] + " AS zz;" if " AS " not in lines[i] else lines[i].replace(" AS ", " AS q", 1)
    elif mode == "comment":
        lines.insert(i, "# " + rng.choice(["note: tricky", "title: extra", "execute: false", "dialect: duckdb"]))
    elif mode == "blank":
        lines.insert(i, "")
    return "\n".join(lines)


def m_test(rel):
    text = read(rel)
    m = list(re.finditer(r'self\.validate_identity\("([^"\\]{8,})"\)', text))
    if not m:
        m = list(re.finditer(r'self\.assertEqual\(([^\n]{5,})\)', text))
        if not m:
            return None
        x = rng.choice(m)
        return text[: x.start(1)] + x.group(1).replace("'", "'x", 1) if False else text[: x.start()] + "self.assertEqual(" + x.group(1) + ")  # checked" + text[x.end():]
    x = rng.choice(m)
    s = x.group(1)
    return text[: x.start(1)] + s.replace(" ", "  ", 1) + text[x.end(1):]


KINDS = [
    ("op_core", lambda: (r := rng.choice(CORE), m_op(r, func_lines))),
    ("op_dialect", lambda: (r := rng.choice(DIALECTISH), m_op(r, func_lines))),
    ("op_dialect", lambda: (r := rng.choice(DIALECTISH), m_op(r, func_lines))),
    ("op_core", lambda: (r := rng.choice(CORE), m_op(r, func_lines))),
    ("class_attr_dialect", lambda: (r := rng.choice(DIALECTISH), m_op(r, class_level_lines) or m_string(r, class_level_lines))),
    ("class_attr_core", lambda: (r := rng.choice(CORE), m_op(r, class_level_lines) or m_string(r, class_level_lines))),
    ("string", lambda: (r := rng.choice(PY), m_string(r, func_lines))),
    ("comment_py", lambda: (r := rng.choice(PY), m_comment(r))),
    ("delete_line", lambda: (r := rng.choice(PY), m_delete_line(r))),
    ("syntax", lambda: (r := rng.choice(DIALECTISH), m_syntax(r))),
    ("default_arg", lambda: (r := rng.choice(PY), m_default(r))),
    ("docstring", lambda: (r := rng.choice(PY), m_docstring(r))),
    ("fixture_expected", lambda: (r := rng.choice(FIXTURES), m_fixture(r, "expected"))),
    ("fixture_comment", lambda: (r := rng.choice(FIXTURES), m_fixture(r, "comment"))),
    ("fixture_blank", lambda: (r := rng.choice(FIXTURES), m_fixture(r, "blank"))),
    ("test_edit", lambda: (r := rng.choice(TESTS), m_test(r))),
]

kinds = {}
made = 0
tries = 0
while made < count and tries < count * 50:
    tries += 1
    kind, fn = KINDS[made % len(KINDS)] if tries < count * 10 else rng.choice(KINDS)
    try:
        rel, new = fn()
    except Exception:
        continue
    if not new:
        continue
    old = read(rel)
    if new == old:
        continue
    patch = diff(rel, old, new)
    if rng.random() < 0.15:  # sometimes a second, unrelated comment-only change
        r2 = rng.choice(PY)
        if r2 != rel:
            n2 = m_comment(r2)
            patch += diff(r2, read(r2), n2)
            kind += "+comment"
    cid = f"m{seed % 100:02d}{made:03d}"
    with open(f"{out}/commits/{cid}.patch", "w", encoding="utf-8", newline="\n") as f:
        f.write(patch)
    kinds[cid] = {"kind": kind, "file": rel}
    made += 1
json.dump(kinds, open(f"{out}/kinds.json", "w"), indent=1)
print("made", made)
