"""pytest plugin: record what every single comparison inside sqlglot functions saw, per test.

sqlglot modules are compiled from a rewritten AST: each `a OP b` (one operator, inside a function
or lambda) becomes `_BR_cmp_(site, a, b, OP)`, which evaluates exactly what the original did
(same order, same exceptions) and records an outcome key:
  - the result of the original operator,
  - what the related operators would return on the same operands when that is cheap and safe
    (== != is is-not for equality, < <= > >= == != for ordering),
  - the operand values themselves when both are small primitives (for constant edits).
Unknown alternatives are recorded as None. Keys are grouped by test context ("S|", "C|", "T|"
phases, or "import" while a sqlglot module body runs). Sites with too many distinct keys are
marked overflowing and never used.

Output: $OPOUT.<worker>.json with {site: {"ovf": bool, "keys": {key_json: [contexts]}}}
site = "file:line:col:end_line:end_col" of the Compare node in the original source.
"""

import ast
import builtins
import enum
import importlib.abc
import importlib.machinery
import json
import operator
import os
import sys

import pytest

ROOT = "/sqlglot/"
MAXKEYS = 256
_ctx = "collect"
_importing = 0
REC: dict = {}
OVF: set = set()

OPS = {
    "Eq": operator.eq, "NotEq": operator.ne, "Lt": operator.lt, "LtE": operator.le, "Gt": operator.gt,
    "GtE": operator.ge, "Is": operator.is_, "IsNot": operator.is_not,
    "In": lambda a, b: a in b, "NotIn": lambda a, b: a not in b,
}
EQ_GROUP = ("Eq", "NotEq", "Is", "IsNot")
ORD_GROUP = ("Lt", "LtE", "Gt", "GtE", "Eq", "NotEq")
CHEAP = (type(None), bool, int, float, str, bytes)


def _cheap(v):
    return isinstance(v, CHEAP) or isinstance(v, enum.Enum) or isinstance(v, type)


def _val(v):
    if isinstance(v, (bool, type(None))):
        return [type(v).__name__, v]
    if isinstance(v, (int, float)) and abs(v) < 1e15:
        return [type(v).__name__, v]
    if isinstance(v, str) and len(v) <= 48:
        return ["str", v]
    return None


EQ_SET = frozenset(EQ_GROUP)
ORD_SET = frozenset(("Lt", "LtE", "Gt", "GtE"))
CHEAP_T = frozenset((type(None), bool, int, float, str, bytes))


def _key(op, a, b, r):
    """Hashable outcome key: (op, result, alternatives..., values). Built from tuples: this runs a lot."""
    ta, tb = type(a), type(b)
    cheap = (ta in CHEAP_T or isinstance(a, (enum.Enum, type))) and (tb in CHEAP_T or isinstance(b, (enum.Enum, type)))
    if op in EQ_SET:
        is_ = a is b
        if cheap or a is None or b is None:
            # comparing anything with None is cheap; a custom __eq__ that errors or returns a
            # non-bool leaves the alternative unknown
            try:
                eq = a == b
            except Exception:
                eq = None
            if type(eq) is not bool:
                eq = None
        elif op == "Eq":
            eq = r
        elif op == "NotEq":
            eq = not r
        else:
            eq = None
        alts = (is_, eq)
    elif op in ORD_SET and cheap:
        try:
            alts = (a < b, a <= b, a > b, a >= b, a == b)
        except Exception:
            alts = None
    else:
        alts = None
    vals = None
    if cheap and (ta is not str or len(a) <= 48) and (tb is not str or len(b) <= 48):
        if ta not in (bytes,) and tb not in (bytes,) and not isinstance(a, type) and not isinstance(b, type):
            vals = (ta.__name__, a if not isinstance(a, enum.Enum) else None, tb.__name__, b if not isinstance(b, enum.Enum) else None)
    return (op, r, alts, vals)


def _BR_cmp_(site, a, b, op):
    r = OPS[op](a, b)
    if type(r) is not bool or site in OVF:
        if type(r) is not bool:
            OVF.add(site)
        return r
    try:
        k = _key(op, a, b, r)
        hash(k)
    except Exception:
        OVF.add(site)
        return r
    d = REC.get(site)
    if d is None:
        d = REC[site] = {}
    s = d.get(k)
    if s is None:
        if len(d) >= MAXKEYS:
            OVF.add(site)
            return r
        s = d[k] = set()
    s.add("import" if _importing else _ctx)
    return r


builtins._BR_cmp_ = _BR_cmp_


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel
        self.depth = 0

    def _fn(self, node):
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _fn

    def visit_Compare(self, node):
        self.generic_visit(node)
        if self.depth == 0 or len(node.ops) != 1:
            return node
        site = f"{self.rel}:{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}"
        call = ast.Call(
            func=ast.Name(id="_BR_cmp_", ctx=ast.Load()),
            args=[ast.Constant(site), node.left, node.comparators[0], ast.Constant(type(node.ops[0]).__name__)],
            keywords=[],
        )
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = self.get_filename(fullname)
        src = self.get_data(path).decode("utf-8")
        tree = ast.parse(src, path)
        tree = _Rewrite(path[len(ROOT):]).visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True)

    def exec_module(self, module):
        global _importing
        _importing += 1
        try:
            super().exec_module(module)
        finally:
            _importing -= 1


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if not (fullname == "sqlglot" or fullname.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            return spec
        if not spec.origin.startswith(ROOT + "sqlglot/"):
            return spec
        spec.loader = _Loader(fullname, spec.origin)
        return spec


sys.meta_path.insert(0, _Finder())

# process-pool work runs in-process (with the same pickling) so it is credited to its test
import concurrent.futures as _cf  # noqa: E402
import pickle as _pickle  # noqa: E402


def _rt(obj):
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


def _switch(c):
    global _ctx
    _ctx = c


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
    out = os.environ.get("OPOUT", "/work/ops.json")
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    data = {}
    for site, d in REC.items():
        data[site] = {"ovf": site in OVF, "keys": [[list(k), sorted(v)] for k, v in d.items()]}
    for site in OVF - set(REC):
        data[site] = {"ovf": True, "keys": []}
    with open(out.replace(".json", f".{wid}.json"), "w") as f:
        json.dump(data, f)
