"""Record the class attributes read during the execution phase of the TPC-H/TPC-DS executor tests.

Same windows as runtime_lines.py (Table construction, ensure_tables, PythonExecutor.execute), with
sqlglot compiled through isoplug's attribute-read rewrite. A replayed test only re-derives parsing,
optimizing, planning and code generation in the patched child; an import-time change whose values
are read during execution must not be replayed.

usage (dev image, cwd /sqlglot, PYTHONPATH=/sqlglot:/work): python runtime_attrs.py out.json
"""

import json
import pickle
import sys

import isoplug  # installs the sqlglot attribute-read rewrite (and line monitoring, unused here)

isoplug.phase = "boot"

import sqlglot.executor as ex  # noqa: E402
from sqlglot import parse_one  # noqa: E402
from sqlglot.executor import python as pyx  # noqa: E402
from sqlglot.executor import table as tbl  # noqa: E402

sys.path.insert(0, "/sqlglot")
import tests.test_executor as te  # noqa: E402
from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA  # noqa: E402


def window(fn):
    def wrapper(*a, **k):
        prev = isoplug.phase
        isoplug.phase = "test"
        try:
            return fn(*a, **k)
        finally:
            isoplug.phase = prev

    return wrapper


pyx.PythonExecutor.execute = window(pyx.PythonExecutor.execute)
ex.ensure_tables = window(ex.ensure_tables)
tbl.Table.__init__ = window(tbl.Table.__init__)

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

reads = sorted(f"{o}|{n}" for o, n, ph in isoplug.ATTRS if ph == "test")
json.dump(reads, open(sys.argv[1], "w"))
print("execution-phase attribute reads", len(reads))
