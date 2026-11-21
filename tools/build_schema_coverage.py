#!/usr/bin/env python3
"""Measure baseline readers of expression schema values and required arguments.

Only the pinned checkout and numbered test manifest are inputs. Unlike ordinary
class dispatch tracing, a schema read does not imply use of every class field.
Class initializer edges retain copies and the required_args derivation, while
runtime required_args reads consume the concrete class's initializer context.
"""
from __future__ import annotations

import argparse
import ast
import builtins
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from m2_dispatch import BASE_COMMIT, declarations
from tools.build_dispatch import (
    Instrument as DispatchInstrument,
    Observer as DispatchObserver,
    read_json,
    refresh_dependencies,
    write_json,
)


METHOD = "expression-schema-value-reads-v1"
SCHEMAS = frozenset(("arg_types", "required_args"))
# These builtins consume only dictionary keys in the indicated argument. A name
# shadowed anywhere in the module is deliberately ineligible for this shortcut.
KEY_BUILTINS = frozenset(("len", "bool", "iter", "list", "tuple", "set", "frozenset",
                          "sorted", "reversed", "any", "all", "sum", "zip", "enumerate"))


class SchemaObserver(DispatchObserver):
    def record_value(self, key, allowed_initializer=False):
        if key is not None:
            if self.stack and not allowed_initializer:
                # An expression parsed into an import-time template can retain
                # a schema value in an arbitrary field. Such field consumers
                # are outside this focused trace and cannot be inferred safe.
                self.blocked.add(key)
            self.record(key)

    def owner(self, cls, name):
        lookup = (id(cls), name)
        if lookup not in self.attribute_cache:
            key = None
            for base in type.__getattribute__(cls, "__mro__"):
                if name in vars(base):
                    basekey = self.classkey(base)
                    key = basekey[:-1] + name if basekey is not None else None
                    break
            self.attribute_cache[lookup] = key
        return self.attribute_cache[lookup]

    def dictionary(self, cls):
        # Class namespace inspection can expose either the original map or its
        # derived set. Instance namespaces do not contain class schema fields.
        classkey = self.classkey(cls)
        if classkey is not None:
            self.record_value(classkey)
        for base in type.__getattribute__(cls, "__mro__"):
            basekey = self.classkey(base)
            if basekey is not None:
                for name in SCHEMAS & vars(base).keys():
                    self.record_value(basekey[:-1] + name)

    def getattr(self, obj, name, *default, _allowed_initializer=False):
        value = builtins.getattr(obj, name, *default)
        cls = obj if isinstance(obj, type) else type(obj)
        if name == "__dict__" and isinstance(obj, type):
            self.dictionary(cls)
        elif name in SCHEMAS:
            if name == "required_args":
                # __init_subclass__ derives this field for each concrete class.
                # Recording only its defining arg_types owner would miss copies
                # such as {**Parent.arg_types, 'extra': False}.
                classkey = self.classkey(cls)
                if classkey is not None:
                    self.record_value(classkey, _allowed_initializer)
            key = self.owner(cls, name)
            if key is not None:
                self.record_value(key, _allowed_initializer)
        return value

    def derived_getattr(self, obj, name):
        return self.getattr(obj, name, _allowed_initializer=True)

    def vars(self, obj):
        value = builtins.vars(obj)
        if isinstance(obj, type):
            self.dictionary(obj)
        return value

    def local(self, value, key):
        if key.rsplit("|", 1)[-1] in SCHEMAS:
            self.record_value(key)
        return value


class Instrument(DispatchInstrument):
    def __init__(self, path, tree):
        super().__init__(path, tree, SCHEMAS | {"__dict__"})
        self.parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        self.shadowed = {node.id for node in ast.walk(tree)
                         if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))}
        self.shadowed.update(node.arg for node in ast.walk(tree) if isinstance(node, ast.arg))
        self.shadowed.update(node.name for node in ast.walk(tree)
                             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    self.shadowed.add(alias.asname or alias.name.split(".")[0])
        self.star_import = any(isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
                               for node in ast.walk(tree))

    def builtin(self, name):
        return not self.star_import and name not in self.shadowed

    def keys_only(self, node):
        parent = self.parents.get(id(node))
        if isinstance(parent, (ast.For, ast.AsyncFor, ast.comprehension)) and parent.iter is node:
            return True
        if isinstance(parent, ast.Compare):
            # A chained comparison can also compare the map itself; admit only
            # one membership test with the map as its right operand.
            return (len(parent.ops) == 1 and isinstance(parent.ops[0], (ast.In, ast.NotIn))
                    and parent.comparators[0] is node)
        if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name):
            name = parent.func.id
            if name in KEY_BUILTINS and self.builtin(name) and node in parent.args:
                # zip consumes all arguments as iterables. Other supported
                # builtins consume only their first positional argument.
                return name == "zip" or parent.args[0] is node
        if isinstance(parent, ast.Attribute) and parent.attr == "keys":
            call = self.parents.get(id(parent))
            return isinstance(call, ast.Call) and call.func is parent and not call.args and not call.keywords
        return False

    def safe_derivation(self, node):
        """Admit only value-preserving schema copies and the pure required set."""
        parent = self.parents.get(id(node))
        # A class declaration alias or **-spread of a plain boolean schema has
        # no value-sensitive control flow or additional arbitrary consumer.
        copied = node
        if isinstance(parent, ast.Dict):
            if not all((key is None and isinstance(value, ast.Attribute) and value.attr == "arg_types")
                       or (isinstance(key, ast.Constant) and type(key.value) is str
                           and isinstance(value, ast.Constant) and type(value.value) is bool)
                       for key, value in zip(parent.keys, parent.values)):
                return False
            copied = parent
            parent = self.parents.get(id(parent))
        if isinstance(parent, (ast.Assign, ast.AnnAssign)) and self.targets.get(id(parent), "").endswith("|arg_types"):
            return parent.value is copied
        # cls.required_args = {key for key, value in cls.arg_types.items() if value}
        method = self.parents.get(id(node))
        call = self.parents.get(id(method))
        generator = self.parents.get(id(call))
        comprehension = self.parents.get(id(generator))
        assignment = self.parents.get(id(comprehension))
        if not (isinstance(method, ast.Attribute) and method.attr == "items"
                and isinstance(call, ast.Call) and call.func is method and not call.args and not call.keywords
                and isinstance(generator, ast.comprehension) and generator.iter is call and not generator.is_async
                and isinstance(generator.target, ast.Tuple) and len(generator.target.elts) == 2
                and all(isinstance(part, ast.Name) for part in generator.target.elts)
                and isinstance(comprehension, ast.SetComp) and comprehension.generators == [generator]
                and isinstance(assignment, ast.Assign) and len(assignment.targets) == 1):
            return False
        key_name, value_name = (part.id for part in generator.target.elts)
        target = assignment.targets[0]
        return (isinstance(comprehension.elt, ast.Name) and comprehension.elt.id == key_name
                and len(generator.ifs) == 1 and isinstance(generator.ifs[0], ast.Name)
                and generator.ifs[0].id == value_name and isinstance(target, ast.Attribute)
                and target.attr == "required_args" and ast.dump(target.value) == ast.dump(node.value))

    def visit_Attribute(self, node):
        keys_only = node.attr == "arg_types" and self.keys_only(node)
        allowed_initializer = node.attr == "arg_types" and self.safe_derivation(node)
        node.value = self.visit(node.value)
        if isinstance(node.ctx, ast.Load) and node.attr in SCHEMAS | {"__dict__"} and not keys_only:
            return self.call("derived_getattr" if allowed_initializer else "getattr",
                             [node.value, ast.Constant(node.attr)], node)
        return node

    def visit_Call(self, node):
        original = node.func
        node = self.generic_visit(node)
        # The ordinary dispatch loader wraps every call to detect constructors.
        # A bool-only schema edit cannot alter construction itself, so retain
        # only metadata access and leave the expensive parse tree calls alone.
        if (isinstance(original, ast.Name) and original.id in ("getattr", "vars")
                and self.builtin(original.id) and (original.id != "vars" or node.args)):
            node.func = ast.Attribute(value=ast.Name(id="_br_dispatch", ctx=ast.Load()),
                                      attr=original.id, ctx=ast.Load())
        return node


class Loader(importlib.machinery.SourceFileLoader):
    def source_to_code(self, data, path, *, _optimize=-1):
        relative = Path(path).resolve().relative_to(self.observer.repo).as_posix()
        tree = ast.parse(importlib.util.decode_source(data), path)
        tree = Instrument(relative, tree).visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)

    def get_code(self, fullname):
        return self.source_to_code(self.get_data(self.path), self.path)

    def exec_module(self, module):
        module.__dict__["_br_dispatch"] = self.observer
        super().exec_module(module)


class Finder(importlib.abc.MetaPathFinder):
    def __init__(self, observer):
        self.observer = observer

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self.observer.modules:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            loader = Loader(fullname, spec.origin)
            loader.observer = self.observer
            spec.loader = loader
        return spec


def install(config):
    observer = SchemaObserver(config)
    sys.meta_path.insert(0, Finder(observer))
    return observer


def child_install():
    if os.environ.get("BR_DISPATCH_CONFIG"):
        install(read_json(os.environ["BR_DISPATCH_CONFIG"]))


def collect(args):
    args.repo = args.repo.resolve()
    args.observations = args.observations.resolve()
    args.output = args.output.resolve()
    args.coverage = args.coverage.resolve()
    if args.observations.exists() and any(args.observations.iterdir()):
        raise ValueError("schema observation directory must be fresh")
    args.observations.mkdir(parents=True, exist_ok=True)
    metadata = read_json(args.checkout_metadata)
    if metadata["base_commit"] != BASE_COMMIT:
        raise ValueError("wrong baseline")
    modules, paths, attributes = {}, {}, {}
    for name, expected_hash in metadata["files"].items():
        source = (args.repo / name).read_bytes()
        if hashlib.sha256(source).hexdigest() != expected_hash:
            raise ValueError(f"baseline changed: {name}")
        if name.endswith(".py") and name.startswith(("sqlglot/", "tests/")):
            module = name[:-3].replace("/", ".")
            modules[module[:-9] if module.endswith(".__init__") else module] = name
            text = source.decode().replace("\r\n", "\n")
            paths[name] = {"sha256": hashlib.sha256(text.encode()).hexdigest()}
            attributes.update({key: {"tests": [], "blocked": True}
                               for key in declarations(ast.parse(text), name) if key.endswith("|arg_types")})
    tests_value = read_json(args.manifest)
    tests = tests_value["tests"] if isinstance(tests_value, dict) else tests_value
    tests = [{"number": row["number"], "id": row["id"]} for row in tests]
    by_id = {row["id"]: row["number"] for row in tests}
    config = {"repo": str(args.repo), "observations": str(args.observations),
              "modules": modules, "process_hooks": True, "method": METHOD}
    config_path = args.observations / "config.json"
    write_json(config_path, config)
    os.environ.update(BR_DISPATCH_CONFIG=str(config_path), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                      PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0")
    os.chdir(args.repo)
    sys.path.insert(0, str(args.repo))
    observer = install(config)
    import pytest
    outcomes, collected, collection_errors = {}, [], []

    class Plugin:
        def pytest_collection_modifyitems(self, items):
            collected.extend(item.nodeid for item in items)

        def pytest_collectreport(self, report):
            if report.failed:
                collection_errors.append(report.nodeid)

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item, nextitem):
            observer.active = by_id.get(item.nodeid)
            observer.attribute_cache.clear()
            if observer.active is not None:
                os.environ["BR_DISPATCH_TEST"] = str(observer.active)
            try:
                return (yield)
            finally:
                observer.active = None
                os.environ.pop("BR_DISPATCH_TEST", None)

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_fixture_setup(self, fixturedef, request):
            if fixturedef.scope == "function":
                return (yield)
            previous = observer.active
            observer.active = None
            os.environ.pop("BR_DISPATCH_TEST", None)
            try:
                return (yield)
            finally:
                observer.active = previous
                if previous is not None:
                    os.environ["BR_DISPATCH_TEST"] = str(previous)

        def pytest_runtest_logreport(self, report):
            row = outcomes.setdefault(report.nodeid, {})
            if row.get(report.when) != "failed":
                row[report.when] = report.outcome

    with tempfile.TemporaryDirectory(prefix="schema-child-") as temporary:
        (Path(temporary) / "sitecustomize.py").write_text(
            "from tools.build_schema_coverage import child_install\nchild_install()\n", encoding="utf-8")
        os.environ["PYTHONPATH"] = os.pathsep.join([
            temporary, str(Path(__file__).resolve().parents[1]), str(args.repo), os.environ.get("PYTHONPATH", "")])
        exit_code = int(pytest.main(["-q", "--assert=plain", "-p", "no:cacheprovider",
                                    *args.pytest_arg, "tests"], plugins=[Plugin()]))
    for module_name, attribute in (("multiprocessing.forkserver", "_forkserver"),
                                   ("multiprocessing.resource_tracker", "_resource_tracker")):
        module = sys.modules.get(module_name)
        service = getattr(module, attribute, None) if module is not None else None
        if service is not None:
            service._stop()
    observer.finish()
    starts = sorted(args.observations.glob("*.start.json"))
    unfinished, process_errors, children, started_pids = [], [], {}, set()
    events = 0
    for start in starts:
        started_pids.add(str(read_json(start)["pid"]))
        done = start.with_name(start.name.replace(".start.json", ".done.json"))
        if not done.exists():
            unfinished.append(start.name)
            continue
        data = read_json(done)
        children.update(data.get("children", {}))
        process_errors.extend(data.get("process_errors", []))
        events += len(data["reads"]) + len(data["edges"]) + len(data["blocked"])
    unobserved = sorted(set(children) - started_pids)
    passed, failed, skipped, missing = [], [], [], []
    for row in tests:
        outcome = outcomes.get(row["id"], {})
        if "failed" in outcome.values():
            failed.append(row["number"])
        elif "skipped" in outcome.values():
            skipped.append(row["number"])
        elif outcome.get("call") == "passed" and outcome.get("teardown") == "passed":
            passed.append(row["number"])
        else:
            missing.append(row["number"])
    extra = [nodeid for nodeid in collected if nodeid not in by_id and nodeid in outcomes
             and "skipped" not in outcomes.get(nodeid, {}).values()]
    complete = bool(events) and not (exit_code or failed or missing or extra or unfinished or collection_errors
                                    or process_errors or unobserved)
    context = {"complete": complete, "pytest_exit_code": exit_code, "failed": failed,
               "passed": passed, "skipped": skipped, "missing": missing, "extra": extra,
               "collection_errors": collection_errors, "unfinished": unfinished,
               "process_errors": process_errors, "unobserved_children": unobserved,
               "processes": len(starts), "schema_observation_keys": events, "method": METHOD,
               "startup_policy": "only-pure-required-set-and-schema-copies"}
    artifact = {"version": 1, "base_commit": BASE_COMMIT, "tests": tests,
                "paths": paths, "attributes": attributes, "context": context}
    artifact = refresh_dependencies(artifact, read_json(args.coverage), [args.observations])
    context["attribute_scopes"] = ["schema-value-reads-and-required-args"]
    context["generator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    write_json(args.output, artifact)
    print(__import__("json").dumps({key: len(value) if key in ("passed", "missing") else value
                                   for key, value in context.items()}, sort_keys=True))
    return 0 if complete else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--checkout-metadata", required=True, type=Path)
    parser.add_argument("--coverage", required=True, type=Path)
    parser.add_argument("--observations", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pytest-arg", action="append", default=[])
    return collect(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
