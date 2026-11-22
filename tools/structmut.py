"""Structural patches: renames, deletions, shared test-helper edits, multi-file code edits.

Written as git-style diffs (rename headers included) so they exercise the patch parser.
usage: python structmut.py <sqlglot> <out>
"""

import difflib
import json
import os
import sys

src, out = sys.argv[1], sys.argv[2]
os.makedirs(f"{out}/commits", exist_ok=True)


def read(rel):
    return open(os.path.join(src, rel), encoding="utf-8", newline="").read()


def udiff(rel_old, rel_new, old, new):
    d = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel_old}", f"b/{rel_new}", n=3))
    return d


def edit(rel, a, b):
    old = read(rel)
    assert a in old, (rel, a)
    return f"diff --git a/{rel} b/{rel}\n" + udiff(rel, rel, old, old.replace(a, b, 1))


patches = {}
# 1. pure rename of a dialect module
patches["s01"] = ("rename_module", "diff --git a/sqlglot/dialects/tsql.py b/sqlglot/dialects/tsqlx.py\nsimilarity index 100%\nrename from sqlglot/dialects/tsql.py\nrename to sqlglot/dialects/tsqlx.py\n")
# 2. pure rename of a fixture
patches["s02"] = ("rename_fixture", "diff --git a/tests/fixtures/optimizer/simplify.sql b/tests/fixtures/optimizer/simplify_v2.sql\nsimilarity index 100%\nrename from tests/fixtures/optimizer/simplify.sql\nrename to tests/fixtures/optimizer/simplify_v2.sql\n")
# 3. delete a fixture file
fx = "tests/fixtures/optimizer/isolate_table_selects.sql"
old = read(fx)
patches["s03"] = ("delete_fixture", f"diff --git a/{fx} b/{fx}\ndeleted file mode 100644\n" + "".join(difflib.unified_diff(old.splitlines(True), [], f"a/{fx}", "/dev/null", n=0)))
# 4. shared test helper edit
patches["s04"] = ("helpers_edit", edit("tests/helpers.py", 'filename), encoding="utf-8"', 'filename), encoding="latin-1"'))
# 5. multi-file: two unrelated comparison flips
p1 = edit("sqlglot/optimizer/simplify.py", " == ", " != ")
p2 = edit("sqlglot/generators/postgres.py", " is None", " is not None")
patches["s05"] = ("multi_file", p1 + p2)
# 6. rename + content change of a parser module
rel = "sqlglot/parsers/dremio.py"
old = read(rel)
new = old.replace("len(args) == 2", "len(args) == 3", 1)
patches["s06"] = ("rename_edit_module", f"diff --git a/{rel} b/sqlglot/parsers/dremio2.py\nsimilarity index 98%\nrename from {rel}\nrename to sqlglot/parsers/dremio2.py\n" + udiff(rel, "sqlglot/parsers/dremio2.py", old, new))
# 7. new unused module (harmless)
patches["s07"] = ("new_file", "diff --git a/sqlglot/extra_notes.py b/sqlglot/extra_notes.py\nnew file mode 100644\n--- /dev/null\n+++ b/sqlglot/extra_notes.py\n@@ -0,0 +1,2 @@\n+NOTES = 1\n+VALUE = NOTES + 1\n")
# 8. module-level constant in a core module
patches["s08"] = ("module_const", edit("sqlglot/helper.py", "PYTHON_VERSION", "PYTHON_VERSION_X") if "PYTHON_VERSION =" in read("sqlglot/helper.py") else edit("sqlglot/tokens.py", " = ", " = ", ))

kinds = {}
for cid, (kind, text) in patches.items():
    open(f"{out}/commits/{cid}.patch", "w", encoding="utf-8", newline="\n").write(text)
    kinds[cid] = {"kind": kind, "file": ""}
json.dump(kinds, open(f"{out}/kinds.json", "w"), indent=1)
print("made", len(patches))
