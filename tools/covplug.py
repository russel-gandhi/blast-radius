"""pytest plugin: record, per test and phase, which sqlglot/tests lines run and which files are opened.

Uses sys.monitoring LINE events, disabled after the first hit and re-armed at
every context switch, so each (context, line) is recorded once.
"""

import json
import os
import sys

import pytest

ROOT = "/sqlglot/"
mon = sys.monitoring
TOOL = mon.COVERAGE_ID

_ctx = "collect"
_lines: dict[str, set] = {}
_opens: dict[str, set] = {}


def _switch(ctx):
    global _ctx
    _ctx = ctx
    mon.restart_events()


def _line(code, line):
    fn = code.co_filename
    if fn.startswith(ROOT):
        key = (fn[len(ROOT):], line)
        s = _lines.get(_ctx)
        if s is None:
            s = _lines[_ctx] = set()
        s.add(key)
        if _import_depth:
            # also note it ran during an import: its effect is shared by later tests too
            _lines.setdefault("import", set()).add(key)
    return mon.DISABLE


def _audit(event, args):
    if event == "open":
        p = args[0]
        if isinstance(p, bytes):
            p = p.decode(errors="replace")
        if isinstance(p, str):
            p = os.path.abspath(p)
            if p.startswith(ROOT):
                _opens.setdefault(_ctx, set()).add(p[len(ROOT):])


# sqlglot loads dialect modules lazily, so a module body (and every function it calls, such as
# metaclasses building dialect classes) runs inside whichever test imports it first, while every
# later test depends on the result. Those lines stay credited to the test that ran them (it may
# itself be about importing) and are also recorded under "import".
import importlib.abc as _iabc  # noqa: E402
import importlib.machinery as _imach  # noqa: E402

_import_depth = 0
_saved_ctx = []


class _ImportTracker(_iabc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if not (fullname == "sqlglot" or fullname.startswith(("sqlglot.", "tests"))):
            return None
        spec = _imach.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None or not hasattr(spec.loader, "exec_module"):
            return spec
        inner = spec.loader.exec_module

        def exec_module(module, _inner=inner):
            global _import_depth
            _import_depth += 1
            if _import_depth == 1:
                mon.restart_events()  # re-arm lines already seen in this test, to mark them
            try:
                return _inner(module)
            finally:
                _import_depth -= 1
                if _import_depth == 0:
                    mon.restart_events()  # re-arm lines marked during the import for the test

        spec.loader.exec_module = exec_module
        return spec


sys.meta_path.insert(0, _ImportTracker())

# Tests hand work to spawned process pools, which this tracer cannot see. Run that work
# in-process on one thread instead so its lines are credited to the test that asked for it.
import concurrent.futures as _cf


import pickle as _pickle


def _rt(obj):
    # a real process pool pickles everything it sends and receives (sqlglot's serde runs here)
    return _pickle.loads(_pickle.dumps(obj))


def _call_roundtrip(fn, args, kwargs):
    return _rt(fn(*_rt(args), **_rt(kwargs)))


class _InProcessPool(_cf.ThreadPoolExecutor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        if initializer is not None:
            init, iargs = initializer, initargs
            initializer, initargs = (lambda: init(*_rt(iargs))), ()
        super().__init__(max_workers=1, initializer=initializer, initargs=initargs)

    def submit(self, fn, /, *args, **kwargs):
        return super().submit(_call_roundtrip, fn, args, kwargs)

    def map(self, fn, *iterables, timeout=None, chunksize=1, **_):
        return super().map(lambda *a: _call_roundtrip(fn, a, {}), *iterables, timeout=timeout)


_cf.ProcessPoolExecutor = _InProcessPool

mon.use_tool_id(TOOL, "covplug")
mon.register_callback(TOOL, mon.events.LINE, _line)
mon.set_events(TOOL, mon.events.LINE)
sys.addaudithook(_audit)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item):
    _switch("S|" + item.nodeid)
    try:
        return (yield)
    finally:
        _switch("idle")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    _switch("C|" + item.nodeid)
    try:
        return (yield)
    finally:
        _switch("idle")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _switch("T|" + item.nodeid)
    try:
        return (yield)
    finally:
        _switch("idle")


def pytest_sessionfinish(session):
    out = os.environ.get("COVOUT", "/work/cov.json")
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    data = {
        "lines": {k: sorted(v) for k, v in _lines.items()},
        "opens": {k: sorted(v) for k, v in _opens.items()},
    }
    with open(out.replace(".json", f".{wid}.json"), "w") as f:
        json.dump(data, f)
