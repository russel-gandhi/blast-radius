"""pytest plugin for tracing ONE test per fresh interpreter (no caches shared between tests).

Records, for the single test this process runs:
  - lines run during the test's setup/call/teardown ("test"), and lines run while any sqlglot or
    test module was being imported, before or during the test ("boot" / "import"),
  - reads of class-level attributes of sqlglot classes: every `X.NAME` load in sqlglot source, where
    NAME is assigned in some sqlglot class body, is compiled as `_BR_attr_(X, "NAME")`. It returns
    getattr(X, NAME) and records the class that owns NAME for X (first class in the MRO whose
    __dict__ has it), with the phase: "boot" (before the test), "import" (a module body ran), "test".

Output: $ISOOUT (json): {"test": nodeid, "lines": {"test": [...], "import": [...]}, "attrs": [[owner, name, phase], ...]}
"""

import ast
import importlib.abc
import importlib.machinery
import json
import os
import sys

import pytest

ROOT = "/sqlglot/"
mon = sys.monitoring
TOOL = mon.COVERAGE_ID

phase = "boot"
import_depth = 0
LINES = {"test": set(), "import": set(), "boot": set(), "after": set()}
ATTRS: set = set()
NODEID = []


def _cur():
    return "import" if import_depth else phase


def _line(code, ln):
    fn = code.co_filename
    if fn.startswith(ROOT):
        LINES[_cur()].add((fn[len(ROOT):], ln))
    return mon.DISABLE


mon.use_tool_id(TOOL, "iso")
mon.register_callback(TOOL, mon.events.LINE, _line)
mon.set_events(TOOL, mon.events.LINE)


def _class_attr_names():
    names = set()
    for root, _, files in os.walk(ROOT + "sqlglot"):
        for f in files:
            if not f.endswith(".py"):
                continue
            try:
                tree = ast.parse(open(os.path.join(root, f), encoding="utf-8").read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for st in node.body:
                        targets = st.targets if isinstance(st, ast.Assign) else [st.target] if isinstance(st, ast.AnnAssign) else []
                        for t in targets:
                            if isinstance(t, ast.Name):
                                names.add(t.id)
    return names


NAMES = _class_attr_names()


def _owner(x, name):
    cls = x if isinstance(x, type) else type(x)
    for k in getattr(cls, "__mro__", ()):
        if name in vars(k):
            return f"{k.__module__}.{k.__qualname__}"
    return None


def _BR_attr_(x, name):
    v = getattr(x, name)
    try:
        o = _owner(x, name)
        if o is not None and o.startswith("sqlglot"):
            ATTRS.add((o, name, _cur()))
    except Exception:
        ATTRS.add(("?", name, _cur()))
    return v


import builtins  # noqa: E402

builtins._BR_attr_ = _BR_attr_


class _Rewrite(ast.NodeTransformer):
    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and node.attr in NAMES:
            call = ast.Call(func=ast.Name(id="_BR_attr_", ctx=ast.Load()),
                            args=[node.value, ast.Constant(node.attr)], keywords=[])
            return ast.copy_location(call, node)
        return node


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = self.get_filename(fullname)
        tree = ast.parse(self.get_data(path).decode("utf-8"), path)
        tree = _Rewrite().visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True)

    def exec_module(self, module):
        global import_depth
        import_depth += 1
        mon.restart_events()
        try:
            super().exec_module(module)
        finally:
            import_depth -= 1
            mon.restart_events()


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if not (fullname == "sqlglot" or fullname.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            return spec
        spec.loader = _Loader(fullname, spec.origin)
        return spec


sys.meta_path.insert(0, _Finder())

# process pools run in-process, with the same pickling, so their work belongs to this test
import concurrent.futures as _cf  # noqa: E402
import pickle as _pickle  # noqa: E402


def _rt(o):
    return _pickle.loads(_pickle.dumps(o))


def _call(fn, a, k):
    return _rt(fn(*_rt(a), **_rt(k)))


class _Pool(_cf.ThreadPoolExecutor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        if initializer is not None:
            i, ia = initializer, initargs
            initializer, initargs = (lambda: i(*_rt(ia))), ()
        super().__init__(max_workers=1, initializer=initializer, initargs=initargs)

    def submit(self, fn, /, *a, **k):
        return super().submit(_call, fn, a, k)

    def map(self, fn, *its, timeout=None, chunksize=1, **_):
        return super().map(lambda *a: _call(fn, a, {}), *its, timeout=timeout)


_cf.ProcessPoolExecutor = _Pool


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global phase
    NODEID.append(item.nodeid)
    phase = "test"
    mon.restart_events()
    try:
        return (yield)
    finally:
        phase = "after"
        LINES.setdefault("after", set())


def pytest_sessionfinish(session):
    out = os.environ["ISOOUT"]
    data = {
        "test": NODEID[0] if NODEID else None,
        "lines": {k: sorted(v) for k, v in LINES.items() if k in ("test", "import")},
        "attrs": sorted(ATTRS),
    }
    with open(out, "w") as f:
        json.dump(data, f)
