"""Record the sqlglot lines that run when expressions are pickled and unpickled.

sqlglot's tests send expressions to spawned process pools. A patch to that code can crash a
real pool child, which an in-process run does not reproduce, so such patches are never verified
here. usage (dev image, cwd /sqlglot): python pickle_lines.py out.json
"""

import json
import pickle
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


mon.use_tool_id(TOOL, "pk")
mon.register_callback(TOOL, mon.events.LINE, line)
mon.set_events(TOOL, mon.events.LINE)

sys.path.insert(0, "/sqlglot")
from sqlglot import parse_one  # noqa: E402
from sqlglot.optimizer import optimize  # noqa: E402
from sqlglot.schema import ensure_schema  # noqa: E402
from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs  # noqa: E402


def roundtrip(obj):
    global active
    active = True
    mon.restart_events()
    try:
        pickle.loads(pickle.dumps(obj))
    finally:
        active = False


import glob  # noqa: E402
import os  # noqa: E402

n = 0
for path, schema in (("optimizer/tpc-h/tpc-h.sql", TPCH_SCHEMA), ("optimizer/tpc-ds/tpc-ds.sql", TPCDS_SCHEMA)):
    for meta, sql, _ in load_sql_fixture_pairs(path):
        e = parse_one(sql)
        roundtrip(e)
        try:
            roundtrip(optimize(e, ensure_schema(schema)))
        except Exception:
            pass
        n += 1
for f in sorted(glob.glob("/sqlglot/tests/fixtures/optimizer/*.sql")):
    for meta, sql, expected in load_sql_fixture_pairs(os.path.relpath(f, "/sqlglot/tests/fixtures")):
        for q in (sql, expected):
            try:
                e = parse_one(q, read=meta.get("dialect"))
                e.meta["x"] = 1
                roundtrip(e)
                n += 1
            except Exception:
                pass
out = {}
for f, ln in sorted(hits):
    out.setdefault(f, []).append(ln)
json.dump(out, open(sys.argv[1], "w"))
print(n, {f: len(v) for f, v in out.items()})
