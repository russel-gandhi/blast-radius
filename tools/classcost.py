"""Measure per-class fixed CPU (module import + setUpClass) for the verifier: one cheapest test per class."""
import json, os, sys
sys.path.insert(0, "/work")
import verify as V
T = json.load(open("/work/sample/tests.json"))["tests"]
ids = [t["id"] for t in T]
v = V.Verifier("/sqlglot", ids)
v.prepare()
by_cls = {}
for t in T:
    c = t["id"].rsplit("::", 1)[0]
    if c not in by_cls or t["seconds"] < by_cls[c]["seconds"]:
        by_cls[c] = t
# baseline: fork with nothing
c0 = V.cpu_now(); v.run({}, [], 30); base = V.cpu_now() - c0
out = {}
for c, t in sorted(by_cls.items()):
    c0 = V.cpu_now()
    v.run({}, [t["number"]], 60)
    out[c] = round(max(0.0, V.cpu_now() - c0 - t["seconds"]), 3)
out["__base__"] = round(base, 3)
json.dump(out, open("/work/classcost.json", "w"), indent=0)
print("base", base, sorted(out.items(), key=lambda x: -x[1])[:12])
