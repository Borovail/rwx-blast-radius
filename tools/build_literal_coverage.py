#!/usr/bin/env python3
"""Observe literal public scalar values of audited concrete SQLGlot dialects."""
import ast
import builtins
import hashlib
import os
from pathlib import Path
import sys
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import build_dispatch as protocol
from tools.build_dispatch import Observer as BaseObserver, Instrument as BaseInstrument, read_json
from tools.literal_eligibility import scalar_declarations, audit, POLICY as ELIGIBILITY_POLICY

METHOD = "literal-config-value-readers-v1"
NAMESPACE_POLICY = "literal-class-namespace-reads-v1"

class ScalarObserver(BaseObserver):
    def scalar_key(self, cls, name, *, direct=False):
        if type(name) is not str or name.startswith("_") or not name.isupper():
            return None
        bases = (cls,) if direct else type.__getattribute__(cls, "__mro__")
        for base in bases:
            fields = type.__getattribute__(base, "__dict__")
            if name in fields:
                key = self.classkey(base)
                if key is None or not any(type(fields[name]) is kind for kind in (bool, int, str)):
                    return None
                return key[:-1] + name
        return None

    def record_scalar(self, key):
        if key is not None:
            if self.stack:
                self.blocked.add(key)
            self.record(key)

    def dictionary(self, obj):
        if not isinstance(obj, type):
            return
        for base in type.__getattribute__(obj, "__mro__"):
            for name in type.__getattribute__(base, "__dict__"):
                self.record_scalar(self.scalar_key(base, name, direct=True))

    def getattr(self, obj, name, *default):
        value = builtins.getattr(obj, name, *default)
        if name == "__dict__":
            self.dictionary(obj)
        elif any(type(value) is kind for kind in (bool, int, str)):
            cls = obj if isinstance(obj, type) else type(obj)
            self.record_scalar(self.scalar_key(cls, name))
        return value

    def vars(self, obj):
        value = builtins.vars(obj)
        self.dictionary(obj)
        return value

    def use(self, cls):
        # Construction alone does not consume a literal scalar setting.
        pass

    def local(self, value, key):
        if any(type(value) is kind for kind in (bool, int, str)):
            self.record_scalar(key)
        return value

    @staticmethod
    def ordinary_namespace(obj, value):
        if not isinstance(obj, type) or type(value) is not types.MappingProxyType:
            return False
        meta = type(obj)
        if type.__getattribute__(meta, "__getattribute__") is not type.__getattribute__:
            return False
        for base in type.__getattribute__(meta, "__mro__"):
            fields = type.__getattribute__(base, "__dict__")
            if "__dict__" in fields:
                return fields["__dict__"] is type.__dict__["__dict__"]
        return False

    def namespace_method(self, obj, method, literal, use_vars=False):
        value = builtins.vars(obj) if use_vars else builtins.getattr(obj, "__dict__")
        original = builtins.getattr(value, method)
        if not self.ordinary_namespace(obj, value):
            self.dictionary(obj)
            return original

        def consume(*args, **kwargs):
            self.record_scalar(self.scalar_key(obj, literal, direct=True))
            return original(*args, **kwargs)
        return consume

    def namespace_item(self, obj, literal, use_vars=False):
        value = builtins.vars(obj) if use_vars else builtins.getattr(obj, "__dict__")
        if self.ordinary_namespace(obj, value):
            self.record_scalar(self.scalar_key(obj, literal, direct=True))
        else:
            self.dictionary(obj)
        return value[literal]

class ScalarInstrument(BaseInstrument):
    def __init__(self, path, tree, attribute_names=None):
        super().__init__(path, tree, attribute_names, declaration_reader=scalar_declarations)
        self.shadowed = {node.id for node in ast.walk(tree)
                         if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))}
        self.shadowed.update(node.arg for node in ast.walk(tree) if isinstance(node, ast.arg))
        self.shadowed.update(node.name for node in ast.walk(tree)
                             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    self.shadowed.add(alias.asname or alias.name.split(".")[0])
        self.star = any(isinstance(node, ast.ImportFrom)
                        and any(alias.name == "*" for alias in node.names) for node in ast.walk(tree))

    def builtin(self, name):
        return not self.star and name not in self.shadowed

    def namespace(self, node):
        if isinstance(node, ast.Attribute) and node.attr == "__dict__" and isinstance(node.ctx, ast.Load):
            return node.value, False
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "vars"
                and self.builtin("vars") and len(node.args) == 1 and not node.keywords):
            return node.args[0], True
        return None

    def visit_Attribute(self, node):
        node.value = self.visit(node.value)
        if isinstance(node.ctx, ast.Load) and (node.attr.isupper() or node.attr == "__dict__"):
            return self.call("getattr", [node.value, ast.Constant(node.attr)], node)
        return node

    def visit_Subscript(self, node):
        direct = self.namespace(node.value)
        if direct and isinstance(node.ctx, ast.Load) and isinstance(node.slice, ast.Constant) and type(node.slice.value) is str:
            obj, use_vars = direct
            return self.call("namespace_item", [self.visit(obj), node.slice, ast.Constant(use_vars)], node)
        return self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        direct = self.namespace(func.value) if isinstance(func, ast.Attribute) and func.attr == "get" else None
        if (direct and 1 <= len(node.args) <= 2 and not node.keywords
                and isinstance(node.args[0], ast.Constant) and type(node.args[0].value) is str):
            obj, use_vars = direct
            node.func = self.call("namespace_method", [self.visit(obj), ast.Constant("get"),
                                                       node.args[0], ast.Constant(use_vars)], func)
            node.args = [self.visit(arg) for arg in node.args]
            return node
        original = node.func
        node = self.generic_visit(node)
        if (isinstance(original, ast.Name) and original.id in ("getattr", "vars")
                and self.builtin(original.id) and (original.id != "vars" or node.args)):
            node.func = ast.Attribute(value=ast.Name(id="_br_dispatch", ctx=ast.Load()),
                                      attr=original.id, ctx=ast.Load())
        return node

def child_install():
    if os.environ.get("BR_DISPATCH_CONFIG"):
        protocol.install(read_json(os.environ["BR_DISPATCH_CONFIG"]),
                         observer_type=ScalarObserver, instrument_type=ScalarInstrument)


def finalize(artifact, args):
    if args.repo is None or args.checkout_metadata is None:
        raise ValueError("literal collector requires fresh source-audited collection")
    report = audit(args.repo, read_json(args.checkout_metadata))
    for key, dependency in artifact["attributes"].items():
        dependency["eligible"] = key in report["eligible_declarations"]
    here = Path(__file__).resolve()
    artifact["context"].update(
        method=METHOD, namespace_policy=NAMESPACE_POLICY, eligibility_policy=ELIGIBILITY_POLICY,
        attribute_scopes=["public-uppercase-literal-scalar-values"],
        eligible_declarations=len(report["eligible_declarations"]),
        metaclass_path_sha256=report["metaclass_path_sha256"],
        generator_sha256=hashlib.sha256(here.read_bytes()).hexdigest(),
        protocol_sha256=hashlib.sha256(Path(protocol.__file__).read_bytes()).hexdigest(),
        eligibility_generator_sha256=hashlib.sha256(here.with_name("literal_eligibility.py").read_bytes()).hexdigest())
    return artifact


def main():
    return protocol.main(observer_type=ScalarObserver, instrument_type=ScalarInstrument,
                         declaration_reader=scalar_declarations, method=METHOD,
                         child_module="tools.build_literal_coverage", finalize=finalize, require_fresh=True)


if __name__ == "__main__":
    raise SystemExit(main())
