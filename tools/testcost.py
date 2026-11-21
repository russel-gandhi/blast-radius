"""Measure each test's CPU when run by the verifier's in-process unittest runner (classes run whole)."""
import json, os, sys, time, unittest, io
sys.path.insert(0, "/work")
import verify as V
T = json.load(open("/work/sample/tests.json"))["tests"]
v = V.Verifier("/sqlglot", [t["id"] for t in T]); v.prepare()
os.chdir(V.TREE); sys.path.insert(0, V.TREE)
V._install_inprocess_pool()
out = {}
classes = {}
for t in T:
    path, cls, meth = t["id"].split("::")
    classes.setdefault((path[:-3].replace("/", "."), cls), []).append((meth, t["number"]))
class R(unittest.TestResult):
    def startTest(self, test):
        super().startTest(test); self.t0 = time.process_time()
    def stopTest(self, test):
        super().stopTest(test)
        out[test.id()] = round(time.process_time() - self.t0, 4)
setup = {}
for (mod, cls), items in classes.items():
    t0 = time.process_time()
    __import__(mod); m = sys.modules[mod]
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(f"{cls}.{meth}", m) for meth, _ in items)
    sys.stdout = sys.stderr = io.StringIO()
    r = R(); suite.run(r)
    sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
    total = time.process_time() - t0
    per = sum(out.get(f"{mod}.{cls}.{meth}", 0) for meth, _ in items)
    setup[f"{mod}.{cls}"] = round(total - per, 3)
res = {"tests": {}, "setup": setup}
for t in T:
    path, cls, meth = t["id"].split("::")
    res["tests"][t["number"]] = out.get(f"{path[:-3].replace('/', '.')}.{cls}.{meth}", t["seconds"])
json.dump(res, open("/work/testcost.json", "w"))
import statistics
ratio = [res["tests"][t["number"]] / t["seconds"] for t in T if t["seconds"] > 0.2]
print("tests", len(out), "median ratio (>0.2s)", round(statistics.median(ratio), 2), "max setup", sorted(setup.items(), key=lambda x: -x[1])[:4])
