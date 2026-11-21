"""Run a few sqlglot tests against a patched tree, in a forked child, and report which fail.

The parent copies the pinned sqlglot checkout to a writable scratch tree once and pre-imports
heavy third-party modules (numpy, pandas, duckdb) that do not depend on sqlglot. For each patch
a child process is forked: it writes the patched files into the tree, imports the test modules
fresh, runs the chosen tests with unittest (sqlglot's tests are unittest TestCases) and reports
the failing ones. The parent restores the tree afterwards.

A test counts as failing on a failure, an error, a failed subtest, an error in its class or
module setup, or when its module cannot be imported, matching how the track labels failures.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import time

TREE = os.environ.get("BR_TREE", "/tmp/w")


def cpu_now() -> float:
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system


class Verifier:
    def __init__(self, src: str, test_ids: list[str]):
        self.src = src
        self.ids = test_ids
        self.ready = False

    def prepare(self) -> None:
        if self.ready:
            return
        if os.path.exists(TREE):
            shutil.rmtree(TREE)
        # symlink overlay: real directories, every file a link to the pinned original.
        # Patched files replace their link with a real file for one run. Far cheaper than a copy,
        # and precompiled .pyc files stay valid because stat() follows the links.
        for d in ("sqlglot", "tests"):
            base = os.path.join(self.src, d)
            for root, dirs, fs in os.walk(base):
                rel = os.path.relpath(root, self.src)
                os.makedirs(os.path.join(TREE, rel), exist_ok=True)
                for f in fs:
                    os.symlink(os.path.join(root, f), os.path.join(TREE, rel, f))
        os.environ["SKIP_INTEGRATION"] = "0"
        os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        sys.dont_write_bytecode = True
        import unittest  # noqa: F401  (sqlglot records whether unittest is loaded: keep it constant)

        self.ready = True

    def warm(self) -> bool:
        """Load the pinned sqlglot and every test module into this process, once.

        Children forked afterwards start with everything imported (~0.01 s instead of ~0.35 s).
        Edits confined to function bodies are applied in the child by swapping the functions'
        code objects; any other edit gets a child that drops these modules and imports afresh.
        Must run after every import check / snapshot (those need a parent without sqlglot).
        """
        if getattr(self, "_warm", None) is not None:
            return self._warm
        self.prepare()
        self.heavy()
        saved = os.dup(1)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)  # module imports must not print onto the answers
        try:
            if TREE not in sys.path:
                sys.path.insert(0, TREE)
            _install_inprocess_pool()
            import glob
            import importlib

            import fingerprint as fp

            fp.import_all()
            for path in sorted(glob.glob(os.path.join(TREE, "tests", "**", "test_*.py"), recursive=True)):
                mod = os.path.relpath(path, TREE)[:-3].replace(os.sep, ".")
                try:
                    importlib.import_module(mod)
                except Exception:
                    pass
            self._warm = True
        except Exception:
            self._warm = False
        finally:
            os.dup2(saved, 1)
            os.close(saved)
            os.close(devnull)
        return self._warm

    def heavy(self) -> None:
        """numpy / pandas / duckdb, imported once before the first test run (not for import checks)."""
        if getattr(self, "_heavy", False):
            return
        for mod in ("numpy", "pandas", "duckdb", "pandas.testing"):
            try:
                __import__(mod)
            except Exception:
                pass
        self._heavy = True

    def run(self, files: dict[str, str | None], tests: list[int], timeout: float,
            replay: dict[int, list[str]] | None = None, swaps: list | None = None) -> set[int] | None:
        """Return failing test numbers among `tests`, or None if the run did not finish.

        swaps: [(path, module, [function qualnames])] for an edit confined to those function
        bodies, run in a warm child; None runs in a child that imports everything afresh.
        """
        self.prepare()
        self.heavy()
        warm = swaps is not None and bool(getattr(self, "_warm", False))
        purge = bool(getattr(self, "_warm", False)) and not warm
        job_swaps = [(rel, mod, quals, files.get(rel)) for rel, mod, quals in swaps] if warm else None
        got = self._with_files(files, lambda: _child(self.ids, tests, replay or {}, job_swaps, purge), timeout)
        return None if got is None else set(got)

    def snapshot(self, files: dict[str, str | None], timeout: float) -> dict | None:
        """Import-time snapshot (fingerprint.snapshot) of the tree with `files` applied, or None.

        The pristine snapshot is taken the same way ({} files), so both come from children forked
        from this parent: same loaded modules, same hash seed, same paths.
        """

        def job():
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)  # stdout carries the selector's answers: nothing else may print
            os.dup2(devnull, 2)
            os.chdir(TREE)
            sys.path.insert(0, TREE)
            import fingerprint as fp

            return fp.snapshot()

        return self._with_files(files, job, timeout)

    def import_check(self, files: dict[str, str | None], timeout: float) -> list | None:
        """Names of the sqlglot modules after importing all of them ("name!" = failed), or None."""

        def job():
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            os.chdir(TREE)
            sys.path.insert(0, TREE)
            import fingerprint as fp

            return fp.import_all()

        return self._with_files(files, job, timeout)

    def _with_files(self, files, job, timeout):
        self.prepare()
        saved: dict[str, bool] = {}
        for rel in files:
            saved[rel] = os.path.lexists(os.path.join(TREE, rel))
        try:
            for rel, text in files.items():
                p = os.path.join(TREE, rel)
                if os.path.lexists(p):
                    os.remove(p)
                if text is not None:
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    with open(p, "w", encoding="utf-8") as fh:
                        fh.write(text)
            return self._fork(job, timeout)
        finally:
            for rel, existed in saved.items():
                p = os.path.join(TREE, rel)
                if os.path.lexists(p):
                    os.remove(p)
                if existed:
                    os.symlink(os.path.join(self.src, rel), p)

    def _fork(self, job, timeout: float):
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:  # child
            os.close(r)
            code = 0
            try:
                result = job()
                if isinstance(result, set):
                    result = sorted(result)
                data = json.dumps(result).encode()
                while data:
                    n = os.write(w, data)
                    data = data[n:]
            except BaseException:
                code = 1
            finally:
                os._exit(code)
        os.close(w)
        deadline = time.monotonic() + timeout
        chunks = []
        os.set_blocking(r, False)
        status = None
        while True:
            try:
                b = os.read(r, 65536)
                if b:
                    chunks.append(b)
            except BlockingIOError:
                pass
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            if time.monotonic() > deadline:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                os.close(r)
                return None
            time.sleep(0.002)
        while True:
            try:
                b = os.read(r, 65536)
            except BlockingIOError:
                break
            if not b:
                break
            chunks.append(b)
        os.close(r)
        if status != 0 or not chunks:
            return None
        return json.loads(b"".join(chunks))


def _install_inprocess_pool():
    """Tests hand work to spawned process pools. Run it in-process (same pickling) instead."""
    import concurrent.futures as cf
    import pickle

    def rt(obj):
        return pickle.loads(pickle.dumps(obj))

    def call(fn, args, kwargs):
        return rt(fn(*rt(args), **rt(kwargs)))

    class InProcessPool(cf.ThreadPoolExecutor):
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
            if initializer is not None:
                init, iargs = initializer, initargs
                initializer, initargs = (lambda: init(*rt(iargs))), ()
            super().__init__(max_workers=1, initializer=initializer, initargs=initargs)

        def submit(self, fn, /, *args, **kwargs):
            return super().submit(call, fn, args, kwargs)

        def map(self, fn, *iterables, timeout=None, chunksize=1, **_):
            return super().map(lambda *a: call(fn, a, {}), *iterables, timeout=timeout)

    cf.ProcessPoolExecutor = InProcessPool


def _hot_swap(swaps) -> None:
    """Give already-loaded functions the code of their patched versions (bodies only)."""
    import types

    for rel, modname, quals, text in swaps:
        mod = sys.modules.get(modname)
        if mod is None:
            continue  # not loaded in the parent: it imports from the patched overlay
        if text is None:
            raise RuntimeError("deleted module")
        code = compile(text, os.path.join(TREE, rel), "exec", dont_inherit=True)
        table: dict[str, list] = {}

        def walk(c):
            for k in c.co_consts:
                if isinstance(k, types.CodeType):
                    table.setdefault(k.co_qualname, []).append(k)
                    walk(k)

        walk(code)
        for q in quals:
            cands = table.get(q, [])
            if len(cands) != 1:
                raise RuntimeError(f"ambiguous {q}")
            obj = mod
            parts = q.split(".")
            for i, part in enumerate(parts):
                obj = getattr(obj, part) if i == 0 else vars(obj)[part]
            if isinstance(obj, (staticmethod, classmethod)):
                obj = obj.__func__
            elif isinstance(obj, property):
                fns = [f for f in (obj.fget, obj.fset, obj.fdel) if f is not None]
                if len(fns) != 1:
                    raise RuntimeError(f"property with several functions {q}")
                obj = fns[0]
            if not isinstance(obj, types.FunctionType) or obj.__code__.co_qualname != q:
                raise RuntimeError(f"not a plain function {q}")
            new = cands[0]
            if new.co_freevars != obj.__code__.co_freevars:
                raise RuntimeError(f"closure changed {q}")
            obj.__code__ = new


def _child(ids: list[str], tests: list[int], replay: dict[int, list[str]], swaps=None, purge=False) -> set[int]:
    import io
    import unittest

    os.chdir(TREE)
    if TREE not in sys.path:
        sys.path.insert(0, TREE)
    if purge:
        # forked from a warm parent but this edit needs a fresh import of the patched tree
        for name in [m for m in sys.modules if m == "sqlglot" or m.startswith(("sqlglot.", "tests.")) or m == "tests"]:
            del sys.modules[name]
    if swaps:
        _hot_swap(swaps)
    # stdout belongs to the selector's answers: silence everything the tests print
    devnull = open(os.devnull, "w")
    os.dup2(devnull.fileno(), 1)
    os.dup2(devnull.fileno(), 2)
    sys.stdout = sys.stderr = io.StringIO()
    _install_inprocess_pool()

    failed: set[int] = set()
    # replayed tests: same fingerprints as on the pinned commit (where they pass) -> pass, else keep
    if replay:
        import replay as rp

        for n, ref in replay.items():
            try:
                if not rp.matches(*rp.TESTS[ids[n]], ref):
                    failed.add(n)
            except BaseException:
                failed.add(n)
    tests = [n for n in tests if n not in replay]
    by_name: dict[str, int] = {}
    groups: dict[str, list[tuple[str, int]]] = {}
    for n in tests:
        path, cls, meth = ids[n].split("::")
        mod = path[:-3].replace("/", ".")
        groups.setdefault(mod, []).append((f"{cls}.{meth}", n))

    class Result(unittest.TestResult):
        def _mark(self, test):
            tid = getattr(test, "id", lambda: "")()
            n = by_name.get(tid)
            if n is None:
                # class/module fixture errors, e.g. "setUpClass (tests.x.Cls)": every test under it
                desc = str(test)
                owner = desc[desc.find("(") + 1 : desc.rfind(")")] if "(" in desc else desc
                hit = [num for name, num in by_name.items() if name.startswith(owner + ".") or name == owner]
                failed.update(hit if hit else by_name.values())
                return
            failed.add(n)

        def addError(self, test, err):
            super().addError(test, err)
            self._mark(test)

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self._mark(test)

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                self._mark(test)

        def addSkip(self, test, reason):
            # a skip here is not proof of a pass where the track runs it: keep the test
            super().addSkip(test, reason)
            self._mark(test)

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            self._mark(test)

    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for mod, items in groups.items():
        try:
            __import__(mod)
            module = sys.modules[mod]
        except BaseException:
            failed.update(n for _, n in items)
            continue
        for name, n in items:
            by_name[f"{mod}.{name}"] = n
            try:
                suite.addTests(loader.loadTestsFromName(name, module))
            except BaseException:
                failed.add(n)
    suite.run(Result())
    return failed
