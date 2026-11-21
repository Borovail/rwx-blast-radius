#!/usr/bin/env python3
"""Certify unentered, source-bound Python functions on the pinned baseline."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import importlib.machinery
import inspect
import json
import marshal
import os
from pathlib import Path
import sys
import tempfile
import types

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from m2_dispatch import BASE_COMMIT
from observer_targets import positions_digest
from tools.build_dispatch import Observer, read_json, write_json


POLICY = "pinned-cold-code-v1"
TOOL = 4


def fingerprint(code):
    return hashlib.sha256(marshal.dumps(code)).hexdigest()


def key_for(path, qualname, first_line):
    return json.dumps([path, qualname, first_line], separators=(",", ":"))


def candidate_codes(repo, coverage):
    candidates = {}
    for path, record in coverage["paths"].items():
        if not path.startswith("sqlglot/") or not path.endswith(".py"):
            continue
        source = (repo / path).read_bytes()
        tree = ast.parse(source, str(repo / path))
        definitions = []

        def visit(body, prefix=""):
            names = Counter(node.name for node in body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
            for node in body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or names[node.name] != 1:
                    continue
                qualname = prefix + node.name
                if type(node) is ast.ClassDef:
                    visit(node.body, qualname + ".")
                    continue
                if type(node) is not ast.FunctionDef or node.decorator_list:
                    continue
                # A nested callable's body needs a separate outer-scope argument.
                if any(isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
                       for statement in node.body for child in ast.walk(statement)):
                    continue
                lines = set(range(node.body[0].lineno, node.end_lineno + 1))
                if lines & set(record["startup"]):
                    continue
                if any(numbers for line, numbers in record["lines"].items() if int(line) in lines):
                    continue
                definitions.append((qualname, node.lineno, node.end_lineno))

        visit(tree.body)
        if not definitions:
            continue
        compiled = compile(source, str(repo / path), "exec", dont_inherit=True)
        codes = {}
        pending = [compiled]
        while pending:
            code = pending.pop()
            codes[code.co_qualname, code.co_firstlineno] = code
            pending.extend(item for item in code.co_consts if type(item) is types.CodeType)
        for qualname, line, end in definitions:
            code = codes.get((qualname, line))
            if code is None or code.co_freevars or code.co_cellvars:
                continue
            if code.co_flags & (inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR):
                continue
            candidates[key_for(path, qualname, line)] = {
                "path": path, "qualname": qualname, "first_line": line,
                "end_line": end, "code_sha256": fingerprint(code),
            }
    return candidates


class ColdObserver(Observer):
    def __init__(self, config):
        self.candidates = config["candidates"]
        self.by_path = {}
        self.by_function = {}
        for key, row in self.candidates.items():
            self.by_path.setdefault(row["path"], set()).add(key)
            self.by_function[row["path"], row["qualname"], row["first_line"]] = key
        self.codes = {}
        self.loaded = set()
        self.entered = set()
        self.rejected = {}
        self.code_observers = set()
        self.consumer_sites = {}
        self.in_audit = False
        # Reading these framework code objects occurs before audit installation.
        self.loader_code = importlib.machinery.SourceFileLoader.get_code.__code__
        self.exec_module_code = importlib.machinery.SourceFileLoader.exec_module.__code__
        self.metadata_getters = {
            function.__code__: function.__name__
            for function in (inspect._has_code_flag, inspect._signature_is_functionlike,
                             inspect._signature_from_function)
        }
        super().__init__(config)
        sys.monitoring.use_tool_id(TOOL, "cold-code-baseline")
        sys.monitoring.register_callback(TOOL, sys.monitoring.events.PY_START, self.on_enter)
        sys.monitoring.register_callback(TOOL, sys.monitoring.events.PY_RESUME, self.on_enter)
        sys.monitoring.register_callback(TOOL, sys.monitoring.events.PY_RETURN, self.on_loader_return)
        sys.monitoring.set_local_events(TOOL, self.loader_code, sys.monitoring.events.PY_RETURN)
        sys.addaudithook(self.on_audit)

    def relative(self, filename):
        if not isinstance(filename, (str, bytes)):
            return None
        filename = os.fsdecode(filename).replace("\\", "/")
        prefix = str(self.repo).replace("\\", "/") + "/"
        if not filename.startswith(prefix):
            return None
        relative = filename[len(prefix):]
        if ".." in relative.split("/"):
            return None
        return relative

    def after_fork(self):
        super().after_fork()
        self.entered = set()
        self.rejected = {}
        self.code_observers = set()
        self.consumer_sites = {}
        self.in_audit = False

    def reject(self, keys, reason):
        for key in keys:
            self.rejected.setdefault(key, set()).add(reason)

    def consumer(self, keys, reason, caller=None):
        if caller is not None:
            code = caller.f_code
            site = json.dumps([reason, self.active, code.co_filename, code.co_qualname, caller.f_lasti])
            self.consumer_sites[site] = self.consumer_sites.get(site, 0) + 1
        if self.active == self.config["typing_observer"]:
            self.code_observers.add(self.active)
        else:
            self.reject(keys, reason)

    def on_enter(self, code, offset):
        key = self.codes.get(id(code))
        if key is not None and key[0] is code:
            self.entered.add(key[1])

    def affected_codes(self, code):
        keys = set()
        pending = [code]
        while pending:
            value = pending.pop()
            path = self.relative(value.co_filename)
            key = self.by_function.get((path, value.co_qualname, value.co_firstlineno))
            if key is not None:
                keys.add(key)
            pending.extend(item for item in value.co_consts if type(item) is types.CodeType)
        return keys

    def on_loader_return(self, code, offset, result):
        if code is not self.loader_code or type(result) is not types.CodeType or self.in_audit or self.done:
            return
        self.in_audit = True
        try:
            path = self.relative(result.co_filename)
            if path not in self.by_path:
                return
            frame = sys._getframe(1)
            parent = frame.f_back
            if parent is None or parent.f_code is not self.exec_module_code:
                self.consumer(self.by_path[path], "external-source-loader-get-code", parent)
        finally:
            self.in_audit = False

    def ordinary_import(self, frame):
        seen_loader = False
        for _ in range(8):
            if frame is None:
                return False
            code = frame.f_code
            if code is self.loader_code:
                seen_loader = True
            if code is self.exec_module_code:
                return seen_loader
            if not code.co_filename.startswith("<frozen importlib.") and code.co_filename != __file__:
                return False
            frame = frame.f_back
        return False

    def on_audit(self, event, arguments):
        if self.in_audit or self.done:
            return
        if event not in ("exec", "compile", "open", "object.__getattr__", "function.__new__", "code.__new__"):
            return
        self.in_audit = True
        try:
            if event == "exec" and type(arguments[0]) is types.CodeType:
                pending = [arguments[0]]
                while pending:
                    code = pending.pop()
                    path = self.relative(code.co_filename)
                    key = self.by_function.get((path, code.co_qualname, code.co_firstlineno))
                    if key is not None:
                        if fingerprint(code) != self.candidates[key]["code_sha256"]:
                            self.reject((key,), "source-code-fingerprint-mismatch")
                        else:
                            self.loaded.add(key)
                            self.codes[id(code)] = code, key
                            sys.monitoring.set_local_events(TOOL, code,
                                sys.monitoring.events.PY_START | sys.monitoring.events.PY_RESUME)
                    pending.extend(value for value in code.co_consts if type(value) is types.CodeType)
                return
            try:
                caller = sys._getframe(1)
            except ValueError:
                caller = None
            if event == "object.__getattr__" and len(arguments) == 2:
                obj, name = arguments
                keys = set()
                if name == "__code__" and type(obj) is types.FunctionType:
                    code = object.__getattribute__(obj, "__code__")
                    keys = self.affected_codes(code)
                    # These exact pinned inspect helpers consume only callable
                    # kinds, argument names/counts and flags. They return no code
                    # object or literal from the function body.
                    if caller is not None and caller.f_code in self.metadata_getters:
                        return
                elif name == "f_code" and type(obj) is types.FrameType:
                    keys = self.affected_codes(obj.f_code)
                if keys:
                    self.consumer(keys, "startup-code-content-reader", caller)
            elif event in ("compile", "open"):
                filename = arguments[1] if event == "compile" else arguments[0]
                path = self.relative(filename)
                if path in self.by_path and not self.ordinary_import(caller):
                    self.consumer(self.by_path[path], "startup-source-or-compile-reader", caller)
            elif event == "function.__new__" and arguments and type(arguments[0]) is types.CodeType:
                path = self.relative(arguments[0].co_filename)
                if path in self.by_path:
                    self.consumer(self.by_path[path], "startup-function-code-clone", caller)
            elif event == "code.__new__" and len(arguments) > 1:
                path = self.relative(arguments[1])
                if path in self.by_path:
                    self.consumer(self.by_path[path], "startup-code-clone", caller)
        finally:
            self.in_audit = False

    def bound_keys(self):
        bound = set()
        self.in_audit = True
        try:
            for key in self.loaded:
                row = self.candidates[key]
                module_name = row["path"][:-3].replace("/", ".")
                if module_name.endswith(".__init__"):
                    module_name = module_name[:-9]
                value = sys.modules.get(module_name)
                if type(value) is not types.ModuleType:
                    continue
                for part in row["qualname"].split("."):
                    namespace = vars(value) if type(value) is types.ModuleType or isinstance(value, type) else {}
                    value = namespace.get(part)
                if type(value) is types.FunctionType:
                    code = value.__code__
                    if (code.co_qualname == row["qualname"] and code.co_firstlineno == row["first_line"]
                            and fingerprint(code) == row["code_sha256"]):
                        bound.add(key)
        finally:
            self.in_audit = False
        return bound

    def finish(self):
        if self.done:
            return
        bound = self.bound_keys()
        super().finish()
        path = self.output / f"{self.identity}.done.json"
        row = read_json(path)
        row["cold_code"] = {
            "loaded": sorted(self.loaded), "bound": sorted(bound), "entered": sorted(self.entered),
            "rejected": {key: sorted(value) for key, value in self.rejected.items()},
            "observers": sorted(self.code_observers),
            "consumer_sites": self.consumer_sites,
        }
        write_json(path, row)


def child_install():
    if os.environ.get("BR_DISPATCH_CONFIG"):
        ColdObserver(read_json(os.environ["BR_DISPATCH_CONFIG"]))


def collect(args):
    repo, observations, output = args.repo.resolve(), args.observations.resolve(), args.output.resolve()
    coverage_path, positions_path = args.coverage.resolve(), args.positions.resolve()
    metadata, coverage, manifest = map(read_json, (args.checkout_metadata, coverage_path, args.manifest))
    tests = manifest["tests"] if isinstance(manifest, dict) else manifest
    tests = [{"number": row["number"], "id": row["id"]} for row in tests]
    if (metadata["base_commit"] != BASE_COMMIT or coverage["base_commit"] != BASE_COMMIT
            or tests != coverage["tests"] or coverage["context"]["complete"] is not True):
        raise ValueError("baseline, manifest or coverage mismatch")
    if observations.exists() and any(observations.iterdir()):
        raise ValueError("observation directory must be fresh")
    observations.mkdir(parents=True, exist_ok=True)
    for path, digest in metadata["files"].items():
        if hashlib.sha256((repo / path).read_bytes()).hexdigest() != digest:
            raise ValueError("baseline source mismatch: " + path)
    candidates = candidate_codes(repo, coverage)
    config = {"repo": str(repo), "observations": str(observations), "modules": {},
              "process_hooks": True, "method": POLICY, "candidates": candidates}
    typing = [row["number"] for row in tests if row["id"] ==
              "tests/test_typing.py::TestTyping::test_no_redundant_expression_metadata"]
    if len(typing) != 1:
        raise ValueError("unknown typing observer identity")
    config["typing_observer"] = typing[0]
    config_path = observations / "config.json"
    write_json(config_path, config)
    os.environ.update(BR_DISPATCH_CONFIG=str(config_path), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                      PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0")
    os.chdir(repo)
    sys.path.insert(0, str(repo))
    observer = ColdObserver(config)
    import pytest
    from _pytest.subtests import SubtestReport
    by_id = {row["id"]: row["number"] for row in tests}
    outcomes, extra, collection_errors, collected = {}, {}, [], []
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

    with tempfile.TemporaryDirectory(prefix="cold-code-child-") as temporary:
        (Path(temporary) / "sitecustomize.py").write_text(
            "from tools.build_cold_code import child_install\nchild_install()\n", "utf-8")
        os.environ["PYTHONPATH"] = os.pathsep.join([
            temporary, str(PROJECT), str(repo), os.environ.get("PYTHONPATH", "")])
        exit_code = int(pytest.main(["-q", "-p", "no:cacheprovider", "tests"], plugins=[Plugin()]))
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
    loaded, bound, entered, observers = set(), set(), set(), set()
    rejected, consumer_sites = {}, {}
    for start in starts:
        pids.add(str(read_json(start)["pid"]))
        done = start.with_name(start.name.replace(".start.json", ".done.json"))
        if not done.exists():
            unfinished.append(start.name)
            continue
        row = read_json(done)
        children.update(row.get("children", {}))
        process_errors.extend(row.get("process_errors", ()))
        facts = row["cold_code"]
        loaded.update(facts["loaded"])
        bound.update(facts["bound"])
        entered.update(facts["entered"])
        observers.update(facts["observers"])
        for site, count in facts["consumer_sites"].items():
            consumer_sites[site] = consumer_sites.get(site, 0) + count
        for key, reasons in facts["rejected"].items():
            rejected.setdefault(key, set()).update(reasons)
    unobserved = sorted(set(children) - pids)
    expected = {row["number"] for row in tests}
    unknown = (loaded | bound | entered | set(rejected)) - set(candidates)
    complete = not (exit_code or failed or missing or unknown_tests or collection_errors or unfinished
                    or process_errors or unobserved or subtests["failed"] or unknown or observers - expected)
    providers = {}
    for key in sorted((loaded & bound) - entered - set(rejected)):
        row = candidates[key]
        record = providers.setdefault(row["path"], {
            "source_sha256": coverage["paths"][row["path"]]["sha256"], "cold": []})
        record["cold"].append({"qualname": row["qualname"], "first_line": row["first_line"]})
    context = {"complete": complete, "passed": passed, "skipped": skipped, "failed": failed,
               "missing": missing, "unknown_tests": unknown_tests, "ignored_skipped_tests": ignored_skipped,
               "collection_errors": collection_errors, "unfinished": unfinished, "process_errors": process_errors,
               "unobserved_children": unobserved, "unknown_candidates": sorted(unknown), "processes": len(starts),
               "pytest_exit_code": exit_code, "subtests": dict(subtests), "candidates": len(candidates),
               "loaded": len(loaded), "bound": len(bound), "entered": len(entered),
               "rejected": {key: sorted(value) for key, value in rejected.items()},
               "consumer_sites": consumer_sites,
               "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "python": sys.version, "platform": sys.platform, "pytest": pytest.__version__}
    artifact = {"version": 1, "policy": POLICY, "base_commit": BASE_COMMIT,
                "coverage_sha256": hashlib.sha256(coverage_path.read_bytes()).hexdigest(),
                "positions_sha256": positions_digest(read_json(positions_path)), "tests": tests,
                "complete": complete, "providers": providers, "observers": sorted(observers),
                "source_observers": [config["typing_observer"]],
                "untraced": sorted(set(coverage["context"]["untraced_tests"]) | set(skipped)), "context": context}
    write_json(output, artifact)
    print(json.dumps({"complete": complete, "processes": len(starts), "passed": len(passed),
                      "candidates": len(candidates), "cold": sum(len(row["cold"]) for row in providers.values()),
                      "loaded": len(loaded), "bound": len(bound), "entered": len(entered),
                      "rejected": len(rejected), "observers": sorted(observers)}))
    return 0 if complete else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "manifest", "checkout-metadata", "coverage", "positions", "observations", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    return collect(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
