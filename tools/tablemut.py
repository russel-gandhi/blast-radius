"""Generate patches that change one entry of a SQL-keyed table (FUNCTIONS, KEYWORDS, *_PARSERS,
*_MAPPING, TRANSFORMS): a string value, a string key, or swap a value with another entry's.

usage: python tablemut.py <sqlglot> <out> <count> <seed>
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
TABLE = re.compile(r"^(FUNCTIONS|KEYWORDS|TRANSFORMS|[A-Z_]*_(FUNCTIONS|PARSERS|MAPPING|KEYWORDS))$")

cands = []
for root, _, files in os.walk(os.path.join(src, "sqlglot")):
    for fn in files:
        if not fn.endswith(".py"):
            continue
        path = os.path.relpath(os.path.join(root, fn), src).replace("\\", "/")
        text = open(os.path.join(src, path), encoding="utf-8", newline="").read()
        for cls in ast.walk(ast.parse(text)):
            if not isinstance(cls, ast.ClassDef):
                continue
            for st in cls.body:
                if not (isinstance(st, (ast.Assign, ast.AnnAssign)) and isinstance(st.value, ast.Dict)):
                    continue
                t = st.targets[0] if isinstance(st, ast.Assign) else st.target
                if not (isinstance(t, ast.Name) and TABLE.match(t.id)):
                    continue
                d = st.value
                pairs = [(k, v) for k, v in zip(d.keys, d.values) if k is not None]
                for k, v in pairs:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str) and v.lineno == v.end_lineno:
                        cands.append(("value", path, t.id, v, None))
                    if isinstance(k, ast.Constant) and isinstance(k.value, str) and k.lineno == k.end_lineno:
                        cands.append(("key", path, t.id, k, None))
                one_line = [(k, v) for k, v in pairs if v.lineno == v.end_lineno and k.lineno == v.lineno]
                if len(one_line) >= 2:
                    a, b = rng.sample(one_line, 2)
                    cands.append(("swap", path, t.id, a[1], b[1]))
rng.shuffle(cands)


def seg(lines, n):
    return lines[n.lineno - 1].encode("utf-8")[n.col_offset:n.end_col_offset].decode("utf-8")


made, kinds, used = 0, {}, set()
for kind, path, name, node, other in cands:
    if made >= count or (path, name) in used:
        continue
    text = open(os.path.join(src, path), encoding="utf-8", newline="").read()
    lines = text.split("\n")
    edits = []
    if kind in ("value", "key"):
        v = node.value
        edits.append((node, repr(v + "X") if v else "'X'"))
    else:
        edits.append((node, seg(lines, other)))
        edits.append((other, seg(lines, node)))
    edits.sort(key=lambda e: (e[0].lineno, e[0].col_offset), reverse=True)
    new_lines = list(lines)
    ok = True
    for n, rep in edits:
        b = new_lines[n.lineno - 1].encode("utf-8")
        new_lines[n.lineno - 1] = (b[:n.col_offset] + rep.encode("utf-8") + b[n.end_col_offset:]).decode("utf-8")
    new = "\n".join(new_lines)
    if new == text:
        continue
    try:
        ast.parse(new)
    except SyntaxError:
        continue
    d = "".join(difflib.unified_diff(text.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}", n=3))
    cid = f"t{seed % 100:02d}{made:03d}"
    open(f"{out}/commits/{cid}.patch", "w", encoding="utf-8", newline="\n").write(f"diff --git a/{path} b/{path}\n" + d)
    kinds[cid] = {"kind": f"table_{kind}", "file": path, "name": name}
    used.add((path, name))
    made += 1
json.dump(kinds, open(f"{out}/kinds.json", "w"), indent=1)
print("made", made)
