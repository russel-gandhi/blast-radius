"""Generate single-comparison patches (operator swap or constant change) at sites the suite evaluates.

usage: python cmpmut.py <sqlglot> <opmap dir> <out> <count> <seed>
"""

import ast
import difflib
import glob
import gzip
import json
import os
import random
import sys

src, opdir, out, count, seed = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
rng = random.Random(seed)
os.makedirs(f"{out}/commits", exist_ok=True)

SWAP = {
    ast.Eq: [ast.NotEq, ast.Is], ast.NotEq: [ast.Eq, ast.IsNot], ast.Is: [ast.Eq, ast.IsNot],
    ast.IsNot: [ast.NotEq, ast.Is], ast.Lt: [ast.LtE, ast.Gt], ast.LtE: [ast.Lt, ast.GtE],
    ast.Gt: [ast.GtE, ast.Lt], ast.GtE: [ast.Gt, ast.LtE], ast.In: [ast.NotIn], ast.NotIn: [ast.In],
}
SYM = {ast.Eq: "==", ast.NotEq: "!=", ast.Is: "is", ast.IsNot: "is not", ast.Lt: "<", ast.LtE: "<=",
       ast.Gt: ">", ast.GtE: ">=", ast.In: "in", ast.NotIn: "not in"}

sites = []
for f in glob.glob(os.path.join(opdir, "*.json.gz")):
    d = json.loads(gzip.decompress(open(f, "rb").read()))
    for site, v in d.items():
        if not v["ovf"]:
            sites.append(site)
rng.shuffle(sites)

made, kinds = 0, {}
for site in sites:
    if made >= count:
        break
    path, l1, c1, l2, c2 = site.rsplit(":", 4)
    l1, c1, l2, c2 = map(int, (l1, c1, l2, c2))
    if l1 != l2:
        continue
    text = open(os.path.join(src, path), encoding="utf-8", newline="").read()
    lines = text.split("\n")
    seg = lines[l1 - 1].encode("utf-8")[c1:c2].decode("utf-8")
    try:
        node = ast.parse(seg, mode="eval").body
    except SyntaxError:
        continue
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        continue
    op = type(node.ops[0])
    left = seg[: node.left.end_col_offset]
    right = seg[node.comparators[0].col_offset:]
    comp = node.comparators[0]
    if isinstance(comp, ast.Constant) and isinstance(comp.value, (int, str)) and not isinstance(comp.value, bool) and rng.random() < 0.4:
        v = comp.value
        nv = repr(v + 1) if isinstance(v, int) else repr(v + "_x" if v else "x")
        new_seg = f"{left} {SYM[op]} {nv}"
        kind = "cmp_const"
    else:
        nop = rng.choice(SWAP[op])
        new_seg = f"{left} {SYM[nop]} {right}"
        kind = f"cmp_{op.__name__}_to_{nop.__name__}"
    b = lines[l1 - 1].encode("utf-8")
    new_line = (b[:c1] + new_seg.encode("utf-8") + b[c2:]).decode("utf-8")
    new_lines = list(lines)
    new_lines[l1 - 1] = new_line
    new = "\n".join(new_lines)
    try:
        ast.parse(new)
    except SyntaxError:
        continue
    d = "".join(difflib.unified_diff(text.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}", n=3))
    cid = f"c{seed % 100:02d}{made:03d}"
    open(f"{out}/commits/{cid}.patch", "w", encoding="utf-8", newline="\n").write(f"diff --git a/{path} b/{path}\n" + d)
    kinds[cid] = {"kind": kind, "file": path, "site": site}
    made += 1
json.dump(kinds, open(f"{out}/kinds.json", "w"), indent=1)
print("made", made)
