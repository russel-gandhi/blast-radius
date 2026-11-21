"""Generate patches that change one class-level setting (bool / number / string / one dict value).

usage: python attrmut.py <sqlglot> <out> <count> <seed>
"""

import ast
import difflib
import json
import os
import random
import sys

src, out, count, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed)
os.makedirs(f"{out}/commits", exist_ok=True)

cands = []
for root, _, files in os.walk(os.path.join(src, "sqlglot")):
    for fn in files:
        if not fn.endswith(".py"):
            continue
        path = os.path.relpath(os.path.join(root, fn), src).replace("\\", "/")
        text = open(os.path.join(src, path), encoding="utf-8", newline="").read()
        tree = ast.parse(text)
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            for st in cls.body:
                if not (isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name)):
                    continue
                v = st.value
                consts = []
                if isinstance(v, ast.Constant):
                    consts = [v]
                elif isinstance(v, ast.Dict):
                    consts = [x for x in v.values if isinstance(x, ast.Constant)]
                elif isinstance(v, (ast.Tuple, ast.List, ast.Set)):
                    consts = [x for x in v.elts if isinstance(x, ast.Constant)]
                for c in consts:
                    if c.lineno == c.end_lineno and isinstance(c.value, (bool, int, str)):
                        cands.append((path, st.targets[0].id, c.lineno, c.col_offset, c.end_col_offset, c.value))
rng.shuffle(cands)

made, kinds = 0, {}
used = set()
for path, name, ln, c1, c2, val in cands:
    if made >= count or (path, name) in used:
        continue
    if isinstance(val, bool):
        new = repr(not val)
    elif isinstance(val, int):
        new = repr(val + 1)
    else:
        new = repr(val + "X" if val else "X")
    text = open(os.path.join(src, path), encoding="utf-8", newline="").read()
    lines = text.split("\n")
    b = lines[ln - 1].encode("utf-8")
    lines[ln - 1] = (b[:c1] + new.encode("utf-8") + b[c2:]).decode("utf-8")
    newtext = "\n".join(lines)
    try:
        ast.parse(newtext)
    except SyntaxError:
        continue
    d = "".join(difflib.unified_diff(text.splitlines(True), newtext.splitlines(True), f"a/{path}", f"b/{path}", n=3))
    cid = f"a{seed % 100:02d}{made:03d}"
    open(f"{out}/commits/{cid}.patch", "w", encoding="utf-8", newline="\n").write(f"diff --git a/{path} b/{path}\n" + d)
    kinds[cid] = {"kind": f"attr_{type(val).__name__}", "file": path, "name": name}
    used.add((path, name))
    made += 1
json.dump(kinds, open(f"{out}/kinds.json", "w"), indent=1)
print("made", made)
