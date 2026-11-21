"""Merge cov.gw*.json traces into a compact line -> tests map (covmap.json.gz).

Layout:
  tests: list of test ids (index = test number from tests.json)
  sets:  list of sorted test-number lists (deduplicated)
  lines: {file: {line: set_index}}      lines run while a test ran (setup/call/teardown)
  imp:   {file: [lines]}                lines run at import/collection time
  opens: {file: set_index}              files a test opened (fixtures etc.)
"""

import glob
import gzip
import json
import sys
from collections import defaultdict

work = sys.argv[1] if len(sys.argv) > 1 else "."
tests = json.load(open(f"{work}/sample/tests.json"))["tests"]
num = {t["id"]: t["number"] for t in tests}

per_test_lines: dict[int, set] = defaultdict(set)
per_test_opens: dict[int, set] = defaultdict(set)
class_setup_lines: dict[str, set] = defaultdict(set)
class_setup_opens: dict[str, set] = defaultdict(set)
imp: dict[str, set] = defaultdict(set)

for path in glob.glob(f"{work}/cov.gw*.json"):
    d = json.load(open(path))
    for ctx, lines in d["lines"].items():
        if "|" not in ctx:
            for f, l in lines:
                imp[f].add(l)
            continue
        phase, nodeid = ctx.split("|", 1)
        if nodeid not in num:
            continue
        n = num[nodeid]
        for f, l in lines:
            per_test_lines[n].add((f, l))
        if phase == "S":
            cls = nodeid.rsplit("::", 1)[0]
            class_setup_lines[cls].update(map(tuple, lines))
    for ctx, files in d["opens"].items():
        if "|" not in ctx:
            continue
        phase, nodeid = ctx.split("|", 1)
        if nodeid not in num:
            continue
        per_test_opens[num[nodeid]].update(files)
        if phase == "S":
            class_setup_opens[nodeid.rsplit("::", 1)[0]].update(files)

# Class-level setup (setUpClass) runs once, inside the first test's setup: credit every test in the class.
for t in tests:
    cls = t["id"].rsplit("::", 1)[0]
    per_test_lines[t["number"]] |= class_setup_lines.get(cls, set())
    per_test_opens[t["number"]] |= class_setup_opens.get(cls, set())

line_tests: dict[tuple, list] = defaultdict(list)
for n in sorted(per_test_lines):
    for key in per_test_lines[n]:
        line_tests[key].append(n)
open_tests: dict[str, list] = defaultdict(list)
for n in sorted(per_test_opens):
    for f in per_test_opens[n]:
        open_tests[f].append(n)

sets: list = []
index: dict[tuple, int] = {}


def sid(lst):
    k = tuple(sorted(set(lst)))
    if k not in index:
        index[k] = len(sets)
        sets.append(list(k))
    return index[k]


lines_out: dict[str, dict] = defaultdict(dict)
for (f, l), ns in line_tests.items():
    lines_out[f][str(l)] = sid(ns)
opens_out = {f: sid(ns) for f, ns in open_tests.items()}

out = {
    "tests": [t["id"] for t in tests],
    "sets": sets,
    "lines": lines_out,
    "imp": {f: sorted(v) for f, v in imp.items()},
    "opens": opens_out,
}
raw = json.dumps(out, separators=(",", ":")).encode()
with gzip.open(f"{work}/covmap.json.gz", "wb", compresslevel=9) as fh:
    fh.write(raw)
print("tests with lines", len(per_test_lines), "files", len(lines_out), "lines", len(line_tests),
      "sets", len(sets), "set members", sum(map(len, sets)), "raw MB", round(len(raw) / 1e6, 2))
missing = [t["id"] for t in tests if not per_test_lines.get(t["number"])]
print("tests with no lines:", len(missing), missing[:10])
