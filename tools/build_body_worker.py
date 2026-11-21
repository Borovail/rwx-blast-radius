"""Opt-in, base-only binary comparison instrumentation for isolated tests."""

from __future__ import annotations

import ast
import atexit
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import weakref

from m2_body import COMPARISONS, comparison_site


_BINDING = "__blast_body_operand_7a932e5__"
_SAFE_EQ = (object.__eq__, int.__eq__, float.__eq__, str.__eq__, bytes.__eq__,
            type(None).__eq__)
_SAFE_NE = (object.__ne__, int.__ne__, float.__ne__, str.__ne__, bytes.__ne__,
            type(None).__ne__)
_SCALARS = (type(None), bool, int, float, str, bytes)
_TYPE_INFO = {}
_CONVERTERS = {int.__eq__: int.__int__, float.__eq__: float.__float__,
               str.__eq__: str.__str__, bytes.__eq__: bytes.__bytes__}


def _method(value, name):
    # Bypass both instance and metaclass attribute hooks.
    for base in type.__getattribute__(type(value), "__mro__"):
        namespace = type.__getattribute__(base, "__dict__")
        if name in namespace:
            return namespace[name]
    return None


def _snapshot(value):
    identity = id(value)
    klass = type(value)
    key = id(klass)
    immutable = any(klass is primitive for primitive in _SCALARS)
    if immutable:
        if key not in _TYPE_INFO:
            _TYPE_INFO[key] = klass, _method(value, "__eq__"), _method(value, "__ne__")
        _, eq, ne = _TYPE_INFO[key]
    else:
        eq, ne = _method(value, "__eq__"), _method(value, "__ne__")
    if not any(eq is method for method in _SAFE_EQ) or not any(ne is method for method in _SAFE_NE):
        return identity, ("unknown",)
    if eq is object.__eq__:
        return identity, ("identity",)
    # Call builtin implementations directly; an enum may customize __str__.
    scalar = None if value is None else _CONVERTERS[eq](value)
    name = type(scalar).__name__
    if scalar is None:
        name, scalar = "none", None
    elif type(scalar) is int:
        scalar = str(scalar)
        if len(scalar) > 128:
            return identity, ("unknown",)
    elif type(scalar) is float:
        scalar = scalar.hex()
    elif type(scalar) is bytes:
        scalar = scalar.hex()
        if len(scalar) > 256:
            return identity, ("unknown",)
    elif type(scalar) is str and len(scalar) > 256:
        return identity, ("unknown",)
    kind = "scalar" if immutable else "scalar_subclass"
    return identity, (kind, name, scalar)


class Instrumenter(ast.NodeTransformer):
    def __init__(self, path, sites, operators=COMPARISONS):
        self.path, self.sites, self.operators = path, sites, operators

    def visit_Compare(self, node):
        original_dump = ast.dump(node)
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]).__name__ not in self.operators:
            return node
        site = comparison_site(node)
        self.sites[site] = {"ast": original_dump, "operator": type(node.ops[0]).__name__}
        for side, child in enumerate((node.left, node.comparators[0])):
            call = ast.Call(func=ast.Name(id=_BINDING, ctx=ast.Load()),
                            args=[ast.Constant(self.path), ast.Constant(site),
                                  ast.Constant(side), child], keywords=[])
            ast.copy_location(call, child)
            if side == 0:
                node.left = call
            else:
                node.comparators[0] = call
        result = ast.Call(func=ast.Name(id=_BINDING, ctx=ast.Load()),
                          args=[ast.Constant(self.path), ast.Constant(site),
                                ast.Constant(2), node], keywords=[])
        return ast.copy_location(result, node)


def install(*, snapshotter=None):
    if not os.environ.get("BLAST_BODY_DIR"):
        return
    if snapshotter is None:
        snapshotter = _snapshot
    directory = Path(os.environ["BLAST_BODY_DIR"])
    root = Path(os.environ["BLAST_TRACE_REPO"]).resolve()
    token = os.environ["BLAST_TRACE_TOKEN"]
    operators = tuple(os.environ.get("BLAST_BODY_OPERATORS", "Is,IsNot").split(","))
    state = {"pid": os.getpid(), "finished": False, "files": {}, "pending": {},
             "values": {}, "errors": [], "unknown": set(), "codes": {}, "modules": {},
             "disabled": set(), "compiling": False}

    def register(code):
        state["codes"][id(code)] = code
        for value in code.co_consts:
            if isinstance(value, types.CodeType):
                register(value)

    def relative(filename):
        try:
            return Path(filename).resolve().relative_to(root).as_posix()
        except (TypeError, ValueError, OSError):
            return None

    def mark_unknown(path, site, key):
        state["pending"].pop(key, None)
        state["values"].setdefault(path, {})[site] = {None}
        state["disabled"].add((path, site))

    def operand(path, site, side, value):
        if (path, site) in state["disabled"]:
            return value
        frame = sys._getframe(1)
        key = id(frame), path, site

        if side == 0:
            try:
                snapshot = snapshotter(value)
                if snapshot[1][0] == "unknown":
                    mark_unknown(path, site, key)
                else:
                    # Recheck mutable classes after RHS evaluation: that code
                    # could replace the left object's class/comparison methods.
                    immutable = any(type(value) is klass for klass in (*_SCALARS, object))
                    reference = None if immutable else weakref.ref(value)
                    state["pending"][key] = snapshot, reference
            except Exception:
                mark_unknown(path, site, key)
        elif side == 1:
            pending = state["pending"].pop(key, _MISSING)
            if pending is not _MISSING:
                try:
                    left, reference = pending
                    if reference is not None:
                        actual_left = reference()
                        if actual_left is None:
                            raise ValueError("left operand disappeared before comparison")
                        left = snapshotter(actual_left)
                    right = snapshotter(value)
                    if left[1][0] == "unknown" or right[1][0] == "unknown":
                        mark_unknown(path, site, key)
                    else:
                        state["pending"][key] = (left[1], right[1], left[0] == right[0])
                except Exception:
                    mark_unknown(path, site, key)
            else:
                mark_unknown(path, site, key)
        else:
            pair = state["pending"].pop(key, None)
            item = pair + (value,) if pair is not None and type(value) is bool else None
            observed = state["values"].setdefault(path, {})
            observations = observed.setdefault(site, set())
            # Bounded factual snapshots prevent loops from producing huge maps.
            if item is None or len(observations) >= 64 and item not in observations:
                mark_unknown(path, site, key)
            else:
                observations.add(item)
        return value

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            filename = self.get_filename(fullname)
            path = relative(filename)
            raw = self.get_data(filename)
            source = importlib.util.decode_source(raw)
            previous = state["compiling"]
            state["compiling"] = True
            try:
                tree = ast.parse(source, filename=filename)
                if any(isinstance(node, ast.Name) and node.id == _BINDING for node in ast.walk(tree)):
                    state["unknown"].add(path)
                    return super().get_code(fullname)
                sites = {}
                tree = Instrumenter(path, sites, operators).visit(tree)
                ast.fix_missing_locations(tree)
                code = compile(tree, filename, "exec", dont_inherit=True)
            finally:
                state["compiling"] = previous
            register(code)
            normalized = source.replace("\r\n", "\n")
            state["files"][path] = {"sha256": hashlib.sha256(normalized.encode()).hexdigest(),
                                      "sites": sites}
            return code

        def exec_module(self, module):
            module.__dict__[_BINDING] = operand
            state["modules"][self.path] = module
            return super().exec_module(module)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
            if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                if relative(spec.origin) is not None and spec.origin.endswith(".py"):
                    spec.loader = Loader(fullname, spec.origin)
                    return spec
            return None

    finder = Finder()
    sys.meta_path.insert(0, finder)

    def audit(event, args):
        if event == "compile" and not state["compiling"]:
            path = relative(args[1])
            if path is not None:
                state["unknown"].add(path)
        elif event == "exec":
            code = args[0]
            path = relative(code.co_filename)
            if path is not None and id(code) not in state["codes"]:
                state["unknown"].add(path)

    sys.addaudithook(audit)

    def finish():
        if state["finished"]:
            return
        state["finished"] = True
        if finder not in sys.meta_path:
            state["errors"].append("instrumenting import hook removed")
        for filename, module in state["modules"].items():
            if module.__dict__.get(_BINDING) is not operand:
                state["unknown"].add(relative(filename))
        files = {}
        for path, observations in state["values"].items():
            if path not in state["unknown"]:
                record = state["files"][path]
                files[path] = {"sha256": record["sha256"], "sites": {
                    site: {**record["sites"][site], "observations": sorted(
                        values, key=lambda item: json.dumps(item, ensure_ascii=True))}
                    for site, values in observations.items()}}
        result = {"version": 1, "pid": state["pid"], "token": token,
                  "complete": not state["errors"], "errors": state["errors"],
                  "unknown": sorted(path for path in state["unknown"] if path), "files": files}
        target = directory / (str(state["pid"]) + ".body.json")
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")))
        temporary.replace(target)

    def after_fork():
        state.update(pid=os.getpid(), finished=False, pending={}, values={}, errors=[], unknown=set(), disabled=set())

    atexit.register(finish)
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=after_fork)
    original_exit = os._exit

    def immediate_exit(code):
        finish()
        original_exit(code)

    os._exit = immediate_exit


_MISSING = object()


if __name__ == "__main__":
    install()
