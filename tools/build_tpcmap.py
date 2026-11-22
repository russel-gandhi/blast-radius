"""Build tpcmap.json.gz from tpc_cov.py output (tpccov/setup.json, tpccov/<which>_<i>.json).

{test id: {"attr": "<which>_sqls", "n": queries, "setup": {file: [lines]},
           "lines": {file: {line: [query indices]}}}}
Refuses (exits non-zero) when a query trace is missing or a query failed on the pinned commit.
"""

import glob
import gzip
import json
import os
import sys
from collections import defaultdict

d = sys.argv[1] if len(sys.argv) > 1 else "tpccov"
setup = defaultdict(list)
for f, l in json.load(open(os.path.join(d, "setup.json"))):
    setup[f].append(l)
out = {}
for which, n in (("tpch", 22), ("tpcds", 99)):
    lines = defaultdict(lambda: defaultdict(list))
    for i in range(n):
        p = os.path.join(d, f"{which}_{i}.json")
        q = json.load(open(p))
        assert q["n"] == n, (p, q["n"])
        assert q["ok"], f"query {which} {i} fails on the pinned commit"
        for f, l in q["lines"]:
            lines[f][str(l)].append(i)
    out[f"tests/test_executor.py::TestExecutor::test_execute_{which}"] = {
        "attr": f"{which}_sqls", "n": n, "setup": dict(setup),
        "lines": {f: dict(v) for f, v in lines.items()},
    }
with gzip.open("tpcmap.json.gz", "wt") as fh:
    json.dump(out, fh)
print({k: (v["n"], sum(len(x) for x in v["lines"].values())) for k, v in out.items()}, os.path.getsize("tpcmap.json.gz"))
