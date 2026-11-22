"""Per-query line coverage of the TPC-H / TPC-DS executor tests, one query per fresh process.

usage (dev image, cwd /sqlglot, PYTHONPATH=/sqlglot:/work, SKIP_INTEGRATION=0):
  python tpc_cov.py setup OUT.json            lines run by TestExecutor.setUpClass (shared by all)
  python tpc_cov.py query WHICH I OUT.json    lines run by test_execute_<WHICH> with only query I

Each query runs exactly as the test runs it (parse_one, pickling through the in-process pool,
mp_execute, the DuckDB comparison in subtestHelper), with the class's query list cut down to
that one query. A fresh process per query means no cache built by another query hides a line.
"""

import json
import sys

ROOT = "/sqlglot/"
mon = sys.monitoring
TOOL = mon.COVERAGE_ID
LINES: set = set()
active = False


def _line(code, ln):
    if active and code.co_filename.startswith(ROOT):
        LINES.add((code.co_filename[len(ROOT):], ln))
        return mon.DISABLE
    return None


mon.use_tool_id(TOOL, "tpc")
mon.register_callback(TOOL, mon.events.LINE, _line)
mon.set_events(TOOL, mon.events.LINE)

sys.path.insert(0, "/work")
import verify  # noqa: E402

verify._install_inprocess_pool()
sys.path.insert(0, "/sqlglot")
import unittest  # noqa: E402

import tests.test_executor as te  # noqa: E402


def main():
    global active
    mode = sys.argv[1]
    cls = te.TestExecutor
    if mode == "setup":
        active = True
        mon.restart_events()
        cls.setUpClass()
        active = False
        json.dump(sorted(LINES), open(sys.argv[2], "w"))
        return
    which, i, out = sys.argv[2], int(sys.argv[3]), sys.argv[4]
    cls.setUpClass()
    attr = f"{which}_sqls"
    full = getattr(cls, attr)
    setattr(cls, attr, [full[i]])
    test = cls(f"test_execute_{which}")
    result = unittest.TestResult()
    active = True
    mon.restart_events()
    test.run(result)
    active = False
    ok = not (result.failures or result.errors)
    json.dump({"ok": ok, "n": len(full), "lines": sorted(LINES)}, open(out, "w"))


main()
