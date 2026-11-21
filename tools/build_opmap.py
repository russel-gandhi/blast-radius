"""Merge opplug traces (ops<seed>.gw*.json) into per-file comparison maps: opmap/<file>.json.gz

Per site: {"ovf": bool, "keys": [[key, [test numbers], global]]}. global is true when the outcome
was also seen at import time or outside any test (it may then reach every test).
Class setup ("S|") observations are credited to every test of that class.
"""

import glob
import gzip
import json
import os
import sys
from collections import defaultdict

work = sys.argv[1] if len(sys.argv) > 1 else "."
tests = json.load(open(f"{work}/sample/tests.json"))["tests"]
num = {t["id"]: t["number"] for t in tests}
by_class = defaultdict(list)
for t in tests:
    by_class[t["id"].rsplit("::", 1)[0]].append(t["number"])

sites: dict = {}
for path in sorted(glob.glob(f"{work}/ops[0-9].gw*.json")):
    d = json.load(open(path))
    for site, v in d.items():
        e = sites.setdefault(site, {"ovf": False, "keys": {}})
        e["ovf"] |= v["ovf"]
        for key, ctxs in v["keys"]:
            k = json.dumps(key, separators=(",", ":"))
            ent = e["keys"].setdefault(k, [set(), False])
            for c in ctxs:
                if "|" not in c:
                    ent[1] = True
                    continue
                phase, nodeid = c.split("|", 1)
                if nodeid not in num:
                    ent[1] = True
                    continue
                if phase == "S":
                    ent[0].update(by_class[nodeid.rsplit("::", 1)[0]])
                else:
                    ent[0].add(num[nodeid])

per_file: dict = defaultdict(dict)
for site, e in sites.items():
    f = site.split(":", 1)[0]
    per_file[f][site] = {
        "ovf": e["ovf"],
        "keys": [[json.loads(k), sorted(ts), g] for k, (ts, g) in e["keys"].items()],
    }
out = f"{work}/opmap"
os.makedirs(out, exist_ok=True)
total = 0
for f, data in per_file.items():
    p = os.path.join(out, f.replace("/", "__") + ".json.gz")
    raw = json.dumps(data, separators=(",", ":")).encode()
    with gzip.open(p, "wb", compresslevel=9) as fh:
        fh.write(raw)
    total += os.path.getsize(p)
print("sites", len(sites), "ovf", sum(e["ovf"] for e in sites.values()), "files", len(per_file), "bytes", total)
