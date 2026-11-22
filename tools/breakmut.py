"""Patches that break whole modules: syntax errors, deletions, renames (git-style diffs).

usage: python breakmut.py <sqlglot> <out> <seed>
"""

import ast
import difflib
import json
import os
import random
import sys

src, out, seed = sys.argv[1], sys.argv[2], int(sys.argv[3])
rng = random.Random(seed)
os.makedirs(f"{out}/commits", exist_ok=True)

mods = []
for root, _, fs in os.walk(os.path.join(src, "sqlglot")):
    for f in fs:
        if f.endswith(".py") and f != "__init__.py":
            mods.append(os.path.relpath(os.path.join(root, f), src).replace("\\", "/"))
mods.sort()
rng.shuffle(mods)


def read(rel):
    return open(os.path.join(src, rel), encoding="utf-8", newline="").read()


kinds, n = {}, 0


def put(kind, rel, text):
    global n
    cid = f"b{seed % 100:02d}{n:03d}"
    open(f"{out}/commits/{cid}.patch", "w", encoding="utf-8", newline="\n").write(text)
    kinds[cid] = {"kind": kind, "file": rel}
    n += 1


it = iter(mods)
# syntax errors inside a function body
while n < 18:
    rel = next(it)
    text = read(rel)
    lines = text.split("\n")
    fn = [x.body[0].lineno for x in ast.walk(ast.parse(text)) if isinstance(x, ast.FunctionDef)]
    if not fn:
        continue
    ln = rng.choice(fn)
    new = list(lines)
    new[ln - 1] = new[ln - 1] + " ("
    d = "".join(difflib.unified_diff(text.splitlines(True), "\n".join(new).splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
    put("syntax", rel, f"diff --git a/{rel} b/{rel}\n" + d)
# deletions
while n < 24:
    rel = next(it)
    text = read(rel)
    d = "".join(difflib.unified_diff(text.splitlines(True), [], f"a/{rel}", "/dev/null", n=0))
    put("delete_module", rel, f"diff --git a/{rel} b/{rel}\ndeleted file mode 100644\n" + d)
# pure renames
while n < 30:
    rel = next(it)
    new = rel[:-3] + "_moved.py"
    put("rename_module", rel, f"diff --git a/{rel} b/{new}\nsimilarity index 100%\nrename from {rel}\nrename to {new}\n")
json.dump(kinds, open(f"{out}/kinds.json", "w"), indent=1)
print("made", n, sorted(v["file"] for v in kinds.values())[:30])
