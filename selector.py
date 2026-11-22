"""Blast Radius selector: pick the sqlglot tests a patch can break.

For each patch:
  1. Apply it in memory to the pinned sqlglot sources shipped in the image.
  2. Python files whose AST is unchanged (comments, blank lines) cannot change behaviour.
  3. Otherwise map the changed statements to the tests whose recorded coverage ran them
     (coverage map built from the pinned commit, see build_map.py).
  4. Data files (fixtures) map to the tests that opened them.
  5. Changes we cannot localise (import-time code, syntax errors, unknown files) fall back
     to wider, safe sets.
  6. Split the chosen tests over the number of workers that minimises the simulated cost.
"""

from __future__ import annotations

import ast
import gzip
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    from verify import Verifier
except Exception:  # pragma: no cover
    Verifier = None

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SRC", "/sqlglot"))
MAP = Path(os.environ.get("BR_MAP", Path(__file__).with_name("covmap.json.gz")))
WORKER_COST = 2000  # ms


def log(*a):
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------- patch parsing

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FilePatch:
    def __init__(self, old: str | None, new: str | None):
        self.old = old
        self.new = new
        self.hunks: list[tuple[int, list[str]]] = []  # (old_start, lines with prefix)
        self.binary = False

    @property
    def path(self) -> str:
        return self.old or self.new


def parse_patch(text: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    cur: FilePatch | None = None
    lines = text.split("\n")
    # git headers: a rename, mode change or binary change may come with no ---/+++ hunks at all
    pending: dict | None = None

    def flush():
        if pending is not None and not pending["used"]:
            fp = FilePatch(pending["old"], pending["new"])
            fp.binary = pending["binary"]
            files.append(fp)

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            flush()
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            pending = {"old": m.group(1) if m else None, "new": m.group(2) if m else None,
                       "used": False, "binary": False}
            i += 1
            continue
        if pending is not None and not pending["used"]:
            if line.startswith("rename from "):
                pending["old"] = line[len("rename from "):].strip()
            elif line.startswith("rename to "):
                pending["new"] = line[len("rename to "):].strip()
            elif line.startswith("deleted file mode"):
                pending["new"] = None
            elif line.startswith("new file mode"):
                pending["old"] = None
            elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                pending["binary"] = True
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if pending is not None:
                pending["used"] = True
            old = line[4:].split("\t")[0].strip()
            new = lines[i + 1][4:].split("\t")[0].strip()
            old = None if old == "/dev/null" else old[2:] if old.startswith("a/") else old
            new = None if new == "/dev/null" else new[2:] if new.startswith("b/") else new
            cur = FilePatch(old, new)
            files.append(cur)
            i += 2
            continue
        m = HUNK.match(line)
        if m and cur is not None:
            old_start = int(m.group(1))
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            body = []
            i += 1
            o = n = 0
            while i < len(lines) and (o < old_len or n < new_len):
                l = lines[i]
                if l.startswith("\\"):
                    i += 1
                    continue
                tag = l[:1] if l else " "
                if tag == " ":
                    o += 1
                    n += 1
                elif tag == "-":
                    o += 1
                elif tag == "+":
                    n += 1
                else:
                    break
                body.append(l if l else " ")
                i += 1
            # swallow "\ No newline" markers right after the hunk
            while i < len(lines) and lines[i].startswith("\\"):
                i += 1
            cur.hunks.append((old_start, body))
            continue
        i += 1
    flush()
    return files


def apply(fp: FilePatch, old_text: str) -> tuple[str, set[int], list[int]]:
    """Return (new_text, removed old line numbers, insertion points as old line numbers).

    An insertion point k means new lines were inserted after old line k (k may be 0).
    """
    old_lines = old_text.split("\n")
    out: list[str] = []
    removed: set[int] = set()
    inserts: list[int] = []
    pos = 0  # 0-based index into old_lines already copied
    for start, body in fp.hunks:
        s = max(start - 1, 0) if start > 0 else 0
        out.extend(old_lines[pos:s])
        pos = s
        block_removed = False  # inside a run of -/+ lines that removed something
        for l in body:
            tag, content = l[0], l[1:]
            if tag == " ":
                out.append(old_lines[pos] if pos < len(old_lines) else content)
                pos += 1
                block_removed = False
            elif tag == "-":
                removed.add(pos + 1)
                pos += 1
                block_removed = True
            else:
                if not block_removed:  # pure insertion: after old line `pos`
                    inserts.append(pos)
                out.append(content)
    out.extend(old_lines[pos:])
    return "\n".join(out), removed, inserts


# ---------------------------------------------------------------- analysis helpers


_CMP = {
    "Eq": lambda a, b: a == b, "NotEq": lambda a, b: a != b, "Lt": lambda a, b: a < b,
    "LtE": lambda a, b: a <= b, "Gt": lambda a, b: a > b, "GtE": lambda a, b: a >= b,
    "Is": lambda a, b: a is b, "IsNot": lambda a, b: a is not b,
    "In": lambda a, b: a in b, "NotIn": lambda a, b: a not in b,
}
_PRIM = {"NoneType", "bool", "int", "float", "str"}


def _new_result(kind, op, new_op, r, alts, vals, old_const, new_const):
    """Result of the edited comparison on one recorded evaluation, or None when unknown."""
    if kind == "op":
        if new_op == op:
            return r
        if alts is not None:
            if op in ("Eq", "NotEq", "Is", "IsNot"):
                is_, eq = alts
                got = {"Is": is_, "IsNot": None if is_ is None else not is_,
                       "Eq": eq, "NotEq": None if eq is None else not eq}.get(new_op)
                if got is not None:
                    return got
            elif op in ("Lt", "LtE", "Gt", "GtE"):
                lt, le, gt, ge, eq = alts
                got = {"Lt": lt, "LtE": le, "Gt": gt, "GtE": ge,
                       "Eq": eq, "NotEq": None if eq is None else not eq}.get(new_op)
                if got is not None:
                    return got
    if vals is None:
        return None
    ta, a, tb, b = vals
    if ta not in _PRIM or tb not in _PRIM:
        return None  # enum members are recorded without their value
    if kind == "left":
        if a != old_const or type(a).__name__ != type(old_const).__name__:
            return None
        a = new_const
    elif kind == "right":
        if b != old_const or type(b).__name__ != type(old_const).__name__:
            return None
        b = new_const
    if new_op in ("Is", "IsNot") and not (a is None or b is None or isinstance(a, bool) or isinstance(b, bool)):
        return None  # identity of ints and strings depends on interning
    try:
        got = _CMP[new_op](a, b)
    except Exception:
        return None
    return got if type(got) is bool else None


_POS = {"lineno", "col_offset", "end_lineno", "end_col_offset", "ctx", "type_comment"}


def _same(x, y) -> bool:
    """Structural equality of AST values, positions ignored (no string dumps: this runs a lot)."""
    if type(x) is not type(y):
        return False
    if isinstance(x, ast.AST):
        for f in x._fields:
            if f in _POS:
                continue
            if not _same(getattr(x, f, None), getattr(y, f, None)):
                return False
        return True
    if isinstance(x, list):
        return len(x) == len(y) and all(_same(a, b) for a, b in zip(x, y))
    return x == y


_DIFF_CACHE: dict = {}


def cached_diffs(old_tree, new_tree, limit=8):
    """ast_diffs for a file pair, computed once per patch (both refinements use it)."""
    key = (id(old_tree), id(new_tree))
    if key not in _DIFF_CACHE:
        _DIFF_CACHE.clear()
        _DIFF_CACHE[key] = (old_tree, new_tree, ast_diffs(old_tree, new_tree, limit=limit))
    return _DIFF_CACHE[key][2]


def ast_diffs(a, b, parents=(), out=None, limit=3):
    """Minimal differing node pairs between two ASTs (positions ignored), with ancestors of a."""
    if out is None:
        out = []
    if len(out) >= limit:
        return out
    if type(a) is not type(b) or not isinstance(a, ast.AST):
        out.append((a, b, list(parents)))
        return out
    for f in a._fields:
        if f in _POS:
            continue
        va, vb = getattr(a, f, None), getattr(b, f, None)
        if _same(va, vb):
            continue
        if isinstance(va, list) and isinstance(vb, list) and len(va) == len(vb):
            for x, y in zip(va, vb):
                if not _same(x, y):
                    if isinstance(x, ast.AST) and isinstance(y, ast.AST):
                        ast_diffs(x, y, (*parents, a), out, limit)
                    else:
                        out.append((a, b, list(parents)))
        elif isinstance(va, ast.AST) and isinstance(vb, ast.AST):
            ast_diffs(va, vb, (*parents, a), out, limit)
        else:
            out.append((a, b, list(parents)))
    return out


def build_text_index(src: Path) -> dict:
    """Texts searched for SQL keys: test methods, class helpers, module code, SQL fixtures,
    sqlglot sources and the lines inside their functions."""
    src = Path(src)
    methods: dict[str, str] = {}
    classes: dict[str, str] = {}
    modules: dict[str, str] = {}
    for p in sorted((src / "tests").rglob("test_*.py")):
        rel = p.relative_to(src).as_posix()
        try:
            text = p.read_text(encoding="utf-8")
            tree = ast.parse(text)
        except Exception:
            continue
        lines = text.split("\n")

        def seg(node):
            start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
            return "\n".join(lines[start - 1: node.end_lineno])

        modules[rel] = "\n".join(seg(n) for n in tree.body if not isinstance(n, ast.ClassDef))
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            extra = []
            for n in cls.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test"):
                    methods[f"{rel}::{cls.name}::{n.name}"] = seg(n)
                else:
                    extra.append(seg(n))
            classes[f"{rel}::{cls.name}"] = "\n".join(extra)
    fixtures: dict[str, str] = {}
    for p in (src / "tests" / "fixtures").rglob("*"):
        if p.is_file() and p.suffix in (".sql", ".json", ".txt", ".yaml", ".yml"):
            fixtures[p.relative_to(src).as_posix()] = p.read_text(encoding="utf-8", errors="replace")
    srcs: dict[str, str] = {}
    fnlines: dict[str, set[int]] = {}
    for p in (src / "sqlglot").rglob("*.py"):
        rel = p.relative_to(src).as_posix()
        text = p.read_text(encoding="utf-8")
        srcs[rel] = text
        lines_in_fn: set[int] = set()
        try:
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    lines_in_fn.update(range(node.body[0].lineno, (node.end_lineno or node.lineno) + 1))
                elif isinstance(node, ast.Lambda):
                    lines_in_fn.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        except SyntaxError:
            lines_in_fn = None
        fnlines[rel] = lines_in_fn
    return {"methods": methods, "classes": classes, "modules": modules, "fixtures": fixtures,
            "src": srcs, "fnlines": fnlines}


STATE_SENSITIVE = ("tests/test_generator.py::TestGenerator::test_generator_memory",)

def uses_name_at_import(tree, name: str, own_stmt) -> bool:
    """Is `name` loaded by bare name in any class or module body statement other than own_stmt?

    Function bodies are skipped (they run later, where the class's attribute is read through
    self/cls and traced); class and module bodies run at import and copy the value.
    """
    def visit(node):
        if node is own_stmt:
            return False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # defaults and decorators run at import; the body does not
            parts = list(getattr(node.args, "defaults", [])) + list(getattr(node.args, "kw_defaults", []) or [])
            parts += list(getattr(node, "decorator_list", []))
            return any(p is not None and visit(p) for p in parts)
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load):
            return True
        return any(visit(c) for c in ast.iter_child_nodes(node))

    return visit(tree)


KEYTEXT_TABLE = re.compile(r"^(FUNCTIONS|KEYWORDS|TRANSFORMS|[A-Z_]*_(FUNCTIONS|PARSERS|MAPPING|KEYWORDS))$")


def _dict_entries(d: ast.Dict):
    """{key_id: (value_dump, value_strings)} for a dict literal, or None when a key is not usable.

    key_id is ("str", text) for string keys and ("cls", Name) for `exp.Name` keys; anything else
    (enum members, ** spreads, computed keys) makes the table unusable here.
    """
    out = {}
    for k, v in zip(d.keys, d.values):
        if k is None:
            kid = ("spread", ast.dump(v))
        elif isinstance(k, ast.Constant) and isinstance(k.value, str):
            kid = ("str", k.value)
        elif (isinstance(k, ast.Attribute) and isinstance(k.value, ast.Name) and k.value.id == "exp"
              and k.attr[:1].isupper()):
            kid = ("cls", k.attr)
        else:
            return None
        strings = tuple(sorted({n.value for n in ast.walk(v) if isinstance(n, ast.Constant) and isinstance(n.value, str)}))
        out[kid] = (ast.dump(v), strings)
    return out


def strip_docstrings(tree: ast.AST) -> ast.AST:
    import copy

    tree = copy.deepcopy(tree)
    for node in [tree, *ast.walk(tree)]:
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            v = body[0].value
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                body[0] = ast.Pass()
    return tree


def stmt_index(tree: ast.AST):
    """List of (start, end, node, in_function) for every statement."""
    res = []

    def walk(node, in_func):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                res.append((child.lineno, child.end_lineno or child.lineno, child, in_func))
                walk(child, in_func or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)))
            else:
                walk(child, in_func or isinstance(child, ast.Lambda))

    walk(tree, False)
    return res


def header_end(node: ast.stmt) -> int:
    """Last line of a compound statement's header (before its first body statement)."""
    body = getattr(node, "body", None)
    if isinstance(body, list) and body and isinstance(body[0], ast.AST):
        first = body[0]
        start = first.lineno
        decos = getattr(first, "decorator_list", None)
        if decos:
            start = min(start, min(d.lineno for d in decos))
        return max(node.lineno, start - 1)
    return node.end_lineno or node.lineno


def innermost(stmts, line):
    best = None
    for s, e, node, in_func in stmts:
        if s <= line <= e and (best is None or (e - s) <= (best[1] - best[0])):
            best = (s, e, node, in_func)
    return best


# ---------------------------------------------------------------- selector


class Selector:
    def __init__(self):
        raw = json.loads(gzip.decompress(MAP.read_bytes()))
        self.sets = raw["sets"]
        self.lines = raw["lines"]
        self.imp = {f: set(v) for f, v in raw["imp"].items()}
        self.opens = raw["opens"]
        tj = json.loads((DATA / "tests.json").read_text())
        self.tests = tj["tests"]
        self.ms = {t["number"]: round(t["seconds"] * 1000) for t in self.tests}
        self.by_id = {t["id"]: t["number"] for t in self.tests}
        self.by_file: dict[str, list[int]] = {}
        for t in self.tests:
            self.by_file.setdefault(t["file"], []).append(t["number"])
        # map id from our coverage map to tests.json numbers (they should be identical)
        self.remap = [self.by_id.get(tid) for tid in raw["tests"]]
        self.all = [t["number"] for t in self.tests]
        here = Path(__file__).parent
        try:
            self.replay_ref = json.loads((here / "replay_ref.json").read_text())
            self.runtime = {f: set(v) for f, v in json.loads((here / "runtime_lines.json").read_text()).items()}
            self.pickling = {f: set(v) for f, v in json.loads((here / "pickle_lines.json").read_text()).items()}
            ra = here / "runtime_attrs.json"
            self.runtime_attrs = set(json.loads(ra.read_text())) if ra.exists() else None
        except Exception:
            self.replay_ref, self.runtime, self.pickling = {}, {}, {}
            self.runtime_attrs = None
        self.replay_tests = {self.by_id[t]: t for t in self.replay_ref if t in self.by_id}
        # tests touching each file at test time (any line)
        self._file_tests: dict[str, set[int]] = {}
        try:  # measured CPU of each test under the verifier, and of each class's setup
            tc = json.loads((here / "testcost.json").read_text())
            self.tcost = {int(k): v for k, v in tc["tests"].items()}
            self.csetup = tc["setup"]
        except Exception:
            self.tcost, self.csetup = {}, {}

    def cls_key(self, n: int) -> str:
        path, cls, _ = self.tests[n]["id"].split("::")
        return path[:-3].replace("/", ".") + "." + cls

    def set_tests(self, sid: int) -> set[int]:
        return {self.remap[n] for n in self.sets[sid] if self.remap[n] is not None}

    def cover(self, path: str, lines) -> set[int]:
        m = self.lines.get(path)
        res: set[int] = set()
        if not m:
            return res
        for l in lines:
            sid = m.get(str(l))
            if sid is not None:
                res |= self.set_tests(sid)
        return res

    def file_tests(self, path: str) -> set[int]:
        if path not in self._file_tests:
            m = self.lines.get(path, {})
            res: set[int] = set()
            for sid in set(m.values()):
                res |= self.set_tests(sid)
            self._file_tests[path] = res
        return self._file_tests[path]

    # -- per file ------------------------------------------------------------

    def select_file(self, fp: FilePatch, reasons: list[str]) -> set[int]:
        path = fp.path
        if fp.binary:
            # binary change: no text to analyse or to run tests against
            paths = [p for p in (fp.old, fp.new) if p]
            self.replay_ok = False; self.no_replay.add("binary")
            self.verify_ok = False
            self.code_changes.append("?binary")
            if any(not p.startswith("tests/fixtures/") for p in paths):
                reasons.append(f"binary {paths} -> all")
                return set(self.all)
            sel = set().union(*(self.opened_by(p) for p in paths))
            reasons.append(f"binary data {paths} -> {len(sel)}")
            return sel
        if not path.startswith("sqlglot/") or not path.endswith(".py"):
            # tests, fixtures, data: the executor tests' own inputs may change
            self.replay_ok = False; self.no_replay.add("non-sqlglot-file")
        src_path = SRC / (fp.old or fp.new)
        old_text = src_path.read_text(encoding="utf-8") if fp.old and src_path.exists() else ""
        new_text, removed, inserts = apply(fp, old_text)
        if fp.old and fp.new and fp.old != fp.new:
            self.files[fp.old] = None
        self.files[fp.new or fp.old] = None if fp.new is None else new_text
        if fp.new is None or (fp.old and fp.new and fp.old != fp.new):
            self.replay_ok = False; self.no_replay.add("delete/rename")
        if fp.new is None:  # deleted file
            reasons.append(f"delete {path}")
            if path.startswith("tests/fixtures/") and not path.endswith(".py"):
                return self.opened_by(path)  # a data file: only its readers can notice
            return self.opened_by(path) | self.importers(path)
        rename_extra: set[int] = set()
        if fp.old and fp.new and fp.old != fp.new:
            # the old path disappears: whoever opened or imported it can break
            rename_extra = self.opened_by(fp.old) | (self.importers(fp.old) if fp.old.endswith(".py") else set())
            self.code_changes.append("?rename")
            reasons.append(f"rename {fp.old} -> {fp.new} -> {len(rename_extra)}")
        if path.endswith(".py"):
            changed = [l[1:] for _, body in fp.hunks for l in body if l[:1] in "+-"]
            trivial = all(not c.strip() or c.lstrip().startswith("#") for c in changed)
            return self.select_py(path, old_text, new_text, removed, inserts, reasons, trivial) | rename_extra
        # data files: whoever opened them
        sel = self.opened_by(path) | rename_extra
        reasons.append(f"data {path} -> {len(sel)}")
        return sel

    def opened_by(self, path: str) -> set[int]:
        sid = self.opens.get(path)
        return self.set_tests(sid) if sid is not None else set()

    def importers(self, path: str) -> set[int]:
        """Tests whose process imports this file (a broken or missing module only breaks those).

        Traced per test in a fresh interpreter (importers.json.gz): startup and lazy imports.
        """
        if path.startswith("tests/"):
            return set(self.by_file.get(path, [])) or set(self.all)
        imap = self.importer_map()
        if imap is None or not path.startswith("sqlglot/"):
            return set(self.all)
        got = {n for n in imap.get(path, ()) if n < len(self.tests)}
        # tests that measure process-wide state (memory growth): failed imports left behind by
        # earlier tests in the same worker can push them over their limit, whatever they import
        got |= {self.by_id[t] for t in STATE_SENSITIVE if t in self.by_id}
        return got

    def importer_map(self):
        if "_imap" not in self.__dict__:
            p = Path(__file__).with_name("importers.json.gz")
            try:
                self._imap = json.loads(gzip.decompress(p.read_bytes())) if p.exists() else None
            except Exception:
                self._imap = None
        return self._imap

    def select_py(self, path, old_text, new_text, removed, inserts, reasons, trivial=True) -> set[int]:
        try:
            new_tree = ast.parse(new_text)
        except SyntaxError:
            self.replay_ok = False; self.no_replay.add("syntax")
            reasons.append(f"syntax error {path}")
            return self.importers(path)
        try:
            old_tree = ast.parse(old_text)
        except SyntaxError:
            old_tree = None
        # only comment/blank lines changed: the code is the same unless they sit inside a string
        if trivial and old_tree is not None and ast.dump(old_tree) == ast.dump(new_tree):
            # same code: comments/blank lines only (opens of .py files are just imports reading source)
            reasons.append(f"ast-equal {path}")
            return set()
        if old_tree is not None and not trivial and self.docstring_only(old_tree, old_text, removed, inserts):
            if ast.dump(strip_docstrings(old_tree)) == ast.dump(strip_docstrings(new_tree)):
                # nothing in sqlglot or its tests reads __doc__
                reasons.append(f"docstring-only {path}")
                return set()
        if old_tree is None:
            self.replay_ok = False; self.no_replay.add("new-file")
            reasons.append(f"new file {path}")
            return self.importers(path)

        stmts = stmt_index(old_tree)
        self.code_changes.append(path)
        try:
            self.refined[path] = self.operand_refine(path, old_tree, new_tree, stmts)
            if self.refined[path] is None:
                self.refined[path] = self.attr_refine(path, old_tree, new_tree, reasons)
        except Exception as exc:  # refinement is optional: fall back to coverage
            self.refined[path] = None
            reasons.append(f"refine error {exc!r}")
        old_n = old_text.count("\n") + 1
        sel: set[int] = set()
        touched: set[int] = set(removed)
        for k in inserts:
            # an inserted line runs exactly when its neighbours do
            for cand in (k, k + 1):
                if 1 <= cand <= old_n:
                    touched.add(cand)
        is_test_file = path.startswith("tests/")
        for line in sorted(touched):
            inner = innermost(stmts, line)
            if inner is None:
                # blank/comment line at module level next to an insertion: neighbour code
                continue
            s, e, node, in_func = inner
            imp = self.imp.get(path)
            if in_func and imp:
                span_end = header_end(node) if isinstance(getattr(node, "body", None), list) and line <= header_end(node) else e
                if any(l in imp for l in range(s, span_end + 1)):
                    # function code that also runs while a module is imported (metaclasses, class
                    # builders, lazily loaded dialects): its effect is shared by every test using it
                    got = self.import_reach(path) | self.cover(path, range(s, span_end + 1))
                    sel |= got
                    rt = self.runtime.get(path) or ()
                    pk = self.pickling.get(path) or ()
                    if any(l in pk for l in range(s, span_end + 1)):
                        self.verify_ok = self.replay_ok = False; self.no_replay.add("pickling")
                    elif any(l in rt for l in range(s, span_end + 1)):
                        self.replay_ok = False; self.no_replay.add("runtime-fn")
                    elif not self.dialect_file(path):
                        self.replay_fp = True  # replay decided by the import-time fingerprint
                    reasons.append(f"import-run {path}:{line} -> {len(got)}")
                    continue
            pk = self.pickling.get(path)
            if pk and (not in_func or any(l in pk for l in range(s, e + 1))):
                # pickling code: real spawned pool children may break where an in-process run does not
                self.verify_ok = self.replay_ok = False; self.no_replay.add("pickling")
            rt = self.runtime.get(path)
            if rt and in_func and any(l in rt for l in range(s, e + 1)):
                self.replay_ok = False; self.no_replay.add("runtime-fn")  # code that runs during execution
            elif not in_func and (rt or not self.dialect_file(path)):
                # class / module level: replay is decided by the import-time fingerprint (what
                # changed, and whether execution reads it)
                self.replay_fp = True
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or hasattr(node, "body") and isinstance(getattr(node, "body"), list):
                # change in a compound statement's header
                if line > header_end(node):
                    # line inside body but not in any child statement (blank/comment): use whole body lines
                    rng = range(s, e + 1)
                else:
                    rng = range(s, header_end(node) + 1)
            else:
                rng = range(s, e + 1)
            if not in_func:
                # import-time statement: def/class headers, class attributes, module constants
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and line <= header_end(node):
                    # signature/decorator change: callers of the function body
                    body_lines = range(node.body[0].lineno, (node.end_lineno or s) + 1)
                    got = self.cover(path, body_lines)
                    if is_test_file:
                        got |= self.test_named(path, node.name)
                    sel |= got
                    reasons.append(f"def-header {path}:{line} -> {len(got)}")
                    continue
                if is_test_file:
                    # a test file: its own tests; a shared helper (tests/helpers.py): every test
                    # file that uses it, since its values feed tests elsewhere
                    got = set(self.by_file.get(path, [])) or self.helper_users(path)
                else:
                    # a class-level value is used by whoever uses the class: tests that read any of
                    # its settings during the test (creating, validating or generating its nodes)
                    got = self.family_tests(path) | self.cover(path, rng) | self.class_users(path, old_tree, line)
                sel |= got
                reasons.append(f"import-time {path}:{line} -> {len(got)}")
                continue
            got = self.cover(path, rng)
            sel |= got
            fn = self.enclosing_def(stmts, line)
            if fn is not None:
                self.weak |= self.cover(path, range(fn.lineno, (fn.end_lineno or fn.lineno) + 1))
            self.weak |= self.file_tests(path)
            reasons.append(f"line {path}:{line} [{s}-{rng[-1]}] -> {len(got)}")
        self.touched_lines[path] = sorted(touched)
        return sel

    def helper_users(self, path: str) -> set[int]:
        """Tests in test files that refer to a shared test module (tests/helpers.py, ...)."""
        stem = path.rsplit("/", 1)[-1][:-3]
        if stem == "__init__":
            return set(self.all)
        got = self.tests_mentioning(stem)
        return got or set(self.all)

    def import_reach(self, path: str) -> set[int]:
        """Tests that can depend on what this file computes while being imported."""
        if path.startswith("tests/"):
            return set(self.by_file.get(path, [])) or self.helper_users(path)
        if self.dialect_file(path):
            return self.family_tests(path)
        return self.importers(path)

    def dialect_file(self, path: str) -> bool:
        parts = path.split("/")
        return (
            len(parts) == 3 and parts[1] in self.FAMILY_DIRS
            and parts[2] not in ("__init__.py", "dialect.py", "python.py")
        )

    @staticmethod
    def docstring_only(tree, text, removed, inserts) -> bool:
        """Every changed old line (and insertion point) is inside a docstring, a comment or blank."""
        ranges = []
        for node in [tree, *ast.walk(tree)]:
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
                v = body[0].value
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    ranges.append((body[0].lineno, body[0].end_lineno or body[0].lineno))
        if not ranges:
            return False
        lines = text.split("\n")

        def ok(l, insert=False):
            if any(a <= l <= b for a, b in ranges):
                return True
            if insert and any(a <= l + 1 <= b for a, b in ranges):
                return True
            src = lines[l - 1].strip() if 1 <= l <= len(lines) else ""
            return not src or src.startswith("#")

        return all(ok(l) for l in removed) and all(ok(k, True) for k in inserts)

    @staticmethod
    def enclosing_def(stmts, line):
        best = None
        for s, e, node, _ in stmts:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and s <= line <= e:
                if best is None or e - s < (best.end_lineno or 0) - best.lineno:
                    best = node
        return best

    # -- comparison outcomes (opmap) -------------------------------------------

    def opmap(self, path: str) -> dict | None:
        cache = self.__dict__.setdefault("_opmaps", {})
        if path not in cache:
            p = Path(__file__).with_name("opmap") / (path.replace("/", "__") + ".json.gz")
            try:
                cache[path] = json.loads(gzip.decompress(p.read_bytes())) if p.exists() else {}
            except Exception:
                cache[path] = None
        return cache[path]

    def attrmap(self) -> dict | None:
        if "_attrmap" not in self.__dict__:
            p = Path(__file__).with_name("attrmap.json.gz")
            try:
                d = json.loads(gzip.decompress(p.read_bytes()))
                d["early"] = set(d["early"])
                d["dynamic"] = set(d["dynamic"])
                self._attrmap = d
            except Exception:
                self._attrmap = None
        return self._attrmap

    # -- dictionary entries keyed by SQL text or expression classes ----------------

    def keytext_refine(self, path, name, old_stmt, new_tree, quals, reasons) -> set[int] | None:
        """Tests that can reach the changed entries of a SQL-text or class keyed table, or None.

        A FUNCTIONS / KEYWORDS / *_PARSERS / *_MAPPING entry is only reached when SQL text
        holding its key is tokenized, parsed or generated; a TRANSFORMS entry keyed by exp.Foo
        only when an exp.Foo node exists. The result is intersected with the coverage selection.
        """
        new_stmt = None
        for node in ast.walk(new_tree):
            if (isinstance(node, (ast.Assign, ast.AnnAssign)) and node.lineno == old_stmt.lineno
                    and type(node) is type(old_stmt)):
                new_stmt = node
        # statements before the edit keep their line numbers; fall back to matching by name
        if new_stmt is None or not isinstance(new_stmt.value, ast.Dict):
            return None
        old_entries = _dict_entries(old_stmt.value)
        new_entries = _dict_entries(new_stmt.value)
        if old_entries is None or new_entries is None:
            return None
        changed = [k for k in set(old_entries) | set(new_entries) if old_entries.get(k) != new_entries.get(k)]
        if not changed or len(changed) > 4:
            return None
        terms: set[str] = set()
        classes: set[str] = set()
        for kd in changed:
            kind, text = kd
            if kind == "str":
                terms.add(text)
            elif kind == "cls":
                classes.add(text)
            else:
                return None
            for side in (old_entries.get(kd), new_entries.get(kd)):
                for s in side[1] if side else ():
                    terms.add(s)
        terms = {t for t in terms if t.strip()}
        got: set[int] = set()
        if terms:
            got |= self.tests_with_text(terms)
            got |= self.tests_running_text(terms, path, old_stmt)
        if classes:
            if "_owner_users" not in self.__dict__:
                self.class_users(path, ast.Module(body=[], type_ignores=[]), 0)
            for c in classes:
                hit = [u for o, u in self._owner_users.items() if o.endswith("." + c)]
                if not hit:
                    return None
                for u in hit:
                    got |= u
        got |= self.tests_mentioning(name)
        reasons.append(f"keytext {name} {sorted(terms)[:3]}{sorted(classes)[:2]} -> {len(got)}")
        return {n for n in got if n < len(self.tests)}

    def _text_index(self):
        """Text index (built at image build time by build_text_index, else built here)."""
        if "_tidx" not in self.__dict__:
            p = Path(__file__).with_name("textindex.pkl.gz")
            idx = None
            if p.exists():
                try:
                    import pickle

                    idx = pickle.loads(gzip.decompress(p.read_bytes()))
                except Exception:
                    idx = None
            self._tidx = idx or build_text_index(SRC)
        return self._tidx

    @staticmethod
    def _pattern(terms):
        """Case-insensitive match of any term as a whole word; spaces match any whitespace."""
        parts = []
        for t in terms:
            p = r"\s+".join(re.escape(w) for w in t.split())
            if t[:1].isalnum() or t[:1] == "_":
                p = r"(?<![A-Za-z0-9_])" + p
            if t[-1:].isalnum() or t[-1:] == "_":
                p = p + r"(?![A-Za-z0-9_])"
            parts.append(p)
        return re.compile("|".join(parts), re.I)

    def tests_with_text(self, terms) -> set[int]:
        """Tests whose method, class helpers, module code or opened SQL fixtures hold the text."""
        idx = self._text_index()
        pat = self._pattern(terms)
        got: set[int] = set()
        by_class: dict[str, list[int]] = {}
        for n, t in enumerate(self.tests):
            f, cls, meth = t["id"].split("::")
            by_class.setdefault(f"{f}::{cls}", []).append(n)
            txt = idx["methods"].get(t["id"])
            if txt is None or pat.search(txt):
                got.add(n)
        for key, txt in idx["classes"].items():
            if pat.search(txt):
                got.update(by_class.get(key, ()))
        for f, txt in idx["modules"].items():
            if pat.search(txt):
                got.update(self.by_file.get(f, ()))
        for f, txt in idx["fixtures"].items():
            if pat.search(txt):
                got |= self.opened_by(f)
        return got

    def tests_running_text(self, terms, path, stmt) -> set[int]:
        """Tests that ran a line inside a sqlglot function holding the text (SQL built by sqlglot)."""
        idx = self._text_index()
        pat = self._pattern(terms)
        got: set[int] = set()
        for rel, text in idx["src"].items():
            if not pat.search(text):
                continue
            fn_lines = idx["fnlines"].get(rel)
            hits = {text.count("\n", 0, m.start()) + 1 for m in pat.finditer(text)}
            if rel == path:
                hits = {i for i in hits if not stmt.lineno <= i <= (stmt.end_lineno or stmt.lineno)}
            # class and module level lines with the same text are other tables' definitions
            keep = sorted(hits if fn_lines is None else hits & fn_lines)
            if keep:
                got |= self.cover(rel, keep)
        return got

    def class_users(self, path: str, tree, line: int) -> set[int]:
        """Tests that read any class-level setting owned by a class enclosing `line`."""
        am = self.attrmap()
        if am is None or not path.startswith("sqlglot/"):
            return set()
        if "_owner_users" not in self.__dict__:
            users: dict[str, set[int]] = {}
            for key, tests in am["reads"].items():
                users.setdefault(key.split("|", 1)[0], set()).update(tests)
            self._owner_users = users
        module = path[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        res: set[int] = set()

        def walk(node, quals):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    if child.lineno <= line <= (child.end_lineno or child.lineno):
                        q = [*quals, child.name]
                        res.update(self._owner_users.get(f"{module}.{'.'.join(q)}", ()))
                        walk(child, q)
                elif not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, quals)

        walk(tree, [])
        return {n for n in res if n < len(self.tests)}

    def tests_mentioning(self, name: str) -> set[int]:
        """Tests in test files whose source names `name` (test code reads are not traced)."""
        res: set[int] = set()
        for f, nums in self.by_file.items():
            p = SRC / f
            try:
                if name in p.read_text(encoding="utf-8"):
                    res.update(nums)
            except OSError:
                res.update(nums)
        return res

    def attr_refine(self, path: str, old_tree, new_tree, reasons) -> set[int] | None:
        """Tests that read the one class-level setting a patch changes, or None.

        Every `X.NAME` read in sqlglot was traced with each test in a fresh interpreter, keyed by
        the class that owns NAME for X. Trusted only when the setting was never read while modules
        were imported (its value may have been copied into derived data) and its name never
        appears as a string in sqlglot (getattr / __dict__ reads are not traced).
        """
        am = self.attrmap()
        if am is None or not path.startswith("sqlglot/"):
            return None
        diffs = cached_diffs(old_tree, new_tree)
        if not diffs or len(diffs) >= 8:
            return None
        stmt = None
        quals = None
        for old, new, parents in diffs:
            chain = list(parents) + ([old] if isinstance(old, ast.stmt) else [])
            # the statement directly inside the innermost class body
            idx = max((i for i, p in enumerate(chain) if isinstance(p, ast.ClassDef)), default=None)
            if idx is None or idx + 1 >= len(chain):
                return None
            if any(isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) for p in chain[: idx + 2]):
                return None
            s = chain[idx + 1]
            q = [p.name for p in chain[: idx + 1] if isinstance(p, ast.ClassDef)]
            bases = [b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else "" for b in chain[idx].bases]
            if any("Enum" in b or "AutoName" in b for b in bases):
                return None  # enum members are found by value or iteration, not attribute reads
            if stmt is None:
                stmt, quals = s, q
            elif s is not stmt:
                return None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            name = stmt.targets[0].id
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name = stmt.target.id
        else:
            return None
        if isinstance(stmt.value, ast.Dict) and KEYTEXT_TABLE.match(name):
            got = self.keytext_refine(path, name, stmt, new_tree, quals, reasons)
            if got is not None:
                self.refine_intersect.add(path)
                return got
        if uses_name_at_import(old_tree, name, stmt):
            # used by bare name in a class or module body (e.g. TYPE_MAPPING = {**STRING_TYPE_MAPPING}):
            # its value feeds other values while the module loads, which reads do not show
            return None
        if name in am["dynamic"] or name.startswith("__"):
            return None  # read by name somewhere, or used by Python itself (__slots__, ...)
        module = path[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        key = f"{module}.{'.'.join(quals)}|{name}"
        if key in am["early"]:
            return None
        got = {n for n in am["reads"].get(key, []) if n < len(self.tests)} | self.tests_mentioning(name)
        reasons.append(f"attr {key} -> {len(got)}")
        return got

    def operand_refine(self, path: str, old_tree, new_tree, stmts) -> set[int] | None:
        """Tests that can see a different result from a single edited comparison, or None."""
        if not path.startswith("sqlglot/") or self.opmap(path) is None:
            return None
        diffs = cached_diffs(old_tree, new_tree)
        if len(diffs) != 1:
            return None
        old, new, parents = diffs[0]
        cmp_old = None
        kind = None
        if isinstance(old, ast.cmpop) and isinstance(new, ast.cmpop):
            cmp_old = parents[-1] if parents and isinstance(parents[-1], ast.Compare) else None
            kind = "op"
        elif isinstance(old, ast.Constant) and isinstance(new, ast.Constant):
            par = parents[-1] if parents else None
            if isinstance(par, ast.Compare) and (par.left is old or (par.comparators and par.comparators[0] is old)):
                cmp_old = par
                kind = "left" if par.left is old else "right"
        if cmp_old is None or len(cmp_old.ops) != 1:
            return None
        # recorded sites are comparisons inside functions or lambdas only
        if not any(isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) for p in parents):
            return None
        site = f"{path}:{cmp_old.lineno}:{cmp_old.col_offset}:{cmp_old.end_lineno}:{cmp_old.end_col_offset}"
        rec = self.opmap(path).get(site)
        if rec is None:
            # never evaluated in the traced suite; trust that only if no test ran the line either
            if self.cover(path, range(cmp_old.lineno, (cmp_old.end_lineno or cmp_old.lineno) + 1)):
                return None
            return set()
        if rec["ovf"]:
            return None
        old_op = type(cmp_old.ops[0]).__name__
        new_op = type(new).__name__ if kind == "op" else old_op
        hit: set[int] = set()
        for key, tests, glob in rec["keys"]:
            op, r, alts, vals = key
            if op != old_op:
                return None
            nr = _new_result(kind, op, new_op, r, alts, vals, getattr(old, "value", None), getattr(new, "value", None))
            if nr is None or nr != r:
                if glob:
                    return None  # changes at import time or outside a test: may reach anything
                hit.update(tests)
        return hit

    FAMILY_DIRS = ("dialects", "parsers", "generators", "tokenizers", "typing")

    def family_tests(self, path: str) -> set[int]:
        """Tests that ran any code of this file or of its dialect siblings.

        An import-time value (class attribute, module constant, registry entry) is read
        by code elsewhere, so line coverage cannot see who depends on it. A dialect's
        settings live across dialects/X.py, parsers/X.py, generators/X.py, ... and are
        used by whoever runs any of those files.
        """
        res = set(self.file_tests(path))
        parts = path.split("/")
        if len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in self.FAMILY_DIRS:
            stem = parts[2]
            if stem not in ("__init__.py", "dialect.py"):
                for d in self.FAMILY_DIRS:
                    res |= self.file_tests(f"sqlglot/{d}/{stem}")
                tf = f"tests/dialects/test_{stem}"
                res |= set(self.by_file.get(tf, []))
        return res

    def test_named(self, path: str, name: str) -> set[int]:
        return {n for n in self.by_file.get(path, []) if self.tests[n]["id"].endswith("::" + name)}

    # -- per commit ----------------------------------------------------------

    def select(self, patch_text: str) -> tuple[set[int], list[str]]:
        reasons: list[str] = []
        sel: set[int] = set()
        self.files: dict[str, str | None] = {}
        self.weak: set[int] = set()
        self.replay_ok = True
        self.replay_fp = False
        self.no_replay: set[str] = set()
        self.verify_ok = True
        self.code_changes: list[str] = []
        self.refined: dict[str, set[int] | None] = {}
        self.refine_intersect: set[str] = set()  # refinements that only narrow the coverage set
        self.touched_lines: dict[str, list[int]] = {}
        self.fp_path = None
        self.fp_detect = False
        per_file: list[tuple[str, set[int]]] = []
        for fp in parse_patch(patch_text):
            try:
                per_file.append((fp.path, self.select_file(fp, reasons)))
            except Exception as exc:  # never crash: fall back to everything
                reasons.append(f"error {fp.path}: {exc!r}")
                per_file.append((fp.path, set(self.all)))
                self.code_changes.append("?")
        # One code change that is a single comparison edit: only the tests whose recorded
        # comparison outcome flips can behave differently. Two code changes can interact, so
        # they keep the coverage selection.
        if len(self.code_changes) == 1 and self.refined.get(self.code_changes[0]) is not None:
            path = self.code_changes[0]
            refined = self.refined[path]
            for i, (p, got) in enumerate(per_file):
                if p == path:
                    self.weak |= got
                    per_file[i] = (p, got & refined if path in self.refine_intersect else set(refined))
                    reasons.append(f"refined {path} -> {len(per_file[i][1])}")
        # one import-time code change with no finer rule: the import-time fingerprint can tell
        # which class attributes and module values it really changes (see fp_refine)
        # any class/module-level change may break a module's import (a value built at import
        # becomes invalid): such patches get an import check in a patched child
        self.fp_detect = any(r.startswith(("import-time ", "import-run ", "def-header ")) for r in reasons)
        if len(self.code_changes) == 1 and self.refined.get(self.code_changes[0]) is not None:
            # narrowed by the table-entry or class-setting rules: those only accept values that
            # are not read while modules import, so the edit cannot break an import
            self.fp_detect = False
        if len(self.code_changes) == 1 and self.refined.get(self.code_changes[0]) is None:
            path = self.code_changes[0]
            if path.startswith("sqlglot/") and any(
                r.startswith((f"import-time {path}:", f"import-run {path}:")) for r in reasons
            ):
                self.fp_path = path
        for _, got in per_file:
            sel |= got
        return sel, reasons

    def replay_safe(self, diff: dict | None) -> bool:
        """Can the executor tests be replayed for a patch with these import-time changes?

        Replay re-derives parsing, optimizing, planning and code generation in the patched child.
        Only values read while queries execute escape it: changed class attributes read in the
        execution phase (runtime_attrs.json), module values named on execution-phase lines, or
        anything this cannot classify.
        """
        if diff is None or self.runtime_attrs is None or diff.get("added") or diff.get("removed"):
            return False
        am = self.attrmap()
        if am is None:
            return False
        if "_traced_names" not in self.__dict__:
            self._traced_names = {k.split("|", 1)[1] for k in am["reads"]} | {k.split("|", 1)[1] for k in am["early"]}
        if "_rt_text" not in self.__dict__:
            src = self._text_index()["src"]
            parts = []
            for f, lines in self.runtime.items():
                text = src.get(f, "").split(chr(10))
                parts.extend(text[l - 1] for l in lines if 0 < l <= len(text))
            self._rt_text = chr(10).join(parts)
        for key in diff.get("changed", []):
            if key == "__modules__" or "|" not in key:
                return False
            left, name = key.split("|", 1)
            if name == "__mro__":
                return False
            mod_file = left.replace(".", "/")
            if (SRC / f"{mod_file}.py").exists() or (SRC / mod_file / "__init__.py").exists():
                if re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", self._rt_text):
                    return False
            elif name not in self._traced_names or key in self.runtime_attrs:
                return False
        return True

    def fp_refine(self, diff: dict | None, path: str, reasons: list[str]) -> set[int] | None:
        """Tests that read what an import-time change really changed, or None (keep the selection).

        `diff` lists the class attributes ("module.Class|NAME") and module values ("module|NAME")
        whose import-time value differs from the pinned commit. A class attribute is reached by the
        tests that read it during the test (traced per test, fresh interpreter); a module value by
        the tests running function code that names it. Anything not covered by those maps
        (new or removed entries, a changed class hierarchy, a module that no longer imports, a
        name never traced, such as a method) keeps the coverage selection.
        """
        am = self.attrmap()
        if diff is None or am is None or diff.get("added") or diff.get("removed"):
            return None
        if "_traced_names" not in self.__dict__:
            self._traced_names = {k.split("|", 1)[1] for k in am["reads"]} | {k.split("|", 1)[1] for k in am["early"]}
        got: set[int] = set()
        for key in diff.get("changed", []):
            if key == "__modules__" or "|" not in key:
                return None
            left, name = key.split("|", 1)
            if name == "__mro__":
                return None
            mod_file = left.replace(".", "/")
            if (SRC / f"{mod_file}.py").exists() or (SRC / mod_file / "__init__.py").exists():
                got |= self.tests_running_text([name], path, ast.Pass(lineno=0, end_lineno=0))
            else:
                if name not in self._traced_names:
                    return None
                got |= {n for n in am["reads"].get(key, []) if n < len(self.tests)}
                if name in am["dynamic"]:
                    got |= self.tests_running_text([name], path, ast.Pass(lineno=0, end_lineno=0))
            got |= self.tests_mentioning(name)
        got |= self.cover(path, self.touched_lines.get(path, []))
        reasons.append(f"fingerprint {len(diff.get('changed', []))} changed -> {len(got)}")
        return got

    # -- packing -------------------------------------------------------------

    def fill(self, workers: list[list[int]], extra) -> list[list[int]]:
        """Add extra tests wherever they fit under the busiest worker: they cost nothing."""
        if len(workers) < 2:
            return workers
        loads = [sum(self.ms[n] for n in w) for w in workers]
        cap = max(loads)
        have = {n for w in workers for n in w}
        for n in extra:
            if n in have:
                continue
            d = self.ms[n]
            best = None
            for i, l in enumerate(loads):
                if l + d <= cap and (best is None or l + d > loads[best] + d):
                    best = i
            if best is not None:
                workers[best].append(n)
                loads[best] += d
                have.add(n)
        return workers

    def pack(self, sel: set[int]) -> tuple[int, list[list[int]]]:
        if not sel:
            return 0, []
        items = sorted(sel, key=lambda n: -self.ms[n])
        best = None
        for w in range(1, 9):
            loads = [0] * w
            bins: list[list[int]] = [[] for _ in range(w)]
            for n in items:
                i = loads.index(min(loads))
                bins[i].append(n)
                loads[i] += self.ms[n]
            bins = [b for b in bins if b]
            cost = max(loads) + WORKER_COST * len(bins)
            if best is None or cost < best[0]:
                best = (cost, bins)
        return best


VERIFY_OVERHEAD = float(os.environ.get("BR_OVH", 0.35))  # s of CPU per verified commit (fork, patched modules, test module imports)
VERIFY_FACTOR = float(os.environ.get("BR_VF", 1.2))  # margin over each test's measured CPU here (cold caches in a fresh child)
PREPARE_COST = 1.2  # one-off: copy the tree and import numpy/pandas/duckdb
P_CODE = float(os.environ.get("BR_PCODE", 0.5))
P_DATA = float(os.environ.get("BR_PDATA", 0.9))
WIDE_REPLAY = os.environ.get("BR_WIDE_REPLAY", "0") == "1"
FP_MIN_SAVING = 4.0  # s: fingerprint only selections costing more than this (child costs ~1 s)
MARGIN = float(os.environ.get("BR_MARGIN", 0.4))


REPLAY_COST = {  # CPU seconds to fingerprint instead of running
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": 7.8,  # measured 7.4-7.9 s
    "tests/test_executor.py::TestExecutor::test_execute_tpch": 0.4,  # measured 0.27-0.34 s
    "tests/test_executor.py::TestExecutor::test_optimized_tpch": 0.4,  # measured 0.31 s
}


def vcost(sel: "Selector", n: int, replay_ok: bool) -> float:
    """ms of CPU to check test n here."""
    if replay_ok and n in sel.replay_tests:
        return REPLAY_COST.get(sel.replay_tests[n], 5.0) * 1000
    return VERIFY_FACTOR * 1000 * sel.tcost.get(n, sel.ms[n] / 1000)


def rep_ids(sel: "Selector", run, ok: bool) -> set[int]:
    return {n for n in run if ok and n in sel.replay_tests}


def plan_verify(sel: "Selector", chosen: set[int], p_fail: float, replay_ok: bool = False) -> tuple[list[int], int]:
    """Choose which candidate tests to check here. Returns (tests to check, expected saving in ms).

    Options: the k cheapest ordinary tests (k over ~24 split points) plus any subset of the
    replayable heavy tests. Expected cost = CPU spent + pack(unchecked) + a small charge for
    the failures a check may find (p_fail: fixture edits nearly always break their test).
    """
    if not chosen:
        return [], 0
    blind = sel.pack(chosen)[0]
    rep = sorted(n for n in chosen if replay_ok and n in sel.replay_tests)
    plain = sorted((n for n in chosen if n not in rep), key=lambda n: (sel.ms[n], n))
    if len(plain) <= 24:
        ks = list(range(0, len(plain) + 1))
    else:
        ks = sorted({0, len(plain)} | {max(1, round(len(plain) * i / 24)) for i in range(1, 25)})
    prefix = [0.0]
    seen_cls: set[str] = set()
    for n in plain:
        extra = 0.0
        c = sel.cls_key(n)
        if c not in seen_cls:
            seen_cls.add(c)
            extra = 1000 * sel.csetup.get(c, 0.0)
        prefix.append(prefix[-1] + vcost(sel, n, False) + extra)
    best_run: list[int] = []
    best_cost = float(blind)
    for mask in range(1 << len(rep)):
        rsub = [rep[b] for b in range(len(rep)) if mask >> b & 1]
        rcost = sum(vcost(sel, n, True) for n in rsub)
        for k in ks:
            if not rsub and k == 0:
                continue
            cpu = VERIFY_OVERHEAD * 1000 + rcost + prefix[k]
            if cpu >= best_cost:
                break
            rest = set(plain[k:]) | (set(rep) - set(rsub))
            cost = cpu + sel.pack(rest)[0] + p_fail * (2000 if not rest else 500)
            if cost < best_cost:
                best_run, best_cost = plain[:k] + rsub, cost
    return best_run, int(blind - best_cost)


def _fp_diff(base: dict, snap: dict) -> dict:
    changed = sorted(k for k in base if k in snap and snap[k] != base[k])
    added = sorted(k for k in snap if k not in base)
    removed = sorted(k for k in base if k not in snap)
    return {"changed": changed, "added": added, "removed": removed}


def main() -> None:
    start = time.monotonic()
    sel = Selector()
    patches = sorted((DATA / "commits").glob("*.patch"))
    # whole batch: 3 min wall (4.5 in the final, ~1.5x commits). Keep a wide safety margin.
    budget = float(os.environ.get("BR_BUDGET", 0.55 * 120 + 1.2 * len(patches)))
    verifier = None
    if Verifier is not None and os.environ.get("BR_VERIFY", "1") == "1" and hasattr(os, "fork"):
        verifier = Verifier(str(SRC), [t["id"] for t in sel.tests])
    results = []
    fp_jobs: dict[int, tuple[str, dict]] = {}
    replay_pending: dict[str, bool] = {}
    weak: dict[str, set[int]] = {}
    replay_ok: dict[str, bool] = {}
    verify_ok: dict[str, bool] = {}
    for patch in patches:
        chosen, reasons = sel.select(patch.read_text(encoding="utf-8"))
        results.append((patch.stem, chosen, reasons, dict(sel.files)))
        replay_ok[patch.stem] = sel.replay_ok and not sel.replay_fp
        # wider replay for class/module-level edits: cost CPU without saving anything on the
        # practice board, so it is off unless asked for
        replay_pending[patch.stem] = WIDE_REPLAY and sel.replay_ok and sel.replay_fp
        if not sel.replay_ok:
            reasons.append("noreplay:" + ",".join(sorted(sel.no_replay)))
        verify_ok[patch.stem] = sel.verify_ok
        weak[patch.stem] = sel.weak - chosen
        pend = replay_pending[patch.stem] and any(n in sel.replay_tests for n in chosen)
        if sel.fp_path or sel.fp_detect or pend:
            fp_jobs[len(results) - 1] = (sel.fp_path, dict(sel.touched_lines))
    # import-time changes: ask a patched child which class attributes / module values changed,
    # against a pristine snapshot taken the same way (same parent, modules, hash seed, paths)
    todo = list(fp_jobs)
    if verifier is not None and todo:
        base = None  # full pristine snapshot, taken on first need
        base_mods = None
        noisy: set[str] = set()
        try:
            verifier.prepare()
            c0 = sum(os.times()[:4])
            base_mods = verifier.import_check({}, timeout=20.0)
            base_mods = sorted(base_mods) if base_mods else None
            log(f"import check base cpu {sum(os.times()[:4]) - c0:.2f}")
        except Exception as exc:
            log("import check base failed", repr(exc))

        def full_base():
            nonlocal base
            if base is None:
                c0 = sum(os.times()[:4])
                a = verifier.snapshot({}, timeout=20.0)
                b = verifier.snapshot({}, timeout=20.0) if a else None
                if a and b:
                    # entries that differ between two pristine imports (orders built from
                    # object ids) say nothing about a patch: drop them
                    for k in set(a) | set(b):
                        if a.get(k) != b.get(k):
                            a.pop(k, None)
                            noisy.add(k)
                    base = a
                else:
                    base = {}
                log(f"fingerprint base cpu {sum(os.times()[:4]) - c0:.2f}, noisy {len(noisy)}")
            return base or None

        for i in sorted(todo, key=lambda k: -sel.pack(results[k][1])[0]) if base_mods else []:
            path, touched = fp_jobs[i]
            cid, chosen, reasons, files = results[i]
            left = budget - (time.monotonic() - start)
            if left < 20:
                break
            c0 = sum(os.times()[:4])
            refine = path is not None and sel.pack(chosen)[0] >= FP_MIN_SAVING * 1000
            pend = replay_pending.get(cid) and any(n in sel.replay_tests for n in chosen)
            full = (refine or pend) and full_base() is not None
            refine = refine and full
            if full:
                snap = verifier.snapshot(files, timeout=min(20.0, left - 5))
                diff = None if snap is None else _fp_diff(base, {k: v for k, v in snap.items() if k not in noisy})
            else:
                # detection only: do all modules still import?
                mods = verifier.import_check(files, timeout=min(20.0, left - 5))
                diff = None if mods is None else {"changed": [] if sorted(mods) == base_mods else ["__modules__"]}
                reasons.append(f"fp check cpu {sum(os.times()[:4]) - c0:.2f}")
            if diff is None or "__modules__" in diff["changed"]:
                # a module no longer imports (or the check itself did not finish): anything may fail
                results[i] = (cid, set(sel.all), reasons, files)
                reasons.append("fp: a module fails to import -> all")
                continue
            if pend and full:
                replay_ok[cid] = sel.replay_safe(diff)
                reasons.append(f"fp replay {'ok' if replay_ok[cid] else 'no'}")
            if not refine:
                reasons.append("fp: imports ok")
                continue
            sel.touched_lines = touched
            refined = sel.fp_refine(diff, path, reasons)
            reasons.append(f"fp cpu {sum(os.times()[:4]) - c0:.2f}")
            if refined is not None:
                narrowed = chosen & refined
                weak[cid] |= chosen - narrowed
                results[i] = (cid, narrowed, reasons, files)
                reasons.append(f"fp {len(chosen)} -> {len(narrowed)}")
    # verify where it pays, biggest expected saving first, within the time budget
    plans = []
    for i, (cid, chosen, reasons, files) in enumerate(results):
        if not verify_ok[cid]:
            reasons.append("no verify (pickling code)")
            continue
        code_change = any(r.startswith(("line ", "import-time", "def-header", "syntax", "new file", "delete", "error")) for r in reasons)
        run, saving = plan_verify(sel, chosen, P_CODE if code_change else P_DATA, replay_ok[cid])
        if run and saving > MARGIN * 1000:
            plans.append((saving, i, run))
    plans.sort(reverse=True)
    if verifier is not None and plans and sum(p[0] for p in plans) > (PREPARE_COST + 0.5) * 1000:
        try:
            c0 = sum(os.times()[:4])
            verifier.prepare()
            log(f"prepare cpu {sum(os.times()[:4]) - c0:.2f}")
        except Exception as exc:
            log("prepare failed", repr(exc))
            verifier = None
        for saving, i, run in plans if verifier else []:
            left = budget - (time.monotonic() - start)
            ok = replay_ok[results[i][0]]
            est = VERIFY_OVERHEAD + sum(vcost(sel, n, ok) for n in run) / 1000 + sum(
                sel.csetup.get(c, 0.0) for c in {sel.cls_key(n) for n in run if n not in rep_ids(sel, run, ok)})
            if left < est * 3 + 2:
                continue
            cid, chosen, reasons, files = results[i]
            rep = {n: sel.replay_ref[sel.replay_tests[n]] for n in run if ok and n in sel.replay_tests}
            c0 = sum(os.times()[:4])
            failed = verifier.run(files, run, timeout=min(left - 1, 3 * est + 2), replay=rep)
            reasons.append(f"cpu {sum(os.times()[:4]) - c0:.2f}/est {est:.2f}")
            if failed is None:
                reasons.append(f"verify {len(run)} -> unfinished, keep all")
                continue
            keep = (chosen - set(run)) | (failed & set(run))
            reasons.append(f"verify {len(run)} (replay {len(rep)}) -> {len(failed)} fail")
            results[i] = (cid, keep, reasons, files)
    for cid, chosen, reasons, files in results:
        cost, workers = sel.pack(chosen)
        extra = sorted(weak.get(cid, ()), key=lambda n: sel.ms[n])
        workers = sel.fill(workers, extra)
        print(json.dumps({"commit": cid, "workers": workers}))
        log(cid, len(chosen), round(cost / 1000, 2), "; ".join(reasons))
    log(f"wall {time.monotonic() - start:.2f}s cpu {sum(os.times()[:4]):.2f}s")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--build-text-index"]:
        import pickle

        data = gzip.compress(pickle.dumps(build_text_index(SRC)), compresslevel=6)
        Path(sys.argv[2]).write_bytes(data)
        print("text index", len(data))
    else:
        main()
