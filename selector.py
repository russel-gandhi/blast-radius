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
from pathlib import Path

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

    @property
    def path(self) -> str:
        return self.old or self.new


def parse_patch(text: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    cur: FilePatch | None = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
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
        # tests touching each file at test time (any line)
        self._file_tests: dict[str, set[int]] = {}

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
        src_path = SRC / (fp.old or fp.new)
        old_text = src_path.read_text(encoding="utf-8") if fp.old and src_path.exists() else ""
        new_text, removed, inserts = apply(fp, old_text)
        if fp.new is None:  # deleted file
            reasons.append(f"delete {path}")
            return self.opened_by(path) | self.importers(path)
        if path.endswith(".py"):
            return self.select_py(path, old_text, new_text, removed, inserts, reasons)
        # data files: whoever opened them
        sel = self.opened_by(path)
        reasons.append(f"data {path} -> {len(sel)}")
        return sel

    def opened_by(self, path: str) -> set[int]:
        sid = self.opens.get(path)
        return self.set_tests(sid) if sid is not None else set()

    def importers(self, path: str) -> set[int]:
        if path.startswith("tests/"):
            return set(self.by_file.get(path, [])) or set(self.all)
        return set(self.all)

    def select_py(self, path, old_text, new_text, removed, inserts, reasons) -> set[int]:
        try:
            new_tree = ast.parse(new_text)
        except SyntaxError:
            reasons.append(f"syntax error {path}")
            return self.importers(path)
        try:
            old_tree = ast.parse(old_text)
        except SyntaxError:
            old_tree = None
        if old_tree is not None and ast.dump(old_tree) == ast.dump(new_tree):
            sel = self.opened_by(path)
            reasons.append(f"ast-equal {path} -> {len(sel)}")
            return sel
        if old_tree is None:
            reasons.append(f"new file {path}")
            return self.importers(path)

        stmts = stmt_index(old_tree)
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
                    got = set(self.by_file.get(path, []))
                else:
                    got = self.family_tests(path) | self.cover(path, rng)
                sel |= got
                reasons.append(f"import-time {path}:{line} -> {len(got)}")
                continue
            got = self.cover(path, rng)
            sel |= got
            reasons.append(f"line {path}:{line} [{s}-{rng[-1]}] -> {len(got)}")
        return sel

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
        for fp in parse_patch(patch_text):
            try:
                sel |= self.select_file(fp, reasons)
            except Exception as exc:  # never crash: fall back to everything
                reasons.append(f"error {fp.path}: {exc!r}")
                sel |= set(self.all)
        return sel, reasons

    # -- packing -------------------------------------------------------------

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


def main() -> None:
    sel = Selector()
    for patch in sorted((DATA / "commits").glob("*.patch")):
        chosen, reasons = sel.select(patch.read_text())
        cost, workers = sel.pack(chosen)
        print(json.dumps({"commit": patch.stem, "workers": workers}))
        log(patch.stem, len(chosen), round(cost / 1000, 2), "; ".join(reasons))


if __name__ == "__main__":
    main()
