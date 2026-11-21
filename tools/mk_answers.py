"""Turn truth/<id>.txt (failing node ids, FILE lines) into answers.json for score.py."""
import json, os, sys
d = sys.argv[1]
T = json.load(open(f"{d}/tests.json"))["tests"]
num = {t["id"]: t["number"] for t in T}
byfile = {}
for t in T:
    byfile.setdefault(t["file"], []).append(t["number"])
kinds = json.load(open(f"{d}/kinds.json"))
ans = {}
for f in sorted(os.listdir(f"{d}/truth")):
    if not f.endswith(".done"):
        continue
    cid = f[:-5]
    fail = set()
    for line in open(f"{d}/truth/{cid}.txt"):
        line = line.strip()
        if line.startswith("FILE "):
            fail.update(byfile.get(line[5:], []))
        elif line in num:
            fail.add(num[line])
        elif "::" in line and line.split("[")[0] in num:
            fail.add(num[line.split("[")[0]])
    ans[cid] = {"failing": sorted(fail), "kind": kinds[cid]["kind"]}
json.dump({"commits": ans}, open(f"{d}/answers.json", "w"))
os.makedirs(f"{d}/done/commits", exist_ok=True)
import shutil
for cid in ans:
    shutil.copy(f"{d}/commits/{cid}.patch", f"{d}/done/commits/{cid}.patch")
shutil.copy(f"{d}/tests.json", f"{d}/done/tests.json")
shutil.copy(f"{d}/answers.json", f"{d}/done/answers.json")
print(len(ans), "labeled")
