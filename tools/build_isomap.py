"""Merge per-test isolated traces (iso/<n>.json) into the coverage map and build attrmap.json.gz.

coverage: every test's isolated "test" lines are added to covmap (union with the existing traces),
and lines run while modules were imported are added to the import-run set.

attrmap: {"reads": {"<owner>|<NAME>": [test numbers that read it during the test]},
          "early": ["<owner>|<NAME>", ...]   read while a module was imported or before the test:
                                              the value may have flowed into derived data
          "dynamic": [NAME, ...]}             names that appear as string literals in sqlglot, so
                                              they may be read through getattr/__dict__ untraced
"""

import ast
import glob
import gzip
import json
import os
import sys
from collections import defaultdict

work, src = sys.argv[1], sys.argv[2]
tests = json.load(open(f"{work}/sample/tests.json"))["tests"]
num = {t["id"]: t["number"] for t in tests}

m = json.loads(gzip.decompress(open(f"{work}/covmap.json.gz", "rb").read()))
sets = m["sets"]
line_tests = defaultdict(set)
for f, lines in m["lines"].items():
    for l, sid in lines.items():
        line_tests[(f, int(l))].update(sets[sid])
imp = {f: set(v) for f, v in m["imp"].items()}

reads = defaultdict(set)
early = set()
seen = 0
for p in glob.glob(f"{work}/iso/*.json"):
    d = json.load(open(p))
    tid = d.get("test")
    if tid not in num:
        continue
    n = num[tid]
    seen += 1
    for f, l in d["lines"]["test"]:
        line_tests[(f, l)].add(n)
    for f, l in d["lines"]["import"]:
        imp.setdefault(f, set()).add(l)
    for owner, name, phase in d["attrs"]:
        key = f"{owner}|{name}"
        if phase == "test":
            reads[key].add(n)
        else:
            early.add(key)

# names used as strings anywhere in sqlglot: possible getattr / __dict__ access
dynamic = set()
for root, _, files in os.walk(os.path.join(src, "sqlglot")):
    for fn in files:
        if fn.endswith(".py"):
            try:
                tree = ast.parse(open(os.path.join(root, fn), encoding="utf-8").read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
                    dynamic.add(node.value)

# rebuild covmap with merged lines
index, new_sets = {}, []


def sid(s):
    k = tuple(sorted(s))
    if k not in index:
        index[k] = len(new_sets)
        new_sets.append(list(k))
    return index[k]


lines_out = defaultdict(dict)
for (f, l), ns in line_tests.items():
    lines_out[f][str(l)] = sid(ns)
opens = {f: sid(sets[s]) for f, s in m["opens"].items()}
m2 = {"tests": m["tests"], "sets": new_sets, "lines": lines_out, "imp": {f: sorted(v) for f, v in imp.items()}, "opens": opens}
with gzip.open(f"{work}/covmap.json.gz", "wb", compresslevel=9) as fh:
    fh.write(json.dumps(m2, separators=(",", ":")).encode())
am = {"reads": {k: sorted(v) for k, v in reads.items()}, "early": sorted(early), "dynamic": sorted(dynamic)}
with gzip.open(f"{work}/attrmap.json.gz", "wb", compresslevel=9) as fh:
    fh.write(json.dumps(am, separators=(",", ":")).encode())
print("isolated tests", seen, "attr keys", len(reads), "early", len(early), "dynamic names", len(dynamic),
      "covmap lines", len(line_tests))
