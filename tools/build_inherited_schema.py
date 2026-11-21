"""Collect receiver and namespace dependencies for inherited argument schemas."""
from __future__ import annotations

import ast
import builtins
from functools import partial
import hashlib
import inspect
import json
import operator
import types

from tools.build_dispatch import read_json, refresh_dependencies


METHOD = "inherited-argument-schemas-v1"
GLOBAL = "@inherited-schema-eval"
CORE = "sqlglot/expressions/core.py"
FIELDS = frozenset({"arg_types", "required_args", "__dict__"})
NAMESPACE_FUNCTIONS = (
    inspect.getmembers, inspect.getmembers_static, inspect.classify_class_attrs,
)


def pass_classes(path, tree):
    if not path.startswith("sqlglot/expressions/"):
        return []
    return [f"{path}|{node.name}|*" for node in tree.body
            if isinstance(node, ast.ClassDef) and len(node.body) == 1
            and isinstance(node.body[0], ast.Pass) and not node.keywords
            and not node.decorator_list]


def class_of(obj):
    return obj if issubclass(type(obj), type) else type(obj)


class InheritedObservation:
    """Used only in the opt-in collector, with unchanged baseline return values."""

    def __init__(self, config):
        self.inherited_candidates = set(config["inherited_candidates"])
        self.reset_inheritance()
        super().__init__(config)

    def reset_inheritance(self):
        self.inherited_schemas = {}
        self.inherited_class_objects = {}
        self.inherited_blockers = {}

    def after_fork(self):
        super().after_fork()
        self.reset_inheritance()

    def finish(self):
        if self.done:
            return
        # Retain exact class objects until exit to check that no observed
        # class schema or hierarchy escaped the startup evidence.
        for key, cls in self.inherited_class_objects.items():
            value = type.__getattribute__(cls, "arg_types")
            if (self.inherited_schemas.get(key) != schema_items(value)
                    or type.__getattribute__(cls, "__subclasses__")()):
                self.block_inherited(key, "schema or subclass changed")
        super().finish()
        output = self.output / f"{self.identity}.done.json"
        data = read_json(output)
        data.update(inherited_schemas=self.inherited_schemas,
                    inherited_blockers={key: sorted(reasons)
                                        for key, reasons in self.inherited_blockers.items()})
        # The existing writer sorts objects. Schemas remain ordered lists here.
        from tools.build_dispatch import write_json
        write_json(output, data)

    def block_inherited(self, key, reason):
        self.inherited_blockers.setdefault(key, set()).add(reason)

    def note(self, obj, *, opaque):
        key = self.classkey(class_of(obj))
        if key not in self.inherited_candidates:
            return
        self.record(key)
        if opaque:
            self.block_inherited(key, "schema or namespace may escape")
        elif self.stack or self.active is None:
            self.block_inherited(key, "validator before or outside numbered test")

    def note_attribute(self, obj, name):
        if type(name) is str and name in FIELDS:
            self.note(obj, opaque=True)

    def getattr(self, obj, name, *default, _allowed_initializer=False):
        self.note_attribute(obj, name)
        return super().getattr(obj, name, *default, _allowed_initializer=_allowed_initializer)

    def inherited_required_set_source(self, cls, name):
        # The AST instrumenter uses this method only for reading cls.arg_types
        # in the pinned Expr.__init_subclass__ required-set comprehension.
        value = super().getattr(cls, name, _allowed_initializer=True)
        hierarchy = type.__getattribute__(cls, "__mro__")
        for base in hierarchy[1:]:
            key = self.classkey(base)
            if key in self.inherited_candidates:
                self.block_inherited(key, "observed subclass")
        key = self.classkey(cls)
        if key in self.inherited_candidates:
            own = type.__getattribute__(cls, "__dict__")
            items = schema_items(value)
            if (type(cls) is not type or "arg_types" in own or items is None
                    or type.__getattribute__(cls, "__getattribute__") is not object.__getattribute__
                    or type.__getattribute__(cls, "__setattr__") is not object.__setattr__):
                self.block_inherited(key, "nonstandard class or schema")
            elif key in self.inherited_schemas and self.inherited_schemas[key] != items:
                self.block_inherited(key, "different inherited schema across class creations")
            else:
                self.inherited_schemas[key] = items
                self.inherited_class_objects[key] = cls
        return value

    def validation_getattr(self, obj, name, *, _runtime_certified=False):
        self.note(obj, opaque=not _runtime_certified)
        return super().validation_getattr(obj, name, _runtime_certified=_runtime_certified)

    def access(self, getter, obj, attr, *default):
        self.note_attribute(obj, attr)
        return getter(obj, attr, *default)

    def bound_access(self, getter, attr, *default):
        self.note_attribute(getter.__self__, attr)
        return getter(attr, *default)

    def namespace(self, getter, obj, *extra, **options):
        self.note(obj, opaque=True)
        return getter(obj, *extra, **options)

    def attrgetter(self, getter, obj):
        for name in getter.__reduce__()[1]:
            first, _, tail = name.partition(".")
            self.note_attribute(obj, first)
            if any(part in FIELDS for part in tail.split(".")):
                self.block_inherited(GLOBAL, "unresolved nested metadata getter")
        return getter(obj)

    def inherited_callable(self, fn):
        # Test actual callable identity to cover imports/aliases without
        # re-evaluating the receiver or changing arguments to the real call.
        if fn is builtins.getattr:
            return self.getattr
        if fn is builtins.vars or fn is builtins.dir or any(fn is api for api in NAMESPACE_FUNCTIONS):
            return partial(self.namespace, fn)
        if (fn is builtins.hasattr or fn is inspect.getattr_static
                or fn is type.__getattribute__ or fn is object.__getattribute__):
            return partial(self.access, fn)
        if type(fn) is types.MethodWrapperType and fn.__name__ == "__getattribute__":
            return partial(self.bound_access, fn)
        if type(fn) is operator.attrgetter:
            return partial(self.attrgetter, fn)
        if fn is builtins.eval or fn is builtins.exec:
            # Preserve the caller frame. Computed expressions are a shared
            # conservative dependency since their AST is not instrumented.
            self.record(GLOBAL)
        return fn


def schema_items(value):
    if type(value) is not dict or not value:
        return None
    if any(type(key) is not str or type(required) is not bool for key, required in value.items()):
        return None
    return [[key, required] for key, required in value.items()]


def inherited_map(artifact, coverage, coverage_bytes, observations, candidates):
    raw = [read_json(path) for path in sorted(observations.glob("*.done.json"))]
    schema_facts = {}
    blockers = {}
    for row in raw:
        for key, schema in row["inherited_schemas"].items():
            if key in schema_facts and schema_facts[key] != schema:
                blockers.setdefault(key, set()).add("processes disagree on inherited schema")
            schema_facts[key] = schema
        for key, reasons in row["inherited_blockers"].items():
            blockers.setdefault(key, set()).update(reasons)
    graph = dict(artifact, attributes={key: {} for key in [*candidates, GLOBAL]},
                 context=dict(artifact["context"]))
    refresh_dependencies(graph, coverage, [observations])
    paths = {}
    counts = {}
    global_dep = graph["attributes"][GLOBAL]
    global_blocked = GLOBAL in blockers or global_dep["blocked"]
    for key in candidates:
        dependency = graph["attributes"][key]
        reasons = blockers.get(key, set()).copy()
        if global_blocked:
            reasons.add("unattributed dynamic metadata access")
        if dependency["blocked"]:
            reasons.add("unattributed receiver access")
        if key not in schema_facts:
            reasons.add("no observed inherited schema")
        if not dependency["tests"]:
            reasons.add("no positive receiver reads")
        if reasons:
            for reason in reasons:
                counts[reason] = counts.get(reason, 0) + 1
            continue
        path, cls, _ = key.split("|")
        record = paths.setdefault(path, {"sha256": coverage["paths"][path]["sha256"], "classes": {}})
        record["classes"][cls] = {
            "schema": dict(schema_facts[key]),
            "tests": sorted(set(dependency["tests"]) | set(global_dep["tests"])),
        }
    return {"base_commit": artifact["base_commit"], "tests": artifact["tests"], "paths": paths,
            "context": {
                "method": METHOD, "complete": artifact["context"]["complete"],
                "coverage_sha256": hashlib.sha256(coverage_bytes).hexdigest(),
                "candidate_classes": len(candidates),
                "eligible_classes": sum(len(row["classes"]) for row in paths.values()),
                "dynamic_code_tests": len(global_dep["tests"]),
                "blocked_reasons": counts,
            }}


def write_map(path, value):
    # A dict's insertion order is part of the schema evidence.
    path.write_text(json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
