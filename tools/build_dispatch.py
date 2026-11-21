#!/usr/bin/env python3
"""Observe baseline class configuration reads, initializer edges, and class use.

Run with the official Python/dependencies. No candidate patches or labels are
inputs. Subprocesses inherit the same loader through a temporary sitecustomize.
"""
from __future__ import annotations

import argparse
import ast
import atexit
import builtins
import gzip
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from m2_dispatch import BASE_COMMIT, declarations, key_for


def read_json(path):
    with (gzip.open if str(path).endswith(".gz") else open)(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(gzip.compress(raw, mtime=0) if str(path).endswith(".gz") else raw)


class Observer:
    def __init__(self, config):
        self.config = config
        self.repo = Path(config["repo"])
        self.modules = config["modules"]
        self.active = int(os.environ["BR_DISPATCH_TEST"]) if "BR_DISPATCH_TEST" in os.environ else None
        self.stack = []
        self.reads = {}
        self.edges = {}
        self.blocked = set()
        self.serial = 0
        self.class_cache = {}
        self.attribute_cache = {}
        self.last_read = {}
        self.done = False
        self.children = {}
        self.process_errors = []
        self.spawning = 0
        self.identity = f"{os.getpid()}-{uuid.uuid4().hex}"
        self.output = Path(config["observations"])
        write_json(self.output / f"{self.identity}.start.json", {"test": self.active, "pid": os.getpid()})
        atexit.register(self.finish)
        if config.get("process_hooks") and hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self.after_fork)
        if config.get("process_hooks"):
            self.process_hooks()

    def process_hooks(self):
        import subprocess
        import _posixsubprocess

        def wrap_spawn(original, kind):
            def spawn(*args, **kwargs):
                previous_pid = os.getpid()
                self.spawning += 1
                try:
                    child = original(*args, **kwargs)
                finally:
                    self.spawning = self.spawning - 1 if os.getpid() == previous_pid else 0
                if child > 0:
                    self.children[str(child)] = {"kind": kind, "test": self.active}
                return child
            return spawn

        if hasattr(os, "fork"):
            os.fork = wrap_spawn(os.fork, "fork")
        for name in ("posix_spawn", "posix_spawnp"):
            if hasattr(os, name):
                setattr(os, name, wrap_spawn(getattr(os, name), name))
        _posixsubprocess.fork_exec = wrap_spawn(_posixsubprocess.fork_exec, "fork_exec")
        subprocess._fork_exec = _posixsubprocess.fork_exec
        original_popen = subprocess.Popen.__init__

        def popen(process, *args, **kwargs):
            self.spawning += 1
            try:
                original_popen(process, *args, **kwargs)
            finally:
                self.spawning -= 1
            self.children[str(process.pid)] = {"kind": "Popen", "test": self.active}

        subprocess.Popen.__init__ = popen
        original_exit = os._exit

        def immediate_exit(status):
            self.finish()
            original_exit(status)

        os._exit = immediate_exit

        def audit(event, args):
            if event in ("os.fork", "os.posix_spawn", "subprocess.Popen") and not self.spawning:
                self.process_errors.append("unwrapped process event: " + event)
            elif event in ("os.exec", "os.execve", "os.forkpty"):
                self.process_errors.append("unsupported transition: " + event)

        sys.addaudithook(audit)

    def after_fork(self):
        self.identity = f"{os.getpid()}-{uuid.uuid4().hex}"
        self.reads, self.edges, self.blocked = {}, {}, set()
        self.children, self.process_errors = {}, []
        self.last_read = {}
        self.done = False
        write_json(self.output / f"{self.identity}.start.json", {"test": self.active, "pid": os.getpid()})
        # multiprocessing exits with os._exit, bypassing Python atexit.
        import multiprocessing.util
        multiprocessing.util.Finalize(None, self.finish, exitpriority=-100)

    def finish(self):
        if self.done:
            return
        self.done = True
        write_json(self.output / f"{self.identity}.done.json", {
            "test": self.active, "reads": {k: sorted(v) for k, v in self.reads.items()},
            "edges": {k: sorted(v) for k, v in self.edges.items()}, "blocked": sorted(self.blocked),
            "pid": os.getpid(), "children": self.children, "process_errors": self.process_errors})

    def begin(self, key):
        self.stack.append(key)

    def end(self):
        self.stack.pop()

    def record(self, key):
        if self.stack:
            for target in self.stack:
                if key != target:
                    self.edges.setdefault(key, set()).add(target)
        elif self.active is not None:
            if self.last_read.get(key) != self.active:
                self.reads.setdefault(key, set()).add(self.active)
                self.last_read[key] = self.active
        else:
            self.blocked.add(key)

    def classkey(self, cls):
        identity = id(cls)
        if identity in self.class_cache:
            return self.class_cache[identity][1]
        try:
            module = type.__getattribute__(cls, "__module__")
            path = self.modules.get(module)
            if path is None:
                self.class_cache[identity] = (cls, None)
                return None
            result = key_for(path, type.__getattribute__(cls, "__qualname__"), "*")
            self.class_cache[identity] = (cls, result)
            return result
        except (TypeError, AttributeError):
            return None

    def use(self, cls):
        key = self.classkey(cls)
        if key is not None:
            self.record(key)

    def getattr(self, obj, name, *default):
        value = builtins.getattr(obj, name, *default)
        cls = obj if isinstance(obj, type) else type(obj)
        classkey = self.classkey(cls)
        if classkey is None:
            return value
        self.record(classkey)
        if name == "__dict__" and isinstance(obj, type):
            for base in type.__getattribute__(cls, "__mro__"):
                basekey = self.classkey(base)
                if basekey is not None:
                    for attr in vars(base):
                        self.record(basekey[:-1] + attr)
        else:
            # A normal class attribute lookup observes the defining MRO entry,
            # not every ancestor with an attribute of the same name.
            lookup = (id(cls), name)
            if lookup not in self.attribute_cache:
                result = None
                for base in type.__getattribute__(cls, "__mro__"):
                    if name in vars(base):
                        basekey = self.classkey(base)
                        if basekey is not None:
                            result = basekey[:-1] + name
                        break
                self.attribute_cache[lookup] = result
            key = self.attribute_cache[lookup]
            if key is not None:
                self.record(key)
        return value

    def callable(self, obj):
        if isinstance(obj, type):
            self.use(obj)
        return obj

    def local(self, value, key):
        self.record(key)
        return value

    def vars(self, obj):
        value = builtins.vars(obj)
        cls = obj if isinstance(obj, type) else type(obj)
        self.use(cls)
        if isinstance(obj, type):
            for base in type.__getattribute__(cls, "__mro__"):
                basekey = self.classkey(base)
                if basekey is not None:
                    for attr in vars(base):
                        self.record(basekey[:-1] + attr)
        return value


class Instrument(ast.NodeTransformer):
    def __init__(self, path, tree, attribute_names=None, *, declaration_reader=declarations):
        self.path = path
        self.decls = declaration_reader(tree, path)
        self.targets = {id(value): key for key, value in self.decls.items()}
        self.scope = []
        self.locals = []
        self.function_depth = 0
        self.attribute_names = None if attribute_names is None else set(attribute_names)

    @staticmethod
    def call(name, args, node):
        return ast.copy_location(ast.Call(
            func=ast.Attribute(value=ast.Name(id="_br_dispatch", ctx=ast.Load()), attr=name, ctx=ast.Load()),
            args=args, keywords=[]), node)

    def wrap(self, node, key):
        begin = ast.copy_location(ast.Expr(value=self.call("begin", [ast.Constant(key)], node)), node)
        end = ast.copy_location(ast.Expr(value=self.call("end", [], node)), node)
        attempt = ast.copy_location(ast.Try(body=[node], handlers=[], orelse=[], finalbody=[end]), node)
        return [begin, attempt]

    def visit_ClassDef(self, node):
        # Function-local classes have <locals> qualnames and cannot be selected
        # as declarations, but their reads still need ordinary instrumentation.
        self.scope.append(node.name)
        self.locals.append(set())
        node.bases = [self.visit(base) for base in node.bases]
        node.keywords = [self.visit(kw) for kw in node.keywords]
        node.decorator_list = [self.visit(decorator) for decorator in node.decorator_list]
        body = []
        for child in node.body:
            result = self.visit(child)
            body.extend(result if isinstance(result, list) else [result])
            original = child
            if isinstance(original, (ast.Assign, ast.AnnAssign)):
                target = original.targets[0] if isinstance(original, ast.Assign) and len(original.targets) == 1 else getattr(original, "target", None)
                if isinstance(target, ast.Name):
                    self.locals[-1].add(target.id)
        node.body = body
        scope = ".".join(self.scope)
        self.locals.pop()
        self.scope.pop()
        return self.wrap(node, key_for(self.path, scope, "*"))

    def visit_FunctionDef(self, node):
        self.function_depth += 1
        node.body = [result for child in node.body for result in
                     ((lambda value: value if isinstance(value, list) else [value])(self.visit(child)))]
        node.decorator_list = [self.visit(item) for item in node.decorator_list]
        # Defaults execute in the enclosing scope. Preserve annotations exactly.
        node.args.defaults = [self.visit(item) for item in node.args.defaults]
        node.args.kw_defaults = [self.visit(item) if item is not None else None for item in node.args.kw_defaults]
        self.function_depth -= 1
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node):
        key = self.targets.get(id(node))
        node.value = self.visit(node.value)
        node.targets = [self.visit(target) for target in node.targets]
        return self.wrap(node, key) if key is not None else node

    def visit_AnnAssign(self, node):
        key = self.targets.get(id(node))
        if node.value is not None:
            node.value = self.visit(node.value)
        return self.wrap(node, key) if key is not None else node

    def visit_Name(self, node):
        if (isinstance(node.ctx, ast.Load) and self.scope and not self.function_depth
                and node.id in self.locals[-1]):
            return self.call("local", [node, ast.Constant(key_for(self.path, ".".join(self.scope), node.id))], node)
        return node

    def visit_Attribute(self, node):
        node.value = self.visit(node.value)
        if (isinstance(node.ctx, ast.Load) and (self.attribute_names is None
                or node.attr in self.attribute_names or node.attr.isupper())):
            return self.call("getattr", [node.value, ast.Constant(node.attr)], node)
        return node

    def visit_Call(self, node):
        original = node.func
        node = self.generic_visit(node)
        if isinstance(original, ast.Name) and original.id in ("getattr", "vars"):
            if original.id == "vars" and not node.args:
                return node
            node.func = ast.Attribute(value=ast.Name(id="_br_dispatch", ctx=ast.Load()), attr=original.id, ctx=ast.Load())
        else:
            node.func = self.call("callable", [node.func], node)
        return node


class Loader(importlib.machinery.SourceFileLoader):
    instrument_type = Instrument

    def source_to_code(self, data, path, *, _optimize=-1):
        source = importlib.util.decode_source(data)
        relative = Path(path).resolve().relative_to(self.observer.repo).as_posix()
        tree = ast.parse(source, path)
        tree = self.instrument_type(relative, tree, self.observer.config.get("attribute_names")).visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)

    def get_code(self, fullname):
        # Never reuse uninstrumented bytecode from the source checkout.
        return self.source_to_code(self.get_data(self.path), self.path)

    def exec_module(self, module):
        module.__dict__["_br_dispatch"] = self.observer
        super().exec_module(module)


class Finder(importlib.abc.MetaPathFinder):
    def __init__(self, observer, instrument_type=Instrument):
        self.observer = observer
        self.instrument_type = instrument_type

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self.observer.modules:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            loader = Loader(fullname, spec.origin)
            loader.observer = self.observer
            loader.instrument_type = self.instrument_type
            spec.loader = loader
        return spec


def install(config, *, observer_type=Observer, instrument_type=Instrument):
    observer = observer_type(config)
    sys.meta_path.insert(0, Finder(observer, instrument_type))
    return observer


def child_install():
    if os.environ.get("BR_DISPATCH_CONFIG"):
        install(read_json(os.environ["BR_DISPATCH_CONFIG"]))


def merge_artifacts(inputs):
    """Union baseline observations after independently repairing failed tests.

    Every numbered test must have a complete passing or skipped protocol in at
    least one input. Process/collection uncertainty cannot be repaired this way.
    """
    if not inputs:
        raise ValueError("at least one observation artifact is required")
    first = inputs[0]
    expected = {row["number"] for row in first["tests"]}
    passed, skipped, failed = set(), set(), set()
    attributes = {key: {"tests": set(), "blocked": False} for key in first["attributes"]}
    for artifact in inputs:
        if (artifact["version"] != 1 or artifact["base_commit"] != BASE_COMMIT
                or artifact["tests"] != first["tests"] or artifact["paths"] != first["paths"]
                or set(artifact["attributes"]) != set(attributes)):
            raise ValueError("baseline, manifest, source, or declaration mismatch")
        context = artifact["context"]
        if any(context.get(key) for key in ("extra", "collection_errors", "unfinished",
                                           "process_errors", "unobserved_children")):
            raise ValueError("uncertain process or collection observations cannot be merged")
        failed.update(context["failed"])
        skipped.update(context["skipped"])
        observed_passed = context.get("passed")
        if observed_passed is None:
            observed_passed = expected - set(context["missing"]) - set(context["failed"]) - set(context["skipped"])
        passed.update(observed_passed)
        for key, dependency in artifact["attributes"].items():
            if set(dependency["tests"]) - expected:
                raise ValueError("unknown test in dependency map")
            attributes[key]["tests"].update(dependency["tests"])
            attributes[key]["blocked"] |= dependency["blocked"]
    missing = expected - passed - skipped
    if missing:
        raise ValueError(f"tests lack a completed baseline protocol: {sorted(missing)}")
    context = {"complete": True, "passed": sorted(passed), "skipped": sorted(skipped),
               "failed": [], "missing": [], "extra": [], "collection_errors": [],
               "unfinished": [], "process_errors": [], "unobserved_children": [],
               "processes": sum(artifact["context"]["processes"] for artifact in inputs),
               "observations": len(inputs), "repaired": sorted(failed & passed),
               "method": "base-attribute-initializer-process-tree-merged-v1"}
    for dependency in attributes.values():
        dependency["tests"] = sorted(dependency["tests"] | skipped)
    return {"version": 1, "base_commit": BASE_COMMIT, "tests": first["tests"],
            "paths": first["paths"], "attributes": attributes, "context": context}


def refresh_dependencies(artifact, coverage, directories):
    """Reattribute shared-pool child reads using baseline dispatch-site coverage.

    A worker inherits the number of its first submitter. If construction was
    unattributed (class/session setup), every numbered submitter in the same
    source owner group must receive that worker's read dependencies. Independent
    per-test pools and unrelated subprocess tests retain their own attribution.
    """
    if (coverage["base_commit"] != artifact["base_commit"] or coverage["tests"] != artifact["tests"]
            or coverage["context"]["complete"] is not True):
        raise ValueError("shared-pool coverage baseline or manifest mismatch")
    for path, row in artifact["paths"].items():
        if coverage["paths"][path]["sha256"] != row["sha256"]:
            raise ValueError("shared-pool source fingerprint mismatch")
    sites = coverage["context"]["process_sites"]
    owner_files = {site["file"] for site in sites
                   if site["construction_only"] and not site["tests"]}
    groups = []
    for path in sorted(owner_files):
        numbers = {number for site in sites if site["file"] == path and not site["construction_only"]
                   for number in site["tests"]}
        if numbers:
            groups.append({"file": path, "tests": sorted(numbers)})
    widened = {}
    for group in groups:
        for number in group["tests"]:
            widened.setdefault(number, set()).update(group["tests"])
    # Overlapping owner groups are a single conservative dependency component.
    for number in list(widened):
        pending, component = [number], set()
        while pending:
            current = pending.pop()
            if current not in component:
                component.add(current)
                pending.extend(widened.get(current, ()))
        for current in component:
            widened[current] = component
    reads, edges, blocked, widened_processes = {}, {}, set(), 0
    scopes = set()
    for directory in directories:
        config_path = Path(directory) / "config.json"
        if config_path.exists():
            configuration = read_json(config_path)
            scopes.add("declarations-and-uppercase" if "attribute_names" in configuration else "all-attributes")
        for start in sorted(Path(directory).glob("*.start.json")):
            start_data = read_json(start)
            done = start.with_name(start.name.replace(".start.json", ".done.json"))
            if not done.exists():
                raise ValueError("cannot refresh incomplete process observations")
            data = read_json(done)
            child = start_data["test"] is not None
            if child and start_data["test"] in widened:
                widened_processes += 1
            for key, values in data["reads"].items():
                numbers = set(values)
                if child:
                    for number in values:
                        numbers.update(widened.get(number, ()))
                reads.setdefault(key, set()).update(numbers)
            for key, values in data["edges"].items():
                edges.setdefault(key, set()).update(values)
            blocked.update(data["blocked"])
    expected = {row["number"] for row in artifact["tests"]}
    skipped = set(artifact["context"]["skipped"])
    for key, dependency in artifact["attributes"].items():
        pending, visited, selected = [key], set(), set(skipped)
        is_blocked = False
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            selected.update(reads.get(current, ()))
            is_blocked |= current in blocked
            pending.extend(edges.get(current, ()))
        if selected - expected:
            raise ValueError("unknown test in refreshed observations")
        dependency.update(tests=sorted(selected), blocked=is_blocked)
    artifact["context"]["shared_pool_groups"] = groups
    artifact["context"]["shared_pool_processes"] = widened_processes
    artifact["context"]["derived_analysis"] = "baseline-shared-pool-dispatch-v1"
    artifact["context"]["attribute_scopes"] = sorted(scopes)
    return artifact


def main(*, observer_type=Observer, instrument_type=Instrument, declaration_reader=declarations,
         method="base-attribute-initializer-process-tree-v1", child_module="build_dispatch",
         finalize=None, require_fresh=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--checkout-metadata", type=Path)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--merge", action="append", type=Path,
                        help="merge complete baseline protocols after isolated reruns")
    parser.add_argument("--refresh-observations", action="append", type=Path,
                        help="recompute merged dependencies with shared-pool attribution")
    parser.add_argument("--coverage", type=Path,
                        default=Path(__file__).resolve().parents[1] / "maps" / "coverage.json.gz")
    parser.add_argument("--pytest-arg", action="append", default=[])
    parser.add_argument("--targeted-attributes", action="store_true",
                        help="observe declaration/uppercase names instead of all instance attributes")
    parser.add_argument("--min-test-number", type=int, default=0,
                        help="baseline replay of the numbered suffix, preserving the complete manifest")
    args = parser.parse_args()
    if args.merge:
        artifact = merge_artifacts([read_json(path) for path in args.merge])
        if args.refresh_observations:
            artifact = refresh_dependencies(artifact, read_json(args.coverage), args.refresh_observations)
        if finalize is not None:
            artifact = finalize(artifact, args)
        write_json(args.output, artifact)
        print(json.dumps({key: len(value) if key in ("passed", "missing") else value
                          for key, value in artifact["context"].items()}, sort_keys=True))
        return 0
    if any(value is None for value in (args.repo, args.manifest, args.checkout_metadata, args.observations)):
        parser.error("observation requires --repo, --manifest, --checkout-metadata, and --observations")
    args.repo = args.repo.resolve()
    args.observations = args.observations.resolve()
    if require_fresh and args.observations.exists() and any(args.observations.iterdir()):
        raise ValueError("observation directory must be fresh")
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
            if module.endswith(".__init__"):
                module = module[:-9]
            modules[module] = name
            text = source.decode().replace("\r\n", "\n")
            paths[name] = {"sha256": hashlib.sha256(text.encode()).hexdigest()}
            for key in declaration_reader(ast.parse(text), name):
                attributes[key] = None
    tests_value = read_json(args.manifest)
    tests = tests_value["tests"] if isinstance(tests_value, dict) else tests_value
    tests = [{"number": row["number"], "id": row["id"]} for row in tests]
    by_id = {row["id"]: row["number"] for row in tests}
    attribute_names = {key.rsplit("|", 1)[-1] for key in attributes} | {"__dict__"}
    config = {"repo": str(args.repo), "observations": str(args.observations),
              "modules": modules, "process_hooks": True}
    if args.targeted_attributes:
        config["attribute_names"] = sorted(attribute_names)
    config_path = args.observations / "config.json"
    write_json(config_path, config)
    os.environ["BR_DISPATCH_CONFIG"] = str(config_path)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["SKIP_INTEGRATION"] = "0"
    os.chdir(args.repo)
    sys.path.insert(0, str(args.repo))
    observer = install(config, observer_type=observer_type, instrument_type=instrument_type)
    import pytest
    outcomes, collected, collection_errors = {}, [], []

    class Plugin:
        def pytest_collection_modifyitems(self, items, config):
            deselected = [item for item in items
                          if item.nodeid in by_id and by_id[item.nodeid] < args.min_test_number]
            if deselected:
                items[:] = [item for item in items if item not in deselected]
                config.hook.pytest_deselected(items=deselected)
            collected.extend(item.nodeid for item in items)

        def pytest_collectreport(self, report):
            if report.failed:
                collection_errors.append(report.nodeid)

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item, nextitem):
            observer.active = by_id.get(item.nodeid)
            # Test code can temporarily replace class attributes. Discard MRO
            # lookup caches at protocol boundaries, retaining stable type keys.
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

    with tempfile.TemporaryDirectory(prefix="dispatch-child-") as temporary:
        (Path(temporary) / "sitecustomize.py").write_text(
            f"from {child_module} import child_install\nchild_install()\n", encoding="utf-8")
        os.environ["PYTHONPATH"] = os.pathsep.join([
            temporary, str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1]),
            str(args.repo), os.environ.get("PYTHONPATH", "")])
        exit_code = int(pytest.main(["-q", "--assert=plain", "-p", "no:cacheprovider", *args.pytest_arg, "tests"], plugins=[Plugin()]))
    # These helper processes normally survive until interpreter shutdown.
    # Finish them before checking that every child supplied a completed trace.
    for module_name, attribute in (("multiprocessing.forkserver", "_forkserver"),
                                   ("multiprocessing.resource_tracker", "_resource_tracker")):
        module = sys.modules.get(module_name)
        service = getattr(module, attribute, None) if module is not None else None
        if service is not None:
            service._stop()
    observer.finish()
    reads, edges, blocked = {}, {}, set()
    starts = sorted(args.observations.glob("*.start.json"))
    unfinished, process_errors, children, started_pids = [], [], {}, set()
    for start in starts:
        start_data = read_json(start)
        started_pids.add(str(start_data["pid"]))
        done = start.with_name(start.name.replace(".start.json", ".done.json"))
        if not done.exists():
            unfinished.append(start.name)
            continue
        data = read_json(done)
        children.update(data.get("children", {}))
        process_errors.extend(data.get("process_errors", []))
        for key, values in data["reads"].items():
            reads.setdefault(key, set()).update(values)
        for key, values in data["edges"].items():
            edges.setdefault(key, set()).update(values)
        blocked.update(data["blocked"])
    unobserved = sorted(set(children) - started_pids)
    for key in attributes:
        pending, visited, selected = [key], set(), set()
        is_blocked = False
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            selected.update(reads.get(current, ()))
            is_blocked |= current in blocked
            pending.extend(edges.get(current, ()))
        attributes[key] = {"tests": sorted(selected), "blocked": is_blocked}
    failed, skipped, missing, passed = [], [], [], []
    for row in tests:
        outcome = outcomes.get(row["id"], {})
        if "failed" in outcome.values():
            failed.append(row["number"])
        elif "skipped" in outcome.values():
            skipped.append(row["number"])
        elif not (outcome.get("call") == "passed" and outcome.get("teardown") == "passed"):
            missing.append(row["number"])
        else:
            passed.append(row["number"])
    extra = [nodeid for nodeid in collected if nodeid not in by_id and nodeid in outcomes
             and "skipped" not in outcomes.get(nodeid, {}).values()]
    complete = not (exit_code or failed or missing or extra or unfinished or collection_errors
                    or process_errors or unobserved)
    # Skipped tests cannot provide read evidence and are mandatory supplements.
    for dependency in attributes.values():
        dependency["tests"] = sorted(set(dependency["tests"]) | set(skipped))
    context = {"complete": complete, "pytest_exit_code": exit_code, "failed": failed,
               "passed": passed, "skipped": skipped, "missing": missing, "extra": extra,
               "collection_errors": collection_errors, "unfinished": unfinished,
               "process_errors": process_errors, "unobserved_children": unobserved,
               "processes": len(starts), "method": method}
    artifact = {"version": 1, "base_commit": BASE_COMMIT, "tests": tests,
                "paths": paths, "attributes": attributes, "context": context}
    artifact = refresh_dependencies(artifact, read_json(args.coverage), [args.observations])
    if finalize is not None:
        artifact = finalize(artifact, args)
    write_json(args.output, artifact)
    summary = {key: len(value) if key in ("passed", "missing") else value
               for key, value in context.items()}
    print(json.dumps({**summary, "attributes": len(attributes),
                      "unblocked": sum(not row["blocked"] for row in attributes.values())}, sort_keys=True))
    return 0 if complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
