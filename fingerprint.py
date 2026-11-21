"""Import-time fingerprint of sqlglot: a hash of every class attribute and module global.

After importing every sqlglot module, each class defined in sqlglot contributes one entry per
attribute in its own __dict__ ("module.Qual|NAME"), and each module one entry per global
("module|NAME"). Values are reduced to a stable digest: containers recursively (sets sorted),
classes and functions by qualified name (plus defaults and closure values for functions: their
bodies are covered by line coverage), primitives by repr, other objects by type and attributes.
Nothing depends on memory addresses or hash seeds.

usage: python fingerprint.py <out.json.gz>   (with the sqlglot checkout first on sys.path)
"""

from __future__ import annotations

import enum
import gzip
import hashlib
import importlib
import json
import pkgutil
import re
import sys
import types

_PRIM = (type(None), bool, int, float, complex, str, bytes)


def _desc(v, depth=0, seen=None) -> str:
    if seen is None:
        seen = set()
    if depth > 8:
        return "<deep>"
    t = type(v)
    if t in _PRIM:
        return f"{t.__name__}:{v!r}"
    if isinstance(v, enum.Enum):
        return f"enum:{type(v).__module__}.{type(v).__qualname__}.{v.name}={_desc(v.value, depth + 1, seen)}"
    if isinstance(v, type):
        return f"cls:{v.__module__}.{v.__qualname__}"
    if isinstance(v, (types.FunctionType, types.BuiltinFunctionType, types.MethodType)):
        f = getattr(v, "__func__", v)
        parts = [f"fn:{getattr(f, '__module__', '')}.{getattr(f, '__qualname__', getattr(f, '__name__', '?'))}"]
        if isinstance(f, types.FunctionType):
            if id(f) in seen:
                return parts[0]
            seen = seen | {id(f)}
            if f.__defaults__:
                parts.append("d=" + _desc(f.__defaults__, depth + 1, seen))
            if f.__kwdefaults__:
                parts.append("k=" + _desc(f.__kwdefaults__, depth + 1, seen))
            if f.__closure__:
                cells = []
                for c in f.__closure__:
                    try:
                        cells.append(_desc(c.cell_contents, depth + 1, seen))
                    except ValueError:
                        cells.append("<empty>")
                parts.append("c=" + ",".join(cells))
            if f.__name__ == "<lambda>":
                # a lambda's body is code: its constants decide what it returns
                parts.append("lc=" + repr([x for x in f.__code__.co_consts if isinstance(x, _PRIM)]))
        return "|".join(parts)
    if isinstance(v, (staticmethod, classmethod)):
        return f"{t.__name__}({_desc(v.__func__, depth + 1, seen)})"
    if isinstance(v, property):
        return "property(" + ",".join(_desc(x, depth + 1, seen) for x in (v.fget, v.fset, v.fdel)) + ")"
    if isinstance(v, (list, tuple)):
        if id(v) in seen:
            return "<cycle>"
        seen = seen | {id(v)}
        return f"{t.__name__}[" + ",".join(_desc(x, depth + 1, seen) for x in v) + "]"
    if isinstance(v, (set, frozenset)):
        if id(v) in seen:
            return "<cycle>"
        seen = seen | {id(v)}
        return f"{t.__name__}{{" + ",".join(sorted(_desc(x, depth + 1, seen) for x in v)) + "}"
    if isinstance(v, dict):
        if id(v) in seen:
            return "<cycle>"
        seen = seen | {id(v)}
        # sorted: some tables are built by iterating sets, so their order follows the hash seed
        return f"{t.__name__}{{" + ",".join(sorted(f"{_desc(k, depth + 1, seen)}:{_desc(x, depth + 1, seen)}" for k, x in v.items())) + "}"
    if isinstance(v, re.Pattern):
        return f"re:{v.pattern!r}:{v.flags}"
    if isinstance(v, types.ModuleType):
        return f"mod:{v.__name__}"
    d = getattr(v, "__dict__", None)
    slots = getattr(type(v), "__slots__", ())
    if id(v) in seen:
        return "<cycle>"
    seen = seen | {id(v)}
    state = {}
    if isinstance(d, dict):
        state.update(d)
    for s in (slots if isinstance(slots, (list, tuple)) else [slots]):
        if isinstance(s, str) and hasattr(v, s):
            state[s] = getattr(v, s)
    return f"obj:{t.__module__}.{t.__qualname__}{{" + ",".join(f"{k}:{_desc(x, depth + 1, seen)}" for k, x in sorted(state.items(), key=lambda kv: str(kv[0]))) + "}"


def _h(s: str) -> str:
    return hashlib.blake2b(s.encode("utf-8", "surrogatepass"), digest_size=10).hexdigest()


def import_all() -> list[str]:
    import sqlglot

    names = ["sqlglot"]
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
            names.append(info.name)
        except Exception:
            names.append(info.name + "!")
    return names


def snapshot() -> dict[str, str]:
    mods = import_all()
    out: dict[str, str] = {"__modules__": _h(",".join(sorted(mods)))}
    for name, mod in sorted(sys.modules.items()):
        if not (name == "sqlglot" or name.startswith("sqlglot.")) or mod is None:
            continue
        for g, v in list(vars(mod).items()):
            if g.startswith("__") and g.endswith("__"):
                continue
            if isinstance(v, types.ModuleType):
                continue
            if isinstance(v, type) and v.__module__ == name:
                for cls in _classes(v):
                    key = f"{cls.__module__}.{cls.__qualname__}"
                    for a, x in list(vars(cls).items()):
                        if a in ("__dict__", "__weakref__", "__doc__", "__module__", "__qualname__"):
                            continue
                        try:
                            out[f"{key}|{a}"] = _h(_desc(x))
                        except Exception as exc:  # noqa: BLE001
                            out[f"{key}|{a}"] = "ERR:" + type(exc).__name__
                    out[f"{key}|__mro__"] = _h(",".join(f"{k.__module__}.{k.__qualname__}" for k in cls.__mro__))
                continue
            try:
                out[f"{name}|{g}"] = _h(_desc(v))
            except Exception as exc:  # noqa: BLE001
                out[f"{name}|{g}"] = "ERR:" + type(exc).__name__
    return out


def _classes(c, acc=None):
    acc = acc if acc is not None else []
    acc.append(c)
    for v in vars(c).values():
        if isinstance(v, type) and v.__qualname__.startswith(c.__qualname__ + "."):
            _classes(v, acc)
    return acc


if __name__ == "__main__" and sys.argv[1:2] in (["--reference"], ["--reference-overlay"]):
    pass  # handled at the bottom, once build_reference is defined
elif __name__ == "__main__":
    snap = snapshot()
    with gzip.open(sys.argv[1], "wt", encoding="utf-8") as fh:
        json.dump(snap, fh)
    print("entries", len(snap))


def build_reference(out_path: str, seeds=("1", "2", "3")) -> None:
    """Reference snapshot of the pinned checkout, minus entries that vary with the hash seed."""
    import os
    import subprocess
    import tempfile

    snaps = []
    for seed in seeds:
        with tempfile.NamedTemporaryFile(suffix=".json.gz", delete=False) as tmp:
            path = tmp.name
        env = {**os.environ, "PYTHONHASHSEED": seed}
        subprocess.run([sys.executable, __file__, path], env=env, check=True, capture_output=True)
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            snaps.append(json.load(fh))
        os.unlink(path)
    ref = snaps[0]
    unstable = sorted({k for s in snaps[1:] for k in set(ref) | set(s) if ref.get(k) != s.get(k)})
    with gzip.open(out_path, "wt", encoding="utf-8") as fh:
        json.dump({"ref": ref, "unstable": unstable}, fh)
    print("reference entries", len(ref), "unstable", len(unstable))


def diff_snapshots(base: dict, snap: dict) -> dict:
    changed = sorted(k for k in base if k in snap and snap[k] != base[k])
    added = sorted(k for k in snap if k not in base)
    removed = sorted(k for k in base if k not in snap)
    return {"changed": changed, "added": added, "removed": removed}


def diff_against(ref_path: str) -> dict:
    """Changed / added / removed entries of the currently importable sqlglot vs the reference."""
    with gzip.open(ref_path, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    ref, unstable = data["ref"], set(data["unstable"])
    snap = snapshot()
    changed = sorted(k for k in ref if k in snap and snap[k] != ref[k] and k not in unstable)
    added = sorted(k for k in snap if k not in ref and k not in unstable)
    removed = sorted(k for k in ref if k not in snap and k not in unstable)
    return {"changed": changed, "added": added, "removed": removed}


if __name__ == "__main__" and sys.argv[1:2] == ["--reference"]:
    build_reference(sys.argv[2])


if __name__ == "__main__" and sys.argv[1:2] == ["--reference-overlay"]:
    # build the reference through the same symlink overlay the runtime child imports from,
    # so values derived from file paths match
    import os

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from verify import TREE, Verifier

    Verifier(sys.argv[3] if len(sys.argv) > 3 else "/sqlglot", []).prepare()
    os.environ["PYTHONPATH"] = TREE
    os.chdir(TREE)
    build_reference(sys.argv[2])
