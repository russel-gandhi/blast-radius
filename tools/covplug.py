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
        s = _lines.get(_ctx)
        if s is None:
            s = _lines[_ctx] = set()
        s.add((fn[len(ROOT):], line))
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
