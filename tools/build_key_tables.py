#!/usr/bin/env python3
"""Observe pinned dictionary key reads with complete child-process accounting."""

from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import types

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from m2_dispatch import BASE_COMMIT
from observer_targets import positions_digest
from tools.build_dispatch import Finder, Observer, read_json, refresh_dependencies, write_json


POLICY = "pinned-table-key-reads-v1"
ATTRIBUTE = "SET_OP_DISTINCT_BY_DEFAULT"
WIDTH = "_SEQ_BYTE_WIDTH"
PROVIDERS = {
    "dialect.Dialect": ("sqlglot/dialects/dialect.py", "sqlglot.dialects.dialect", "Dialect",
                        {"Except": True, "Intersect": True, "Union": True}),
    "clickhouse.ClickHouse": ("sqlglot/dialects/clickhouse.py", "sqlglot.dialects.clickhouse", "ClickHouse",
                              {"Except": False, "Intersect": False, "Union": None}),
    "duckdb._SEQ_BYTE_WIDTH": ("sqlglot/generators/duckdb.py", "sqlglot.generators.duckdb", None,
                              {"Seq1": 1, "Seq2": 2, "Seq4": 4, "Seq8": 8}),
}
OTHER_PROVIDER = ("sqlglot.dialects.bigquery", "BigQuery")
SITES = {
    ("sqlglot/parser.py", 6183): "self.dialect.SET_OP_DISTINCT_BY_DEFAULT[operation]",
    ("sqlglot/generator.py", 1964): "self.dialect.SET_OP_DISTINCT_BY_DEFAULT[op_type]",
    ("sqlglot/generators/duckdb.py", 727): "_SEQ_BYTE_WIDTH[type(node)]",
}
CLASS_PROVIDERS = frozenset(("dialect.Dialect", "clickhouse.ClickHouse"))


def normalized_hash(path):
    source = path.read_text("utf-8").replace("\r\n", "\n")
    return hashlib.sha256(source.encode()).hexdigest()


class KeyObserver(Observer):
    def __init__(self, config):
        self.key_reads = defaultdict(set)
        self.key_blocked = set()
        self.key_errors = set()
        self.line_reads = defaultdict(set)
        self.identities = {}
        super().__init__(config)

    def after_fork(self):
        super().after_fork()
        self.key_reads = defaultdict(set)
        self.key_blocked = set()
        self.key_errors = set()
        self.line_reads = defaultdict(set)

    def finish(self):
        if self.done:
            return
        super().finish()
        path = self.output / f"{self.identity}.done.json"
        row = read_json(path)
        row["key_reads"] = {key: sorted(values) for key, values in self.key_reads.items()}
        row["key_blocked"] = sorted(self.key_blocked)
        row["key_errors"] = sorted(self.key_errors)
        row["line_reads"] = {key: sorted(values) for key, values in self.line_reads.items()}
        write_json(path, row)

    def block(self, providers, reason):
        for provider in providers:
            self.key_blocked.update(f"{provider}:{key}" for key in PROVIDERS[provider][3])
        self.key_errors.add(reason)

    def record_key(self, provider, key, site):
        if provider not in PROVIDERS:
            return
        identifier = f"{provider}:{key}"
        if self.active is None:
            self.key_blocked.add(identifier)
        else:
            self.key_reads[identifier].add(self.active)
            self.line_reads[site].add(self.active)

    def exact_table(self, provider, table):
        _, module_name, class_name, expected = PROVIDERS[provider]
        module = sys.modules.get(module_name)
        expressions = sys.modules.get("sqlglot.expressions")
        if type(module) is not types.ModuleType or type(expressions) is not types.ModuleType:
            return False
        if class_name is None:
            canonical = vars(module).get(WIDTH)
        else:
            cls = vars(module).get(class_name)
            canonical = vars(cls).get(ATTRIBUTE) if isinstance(cls, type) else None
        if type(table) is not dict or table is not canonical or len(table) != len(expected):
            return False
        for name, value in expected.items():
            key = vars(expressions).get(name)
            if not isinstance(key, type) or key not in table:
                return False
            actual = table[key]
            if type(actual) is not type(value) or actual != value:
                return False
        previous = self.identities.setdefault(provider, table)
        return previous is table

    def lookup_attribute(self, obj, key, site):
        table = getattr(obj, ATTRIBUTE)
        cls = obj if isinstance(obj, type) else type(obj)
        owner = next((base for base in type.__getattribute__(cls, "__mro__")
                      if ATTRIBUTE in type.__getattribute__(base, "__dict__")), None)
        if owner is None or inspect.getattr_static(obj, ATTRIBUTE, None) is not table:
            self.block(CLASS_PROVIDERS, "noncanonical attribute resolution at " + site)
            return table[key]
        owner_identity = (type.__getattribute__(owner, "__module__"),
                          type.__getattribute__(owner, "__qualname__"))
        provider = next((name for name in CLASS_PROVIDERS
                         if PROVIDERS[name][1:3] == owner_identity), None)
        if provider is None:
            if owner_identity != OTHER_PROVIDER:
                self.block(CLASS_PROVIDERS, "unknown dictionary owner at " + site)
            else:
                module = sys.modules.get(OTHER_PROVIDER[0])
                canonical = vars(module).get(OTHER_PROVIDER[1]) if type(module) is types.ModuleType else None
                expressions = sys.modules.get("sqlglot.expressions")
                if (canonical is not owner or type(table) is not dict or len(table) != 3
                        or any(vars(expressions).get(name) not in table or table[vars(expressions)[name]] is not None
                               for name in ("Except", "Intersect", "Union"))):
                    self.block(CLASS_PROVIDERS, "noncanonical independent BigQuery dictionary")
                elif self.active is not None:
                    self.line_reads[site].add(self.active)
            return table[key]
        if (not self.exact_table(provider, table) or not isinstance(key, type)
                or key is not vars(sys.modules["sqlglot.expressions"]).get(key.__name__)
                or key.__name__ not in PROVIDERS[provider][3]):
            self.block((provider,), "noncanonical table or key at " + site)
            return table[key]
        self.record_key(provider, key.__name__, site)
        return table[key]

    def lookup_width(self, table, key, site):
        provider = "duckdb._SEQ_BYTE_WIDTH"
        if (not self.exact_table(provider, table) or not isinstance(key, type)
                or key is not vars(sys.modules["sqlglot.expressions"]).get(key.__name__)
                or key.__name__ not in PROVIDERS[provider][3]):
            self.block((provider,), "noncanonical width dictionary or key at " + site)
            return table[key]
        frame = sys._getframe(1)
        while frame is not None and not (
                frame.f_code.co_qualname == "Generator.generate"
                and Path(frame.f_code.co_filename) == self.repo / "sqlglot/generator.py"):
            frame = frame.f_back
        if frame is None or frame.f_locals.get("copy") is not True:
            self.key_blocked.add(f"{provider}:{key.__name__}")
            self.key_errors.add("width read outside copying Generator.generate")
        self.record_key(provider, key.__name__, site)
        return table[key]

    def opaque_width(self, table):
        self.block(("duckdb._SEQ_BYTE_WIDTH",), "width dictionary escaped direct subscript")
        return table

    def getattr(self, obj, name, *default):
        value = getattr(obj, name, *default)
        if name == ATTRIBUTE:
            self.block(CLASS_PROVIDERS, "table attribute escaped direct subscript")
        elif name == WIDTH:
            self.block(("duckdb._SEQ_BYTE_WIDTH",), "width attribute escaped direct subscript")
        elif name == "__dict__":
            self.namespace(obj)
        return value

    def namespace(self, obj):
        if obj is sys.modules.get("sqlglot.generators.duckdb"):
            self.block(("duckdb._SEQ_BYTE_WIDTH",), "width module namespace exposed")
        cls = obj if isinstance(obj, type) else type(obj)
        if any(ATTRIBUTE in type.__getattribute__(base, "__dict__")
               for base in type.__getattribute__(cls, "__mro__")):
            self.block(CLASS_PROVIDERS, "dictionary provider namespace exposed")

    def vars(self, obj):
        value = vars(obj)
        self.namespace(obj)
        return value

    @staticmethod
    def ordinary_namespace(obj, value):
        if isinstance(obj, type) and type(value) is types.MappingProxyType:
            meta = type(obj)
            return type.__getattribute__(meta, "__getattribute__") is type.__getattribute__
        return type(obj) is types.ModuleType and type(value) is dict

    def namespace_item(self, obj, literal, use_vars=False):
        value = vars(obj) if use_vars else getattr(obj, "__dict__")
        if literal in (ATTRIBUTE, WIDTH) or not self.ordinary_namespace(obj, value):
            self.namespace(obj)
        return value[literal]

    def namespace_method(self, obj, literal, use_vars=False):
        value = vars(obj) if use_vars else getattr(obj, "__dict__")
        if literal in (ATTRIBUTE, WIDTH) or not self.ordinary_namespace(obj, value):
            self.namespace(obj)
        return value.get


class KeyInstrument(ast.NodeTransformer):
    def __init__(self, path, tree, attribute_names=None):
        self.path = path
        self.expected = {line: ast.dump(ast.parse(source, mode="eval").body, include_attributes=False)
                         for (path_, line), source in SITES.items() if path_ == path}
        self.shadowed = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
                         and isinstance(node.ctx, (ast.Store, ast.Del))}
        self.shadowed.update(node.arg for node in ast.walk(tree) if isinstance(node, ast.arg))
        self.shadowed.update(node.name for node in ast.walk(tree)
                             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self.shadowed.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        self.star = any(isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
                        for node in ast.walk(tree))

    @staticmethod
    def call(name, arguments, node):
        return ast.copy_location(ast.Call(
            func=ast.Attribute(value=ast.Name(id="_br_dispatch", ctx=ast.Load()), attr=name, ctx=ast.Load()),
            args=arguments, keywords=[]), node)

    def visit_Subscript(self, node):
        namespace = self.namespace(node.value)
        if (namespace is not None and isinstance(node.ctx, ast.Load)
                and isinstance(node.slice, ast.Constant) and type(node.slice.value) is str):
            obj, use_vars = namespace
            return self.call("namespace_item", [self.visit(obj), node.slice, ast.Constant(use_vars)], node)
        if (isinstance(node.ctx, ast.Load) and node.lineno in self.expected
                and ast.dump(node, include_attributes=False) == self.expected[node.lineno]):
            site = ast.Constant(f"{self.path}:{node.lineno}")
            if isinstance(node.value, ast.Attribute):
                return self.call("lookup_attribute", [self.visit(node.value.value), self.visit(node.slice), site], node)
            return self.call("lookup_width", [node.value, self.visit(node.slice), site], node)
        return self.generic_visit(node)

    def visit_Attribute(self, node):
        node.value = self.visit(node.value)
        if isinstance(node.ctx, ast.Load) and node.attr in (ATTRIBUTE, WIDTH, "__dict__"):
            return self.call("getattr", [node.value, ast.Constant(node.attr)], node)
        return node

    def visit_Name(self, node):
        if node.id == WIDTH and isinstance(node.ctx, ast.Load):
            return self.call("opaque_width", [node], node)
        return node

    def visit_Call(self, node):
        original = node.func
        namespace = self.namespace(original.value) if isinstance(original, ast.Attribute) and original.attr == "get" else None
        if (namespace is not None and 1 <= len(node.args) <= 2 and not node.keywords
                and isinstance(node.args[0], ast.Constant) and type(node.args[0].value) is str):
            obj, use_vars = namespace
            node.func = self.call("namespace_method", [self.visit(obj), node.args[0], ast.Constant(use_vars)], original)
            node.args = [self.visit(argument) for argument in node.args]
            return node
        node = self.generic_visit(node)
        if (isinstance(original, ast.Name) and original.id in ("getattr", "vars")
                and not self.star and original.id not in self.shadowed
                and (original.id != "vars" or node.args)):
            node.func = ast.Attribute(value=ast.Name(id="_br_dispatch", ctx=ast.Load()),
                                      attr=original.id, ctx=ast.Load())
        return node

    def namespace(self, node):
        if isinstance(node, ast.Attribute) and node.attr == "__dict__" and isinstance(node.ctx, ast.Load):
            return node.value, False
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "vars"
                and not self.star and "vars" not in self.shadowed and len(node.args) == 1 and not node.keywords):
            return node.args[0], True
        return None


def install(config):
    observer = KeyObserver(config)
    sys.meta_path.insert(0, Finder(observer, KeyInstrument))
    return observer


def child_install():
    if os.environ.get("BR_DISPATCH_CONFIG"):
        install(read_json(os.environ["BR_DISPATCH_CONFIG"]))


def collect(args):
    repo = args.repo.resolve()
    observations = args.observations.resolve()
    output = args.output.resolve()
    coverage_path = args.coverage.resolve()
    positions_path = args.positions.resolve()
    metadata, coverage, manifest = map(read_json, (args.checkout_metadata, coverage_path, args.manifest))
    tests = manifest["tests"] if isinstance(manifest, dict) else manifest
    tests = [{"number": row["number"], "id": row["id"]} for row in tests]
    if (metadata["base_commit"] != BASE_COMMIT or coverage["base_commit"] != BASE_COMMIT
            or tests != coverage["tests"] or coverage["context"]["complete"] is not True
            or len({row["number"] for row in tests}) != len(tests)
            or len({row["id"] for row in tests}) != len(tests)):
        raise ValueError("baseline, manifest or coverage mismatch")
    if observations.exists() and any(observations.iterdir()):
        raise ValueError("observation directory must be fresh")
    observations.mkdir(parents=True, exist_ok=True)
    modules = {}
    for path, digest in metadata["files"].items():
        if hashlib.sha256((repo / path).read_bytes()).hexdigest() != digest:
            raise ValueError("baseline source mismatch: " + path)
        if path.endswith(".py") and path.startswith(("sqlglot/", "tests/")):
            module = path[:-3].replace("/", ".")
            modules[module[:-9] if module.endswith(".__init__") else module] = path
    for path, line in SITES:
        tree = ast.parse((repo / path).read_bytes(), path)
        matches = [node for node in ast.walk(tree) if isinstance(node, ast.Subscript) and node.lineno == line
                   and ast.dump(node, include_attributes=False)
                   == ast.dump(ast.parse(SITES[(path, line)], mode="eval").body, include_attributes=False)]
        if len(matches) != 1:
            raise ValueError("expected exact key lookup site: " + path)
    config = {"repo": str(repo), "observations": str(observations), "modules": modules,
              "process_hooks": True, "method": POLICY}
    write_json(observations / "config.json", config)
    os.environ.update(BR_DISPATCH_CONFIG=str(observations / "config.json"),
                      PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0")
    os.chdir(repo)
    sys.path.insert(0, str(repo))
    observer = install(config)
    import pytest
    from _pytest.subtests import SubtestReport
    by_id = {row["id"]: row["number"] for row in tests}
    outcomes, extra, collected, collection_errors = {}, {}, [], []
    subtests = Counter()

    class Plugin:
        def pytest_collection_modifyitems(self, items):
            collected.extend(item.nodeid for item in items)

        def pytest_collectreport(self, report):
            if report.failed:
                collection_errors.append(report.nodeid)

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item, nextitem):
            observer.active = by_id.get(item.nodeid)
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
            target = outcomes if report.nodeid in by_id else extra
            key = by_id.get(report.nodeid, report.nodeid)
            phases = target.setdefault(key, {})
            if phases.get(report.when) != "failed":
                phases[report.when] = report.outcome
            if isinstance(report, SubtestReport):
                subtests[report.outcome] += 1

    with tempfile.TemporaryDirectory(prefix="key-table-child-") as temporary:
        (Path(temporary) / "sitecustomize.py").write_text(
            "from tools.build_key_tables import child_install\nchild_install()\n", "utf-8")
        os.environ["PYTHONPATH"] = os.pathsep.join([
            temporary, str(PROJECT), str(repo), os.environ.get("PYTHONPATH", "")])
        exit_code = int(pytest.main(["-q", "--assert=plain", "-p", "no:cacheprovider", "tests"], plugins=[Plugin()]))
    for module_name, attribute in (("multiprocessing.forkserver", "_forkserver"),
                                   ("multiprocessing.resource_tracker", "_resource_tracker")):
        module = sys.modules.get(module_name)
        service = getattr(module, attribute, None) if module is not None else None
        if service is not None:
            service._stop()
    observer.finish()
    passed, skipped, failed, missing = [], [], [], []
    for test in tests:
        phases = outcomes.get(test["number"], {})
        if "failed" in phases.values():
            failed.append(test["number"])
        elif "skipped" in phases.values():
            skipped.append(test["number"])
        elif phases.get("call") == "passed" and phases.get("teardown") == "passed":
            passed.append(test["number"])
        else:
            missing.append(test["number"])
    ignored_skipped = sorted(key for key, phases in extra.items()
                             if "skipped" in phases.values() and "failed" not in phases.values())
    unknown_tests = sorted(set(extra) - set(ignored_skipped))
    starts = sorted(observations.glob("*.start.json"))
    unfinished, process_errors, children, pids = [], [], {}, set()
    reads, blocked, line_reads, key_errors = defaultdict(set), set(), defaultdict(set), set()
    for start in starts:
        pids.add(str(read_json(start)["pid"]))
        done = start.with_name(start.name.replace(".start.json", ".done.json"))
        if not done.exists():
            unfinished.append(start.name)
            continue
        row = read_json(done)
        children.update(row.get("children", {}))
        process_errors.extend(row.get("process_errors", ()))
        for key, numbers in row["key_reads"].items():
            reads[key].update(numbers)
        blocked.update(row["key_blocked"])
        key_errors.update(row["key_errors"])
        for site, numbers in row["line_reads"].items():
            line_reads[site].update(numbers)
    unobserved = sorted(set(children) - pids)
    expected = {row["number"] for row in tests}
    unknown_numbers = set().union(*reads.values(), *line_reads.values()) - expected
    complete = not (exit_code or failed or missing or unknown_tests or collection_errors or unfinished
                    or process_errors or unobserved or subtests["failed"] or unknown_numbers)
    context = {"complete": complete, "passed": passed, "skipped": skipped, "failed": failed,
               "missing": missing, "unknown_tests": unknown_tests, "ignored_skipped_tests": ignored_skipped,
               "collection_errors": collection_errors, "unfinished": unfinished, "process_errors": process_errors,
               "unobserved_children": unobserved, "unknown_numbers": sorted(unknown_numbers),
               "processes": len(starts), "pytest_exit_code": exit_code, "subtests": dict(subtests),
               "key_errors": sorted(key_errors), "line_reads": {key: sorted(value) for key, value in line_reads.items()},
               "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "python": sys.version, "platform": sys.platform, "pytest": pytest.__version__}
    temporary = {"version": 1, "base_commit": BASE_COMMIT, "tests": tests, "paths": coverage["paths"],
                 "attributes": {}, "context": dict(context)}
    refresh_dependencies(temporary, coverage, [observations])
    groups = [set(row["tests"]) for row in temporary["context"]["shared_pool_groups"]]
    providers = {}
    for provider, (path, module_name, class_name, values) in PROVIDERS.items():
        expected_sites = [site for site in SITES if (site[0].endswith("duckdb.py")) == (class_name is None)]
        fallback = set(skipped) | set(coverage["context"]["untraced_tests"])
        for source_path, line in expected_sites:
            fallback.update(set(coverage["paths"][source_path]["lines"][str(line)])
                            - line_reads[f"{source_path}:{line}"])
        entries = {}
        for key in values:
            selected = set(reads[f"{provider}:{key}"]) | fallback
            while True:
                widened = selected.union(*(group for group in groups if group & selected))
                if selected == widened:
                    break
                selected = widened
            entries[key] = sorted(selected)
        providers[path] = {"source_sha256": normalized_hash(repo / path), "provider": provider,
                           "keys": entries, "blocked_keys": sorted(key for key in values
                                                                    if f"{provider}:{key}" in blocked)}
    artifact = {"version": 1, "policy": POLICY, "base_commit": BASE_COMMIT,
                "coverage_sha256": hashlib.sha256(coverage_path.read_bytes()).hexdigest(),
                "positions_sha256": positions_digest(read_json(positions_path)), "tests": tests,
                "complete": complete, "providers": providers, "shared_pool_groups": [sorted(group) for group in groups],
                "untraced": sorted(set(coverage["context"]["untraced_tests"]) | set(skipped)), "context": context}
    write_json(output, artifact)
    print(json.dumps({"complete": complete, "processes": len(starts), "passed": len(passed),
                      "subtests": dict(subtests), "key_errors": sorted(key_errors),
                      "providers": {path: {"keys": {key: len(numbers) for key, numbers in row["keys"].items()},
                                           "blocked_keys": row["blocked_keys"]} for path, row in providers.items()}}, sort_keys=True))
    return 0 if complete else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "manifest", "checkout-metadata", "coverage", "positions", "observations", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    return collect(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
