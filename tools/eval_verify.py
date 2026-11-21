"""Dev check: verify candidate tests at runtime for every sample commit and compare with answers."""

import json
import os
import sys
import time

sys.path.insert(0, "/work")
import selector as S  # noqa: E402
import verify as V  # noqa: E402

CAP = float(os.environ.get("CAP_MS", "5000"))
data = os.environ.get("BR_DATA", "/work/sample")
answers = json.load(open(f"{data}/answers.json"))["commits"] if os.path.exists(f"{data}/answers.json") else {}
sel = S.Selector()
ver = V.Verifier("/sqlglot", [t["id"] for t in sel.tests])
t0 = V.cpu_now()
ver.prepare()
print("prepare cpu", round(V.cpu_now() - t0, 2), flush=True)
tot_cpu = 0.0
bad = 0
for patch in sorted(os.listdir(f"{data}/commits")):
    cid = patch[:-6]
    chosen, reasons = sel.select(open(f"{data}/commits/{patch}").read())
    R = sorted(n for n in chosen if sel.ms[n] <= CAP)
    if not R:
        continue
    c0 = V.cpu_now()
    w0 = time.monotonic()
    F = ver.run(sel.files, R, timeout=120)
    cpu = V.cpu_now() - c0
    tot_cpu += cpu
    est = sum(sel.ms[n] for n in R) / 1000
    truth = set(answers.get(cid, {}).get("failing", [])) & set(R)
    ok = F is not None and F == truth
    if not ok:
        bad += 1
    print(cid, "R", len(R), "est", round(est, 2), "cpu", round(cpu, 2), "wall", round(time.monotonic() - w0, 2),
          "F", None if F is None else len(F), "truth", len(truth), "OK" if ok else f"MISMATCH extra={sorted((F or set()) - truth)[:5]} missing={sorted(truth - (F or set()))[:5]}", flush=True)
print("total verify cpu", round(tot_cpu, 2), "mismatches", bad)
