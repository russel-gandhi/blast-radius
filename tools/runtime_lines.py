"""Record the sqlglot lines that run during the execution phase of the TPC-H/TPC-DS executor tests.

Everything the replay fingerprint does not capture: loading the CSV tables into executor Tables,
ensure_tables, PythonExecutor.execute (scans, joins, aggregation, subquery compilation, the env
functions the generated code calls). A patch touching any of these lines cannot be replayed.

usage (dev image, cwd /sqlglot): python runtime_lines.py out.json
"""

import json
import sys

mon = sys.monitoring
TOOL = mon.COVERAGE_ID
ROOT = "/sqlglot/"
active = False
hits: set = set()


def line(code, ln):
    if active and code.co_filename.startswith(ROOT):
        hits.add((code.co_filename[len(ROOT):], ln))
        return mon.DISABLE
    return None


mon.use_tool_id(TOOL, "rt")
mon.register_callback(TOOL, mon.events.LINE, line)
mon.set_events(TOOL, mon.events.LINE)

import pickle  # noqa: E402

import sqlglot.executor as ex  # noqa: E402
from sqlglot import parse_one  # noqa: E402
from sqlglot.executor import python as pyx  # noqa: E402
from sqlglot.executor import table as tbl  # noqa: E402
from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs  # noqa: E402

sys.path.insert(0, "/sqlglot")
import tests.test_executor as te  # noqa: E402


def traced(fn):
    def wrapper(*a, **k):
        global active
        prev = active
        active = True
        mon.restart_events()
        try:
            return fn(*a, **k)
        finally:
            active = prev

    return wrapper


pyx.PythonExecutor.execute = traced(pyx.PythonExecutor.execute)
ex.ensure_tables = traced(ex.ensure_tables)
tbl.Table.__init__ = traced(tbl.Table.__init__)

# load data exactly as the test class does (Table construction is traced)
te.TestExecutor.setUpClass()
cls = te.TestExecutor
for schema, tables, sqls in ((TPCH_SCHEMA, cls.tpch_tables, cls.tpch_sqls), (TPCDS_SCHEMA, cls.tpcds_tables, cls.tpcds_sqls)):
    te.initializer(schema, tables)
    for meta, sql, _ in sqls:
        try:
            res = te.mp_execute(pickle.loads(pickle.dumps(parse_one(sql))), meta)
            if res is not None:
                pickle.loads(pickle.dumps(res))
        except Exception as x:  # noqa: BLE001
            print("error", type(x).__name__, x, file=sys.stderr)

out = {}
for f, ln in sorted(hits):
    out.setdefault(f, []).append(ln)
json.dump(out, open(sys.argv[1], "w"))
print({f: len(v) for f, v in out.items()})
