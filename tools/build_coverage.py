#!/usr/bin/env python3
r"""Build baseline-only, test-numbered coverage for the pinned SQLGlot tree.

Run in a disposable, dependency-equipped Python environment, for example::

    python tools/build_coverage.py --repo /opt/sqlglot \
        --tests-manifest /data/tests.json --output maps/coverage.json.gz \
        --sources-output maps/base_sources.json.gz

No candidate patches or patch outcome labels are inputs. The supplied checkout
must be clean and at --base-commit. Coverage includes collection/imports, complete
pytest setup/call/teardown protocols, shared fixture contexts, and audited file
reads. Tests that launch subprocesses are recorded as unconditional supplements
because parent-process tracing cannot observe their children. Unknown execution,
collection errors, failures, and missing tests make ``context.complete`` false.
Explicitly reported skips are recorded; extra collected tests are accepted only
when skipped. Consumers must use the complete manifest for incomplete maps.

Schema version 1 uses benchmark *numbers*, never collection-order indices:
``paths[path].lines[str(line)] -> [number, ...]``; ``startup`` contains lines
without a uniquely attributable test (including shared fixture setup).
``readers`` and ``startup_read`` describe audited reads of tracked files.
``functions`` records AST bounds and the union of body-line coverage. The separate
source artifact is a flat path-to-text dictionary, with normalized LF newlines.
Both gzip outputs are deterministic for the same observations (mtime = 0).
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import gzip
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from typing import Any


BASE_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
PROCESS_EVENTS = {"subprocess.Popen", "os.fork", "os.posix_spawn", "_winapi.CreateProcess"}


def _json_read(path: Path) -> Any:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            return json.load(stream)
    return json.loads(path.read_text(encoding="utf-8"))


def _json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if path.suffix == ".gz":
        path.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
    else:
        path.write_bytes(raw + b"\n")


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _manifest(path: Path) -> list[dict[str, Any]]:
    value = _json_read(path)
    tests = value["tests"] if isinstance(value, dict) else value
    result = [{"number": row["number"], "id": row["id"]} for row in tests]
    if any(type(row["number"]) is not int or not isinstance(row["id"], str) for row in result):
        raise ValueError("manifest numbers must be integers and IDs must be strings")
    if len({row["number"] for row in result}) != len(result):
        raise ValueError("duplicate test numbers in manifest")
    if len({row["id"] for row in result}) != len(result):
        raise ValueError("duplicate test IDs in manifest")
    return sorted(result, key=lambda row: row["number"])


class Observation:
    """Mutable observation state shared by the hooks and the Python audit hook."""

    def __init__(self, repo: Path, tests: list[dict[str, Any]], cov: Any) -> None:
        self.repo = repo
        self.root = os.path.normcase(str(repo)) + os.sep
        self.tests = {row["id"]: row["number"] for row in tests}
        self.numbers = set(self.tests.values())
        self.cov = cov
        self.context = ""
        self.active: int | None = None
        self.collected: list[str] = []
        self.executed: set[int] = set()
        self.outcomes: dict[int, dict[str, str]] = {}
        self.extra_outcomes: dict[str, dict[str, str]] = {}
        self.skip_reasons: dict[str, str] = {}
        self.readers: dict[str, set[int]] = {}
        self.startup_reads: set[str] = set()
        self.untraced_tests: set[int] = set()
        self.startup_untraced = False
        self.process_events: list[dict[str, Any]] = []
        self.unknown: set[str] = set()
        self.collection_errors: list[str] = []
        self.audit_enabled = True

    def switch(self, context: str, active: int | None) -> None:
        self.context, self.active = context, active
        self.cov.switch_context(context)

    def relative(self, filename: str | bytes | os.PathLike[str]) -> str | None:
        path = os.path.normcase(os.path.abspath(os.fsdecode(filename)))
        if not path.startswith(self.root):
            return None
        # Preserve repository spelling in output, including on Windows.
        return os.path.relpath(os.path.abspath(os.fsdecode(filename)), self.repo).replace(os.sep, "/")

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        if not self.audit_enabled:
            return
        if event in PROCESS_EVENTS:
            self.process_events.append({"event": event, "test": self.active,
                                        "args": [str(value)[:512] for value in args[:2]]})
            if self.active is None:
                self.startup_untraced = True
            else:
                self.untraced_tests.add(self.active)
        elif event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            mode, flags = args[1], args[2]
            if isinstance(mode, str) and not ("r" in mode or "+" in mode):
                return
            if mode is None and (flags & os.O_WRONLY) and not (flags & os.O_RDWR):
                return
            # Normal interpreter imports read source/bytecode but do not inspect
            # its literal contents as test data. Import-time *execution* is
            # already traced. Preserve explicit inspect/linecache/source reads.
            caller = sys._getframe(1)
            if caller.f_code.co_filename == "<frozen importlib._bootstrap_external>":
                return
            path = self.relative(args[0])
            if path is not None:
                if self.active is None:
                    self.startup_reads.add(path)
                else:
                    self.readers.setdefault(path, set()).add(self.active)


def _plugin(observation: Observation, pytest: Any) -> Any:
    class Plugin:
        def pytest_collection_modifyitems(self, items: list[Any]) -> None:
            observation.collected = [item.nodeid for item in items]

        def pytest_collectreport(self, report: Any) -> None:
            if report.failed:
                observation.collection_errors.append(report.nodeid)

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item: Any, nextitem: Any) -> Any:
            number = observation.tests.get(item.nodeid)
            if number is None:
                observation.switch("unknown", None)
            else:
                observation.executed.add(number)
                observation.switch(f"test:{number}", number)
            try:
                return (yield)
            finally:
                observation.switch("", None)

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_fixture_setup(self, fixturedef: Any, request: Any) -> Any:
            if fixturedef.scope == "function":
                return (yield)
            previous = observation.context, observation.active
            # Shared setup can affect every dependent test. It is deliberately
            # unassigned, so a changed line/read causes a conservative fallback.
            observation.switch(f"shared-fixture:{fixturedef.argname}", None)
            try:
                return (yield)
            finally:
                observation.switch(*previous)

        def pytest_runtest_logreport(self, report: Any) -> None:
            number = observation.tests.get(report.nodeid)
            target = (observation.outcomes.setdefault(number, {}) if number is not None else
                      observation.extra_outcomes.setdefault(report.nodeid, {}))
            # Pytest 9 may report failed subtests before a final passing call.
            # A later report must never erase an observed failure.
            if target.get(report.when) != "failed":
                target[report.when] = report.outcome
            if report.skipped:
                observation.skip_reasons[report.nodeid] = str(report.longrepr)

    return Plugin()


def _functions(source: str, lines: dict[str, list[int]]) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    result: list[dict[str, Any]] = []

    def visit(node: ast.AST, parents: tuple[str, ...]) -> None:
        name = getattr(node, "name", None)
        scope = parents + (name,) if isinstance(name, str) else parents
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([node.lineno] + [decorator.lineno for decorator in node.decorator_list])
            body_start = node.body[0].lineno
            end = node.end_lineno or node.lineno
            numbers = {number for line in range(body_start, end + 1) for number in lines.get(str(line), [])}
            result.append({"name": ".".join(scope), "start": start, "end": end,
                           "body_start": body_start, "tests": sorted(numbers)})
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, ())
    return result


def _process_sites(sources: dict[str, str], paths: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Supplement audit hooks for CPython's unaudited spawnv_passfds path.

    A covered process constructor is sufficient evidence to conservatively add
    its tests even when it does not launch a child (for example an empty pool).
    False positives only add work. Pool construction does not run user code:
    actual dispatch sites are also collected, including methods on shared pools.
    Shared startup dispatch makes the map incomplete. This is static analysis of
    the *base*, not patch labels.
    """
    names = {"ProcessPoolExecutor", "Pool", "Process", "Popen", "fork", "fork_exec",
             "posix_spawn", "spawnv_passfds", "start_new_thread"}
    inert_constructors = {"ProcessPoolExecutor", "Process"}
    dispatches = {"submit", "map", "starmap", "apply", "apply_async", "map_async",
                  "starmap_async", "imap", "imap_unordered", "start"}
    result = []
    for name, source in sources.items():
        if not name.endswith(".py"):
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            continue
        aliases = set(names)
        inert_aliases = set(inert_constructors)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                aliases.update(alias.asname for alias in node.names
                               if alias.name in names and alias.asname)
                inert_aliases.update(alias.asname for alias in node.names
                                     if alias.name in inert_constructors and alias.asname)
        pool_receivers: set[str] = set()

        def called_name(node: ast.AST) -> str | None:
            return getattr(node, "id", None) or getattr(node, "attr", None)

        for node in ast.walk(tree):
            value = getattr(node, "value", None)
            if isinstance(value, ast.Call) and called_name(value.func) in aliases:
                targets = getattr(node, "targets", [getattr(node, "target", None)])
                pool_receivers.update(called_name(target) for target in targets if target is not None)
            if isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if (isinstance(item.context_expr, ast.Call)
                            and called_name(item.context_expr.func) in aliases
                            and item.optional_vars is not None):
                        pool_receivers.add(called_name(item.optional_vars))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            called = called_name(function)
            dispatch = (isinstance(function, ast.Attribute) and called in dispatches
                        and called_name(function.value) in pool_receivers)
            if called not in aliases and not dispatch:
                continue
            row = paths[name]
            tests = row["lines"].get(str(node.lineno), [])
            observed_startup = node.lineno in row["startup"]
            startup = observed_startup and called not in inert_aliases
            if tests or observed_startup:
                result.append({"file": name, "line": node.lineno, "call": called,
                               "tests": tests, "startup": startup,
                               "construction_only": called in inert_aliases})
    return sorted(result, key=lambda row: (row["file"], row["line"]))


def _complete(context: dict[str, Any], tests: list[dict[str, Any]]) -> bool:
    accounted = set(context["passed"]) | set(context["skipped"])
    return (accounted == {row["number"] for row in tests}
            and context["executed"] == len(tests)
            and not any(context[key] for key in (
                "pytest_exit_code", "missing_tests", "unknown_tests", "failed", "incomplete",
                "collection_errors", "unknown_files", "unknown_contexts", "startup_untraced")))


def _refresh_derived(artifact: dict[str, Any], tests: list[dict[str, Any]], head: str,
                     sources: dict[str, str], expected_paths: dict[str, dict[str, Any]]) -> None:
    """Recompute static-only metadata without rerunning or changing observations."""
    if artifact.get("version") != 1 or artifact.get("base_commit") != head or artifact.get("tests") != tests:
        raise ValueError("observation artifact has a different schema, baseline, or test manifest")
    paths = artifact["paths"]
    if set(paths) != set(expected_paths):
        raise ValueError("observation artifact's path set differs from the verified checkout")
    for name, expected in expected_paths.items():
        if any(paths[name].get(key) != expected[key] for key in ("sha256", "line_count")):
            raise ValueError(f"observation source hash/line count mismatch for {name}")
        # CPython 3.13/coverage can report a synthetic line zero for an empty
        # module's entry code. Preserve that fact without inventing source lines.
        if 0 in paths[name]["startup"] or "0" in paths[name]["lines"]:
            paths[name]["module_entry"] = True
            paths[name]["startup"] = [line for line in paths[name]["startup"] if line != 0]
            paths[name]["lines"].pop("0", None)
    context = artifact["context"]
    numbers = {row["number"] for row in tests}
    untraced = set(context["skipped"])
    startup = False
    for event in context["process_events"]:
        if event["event"] not in PROCESS_EVENTS or event["test"] not in numbers | {None}:
            raise ValueError("invalid observed process event")
        if event["test"] is None:
            startup = True
        else:
            untraced.add(event["test"])
    sites = _process_sites(sources, paths)
    for site in sites:
        untraced.update(site["tests"])
        startup |= site["startup"]
    context["untraced_tests"] = sorted(untraced)
    context["startup_untraced"] = startup
    context["process_sites"] = sites
    context["derived_analysis"] = "process-dispatch-v2"
    context["complete"] = _complete(context, tests)


def build(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    if args.checkout_metadata:
        metadata = _json_read(args.checkout_metadata.resolve())
        head = metadata["base_commit"]
        tracked = sorted(metadata["files"])
        for name in tracked:
            if hashlib.sha256((repo / name).read_bytes()).hexdigest() != metadata["files"][name]:
                raise ValueError(f"checkout metadata hash mismatch for {name}")
    else:
        head = _git(repo, "rev-parse", "HEAD")
        if _git(repo, "status", "--porcelain", "--untracked-files=no"):
            raise ValueError("tracked checkout modifications are not allowed during baseline generation")
        tracked = [path for path in _git(repo, "ls-files", "-z").split("\0")
                   if path and (repo / path).is_file()]
    if head != args.base_commit:
        raise ValueError(f"expected clean base {args.base_commit}, found {head}")
    if args.write_checkout_metadata:
        _json_write(args.write_checkout_metadata.resolve(), {"base_commit": head, "files": {
            name: hashlib.sha256((repo / name).read_bytes()).hexdigest() for name in tracked}})
        return 0
    if args.tests_manifest is None:
        raise ValueError("--tests-manifest is required when generating coverage")
    tests = _manifest(args.tests_manifest.resolve())
    sources: dict[str, str] = {}
    paths: dict[str, dict[str, Any]] = {}
    for name in tracked:
        path = repo / name
        if not path.is_file():
            continue
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8").replace("\r\n", "\n")
        except UnicodeDecodeError:
            text = None
        # Source snapshots include all tracked UTF-8 text, covering fixtures and
        # configuration hunk validation as well as Python AST regions.
        if text is not None and "\0" not in text:
            sources[name] = text
            fingerprint = hashlib.sha256(text.encode()).hexdigest()
            line_count = len(text.splitlines())
        else:
            fingerprint = hashlib.sha256(raw).hexdigest()
            line_count = 0
        paths[name] = {"sha256": fingerprint, "line_count": line_count,
                       "lines": {}, "startup": [], "readers": [], "startup_read": False}

    if args.refresh_derived:
        artifact = _json_read(args.refresh_derived.resolve())
        _refresh_derived(artifact, tests, head, sources, paths)
        _json_write(args.output.resolve(), artifact)
        if args.sources_output is not None:
            _json_write(args.sources_output.resolve(), sources)
        print(json.dumps({"coverage": str(args.output), "complete": artifact["context"]["complete"],
                          "untraced_tests": artifact["context"]["untraced_tests"],
                          "refreshed_static_analysis_only": True}, sort_keys=True))
        return 0 if artifact["context"]["complete"] else 2

    import coverage

    old_cwd, old_path = Path.cwd(), list(sys.path)
    os.chdir(repo)
    sys.path.insert(0, str(repo))
    # Avoid unrelated globally installed pytest plugins and native SQLGlot
    # extensions. No behavior flags are changed within SQLGlot or its tests.
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["SKIP_INTEGRATION"] = "0"
    with tempfile.TemporaryDirectory(prefix="sqlglot-coverage-") as tmp:
        includes = [str(repo / package / "*") for package in args.package]
        cov = coverage.Coverage(data_file=str(Path(tmp) / ".coverage"), config_file=False,
                                include=includes, branch=False)
        observation = Observation(repo, tests, cov)
        sys.addaudithook(observation.audit)
        cov.start()
        try:
            import pytest

            # A single serial run preserves ordinary suite behavior. xdist would
            # lose dynamic context unless each worker were collected separately.
            exit_code = int(pytest.main(["-q", "-p", "no:cacheprovider", *args.pytest_arg, "tests"],
                                        plugins=[_plugin(observation, pytest)]))
        finally:
            observation.audit_enabled = False
            cov.stop()
            os.chdir(old_cwd)
            sys.path[:] = old_path
        data = cov.get_data()
        unknown_contexts: set[str] = set()
        unknown_files: set[str] = set()
        for filename in sorted(data.measured_files()):
            name = observation.relative(filename)
            if name not in paths:
                unknown_files.add(name or filename)
                continue
            line_map = paths[name]["lines"]
            startup: list[int] = []
            for line, contexts in sorted(data.contexts_by_lineno(filename).items()):
                if line == 0:
                    paths[name]["module_entry"] = True
                    continue
                numbers: set[int] = set()
                for context in contexts:
                    if context.startswith("test:"):
                        number = int(context[5:])
                        if number not in observation.numbers:
                            unknown_contexts.add(context)
                        else:
                            numbers.add(number)
                    else:
                        startup.append(line)
                        if context and not context.startswith("shared-fixture:"):
                            unknown_contexts.add(context)
                if numbers:
                    line_map[str(line)] = sorted(numbers)
            paths[name]["startup"] = sorted(set(startup))

    for name, row in paths.items():
        row["readers"] = sorted(observation.readers.get(name, set()))
        row["startup_read"] = name in observation.startup_reads
        if name.endswith(".py") and name in sources:
            row["functions"] = _functions(sources[name], row["lines"])
    process_sites = _process_sites(sources, paths)
    for site in process_sites:
        observation.untraced_tests.update(site["tests"])
        observation.startup_untraced |= site["startup"]
    expected = {row["id"] for row in tests}
    collected = set(observation.collected)
    missing = sorted(expected - collected)
    extra = sorted(collected - expected)
    ignored_skipped = sorted(nodeid for nodeid in extra
                             if "skipped" in observation.extra_outcomes.get(nodeid, {}).values()
                             and "failed" not in observation.extra_outcomes.get(nodeid, {}).values())
    extra = sorted(set(extra) - set(ignored_skipped))
    passed, failed, skipped, incomplete = [], [], [], []
    for row in tests:
        number = row["number"]
        outcomes = observation.outcomes.get(number, {})
        if "failed" in outcomes.values():
            failed.append(number)
        elif "skipped" in outcomes.values():
            skipped.append(number)
        elif outcomes.get("call") == "passed" and outcomes.get("teardown") == "passed":
            passed.append(number)
        else:
            incomplete.append(number)
    # A skipped numbered test has no complete body trace, so always retain it.
    observation.untraced_tests.update(skipped)
    versions = {}
    for package in ("coverage", "pytest", "numpy", "pandas", "duckdb", "python-dateutil", "pytz"):
        with contextlib.suppress(importlib.metadata.PackageNotFoundError):
            versions[package] = importlib.metadata.version(package)
    context = {"collected": len(observation.collected),
               "executed": len(observation.executed), "passed": passed, "failed": failed,
               "skipped": skipped, "incomplete": incomplete, "missing_tests": missing,
               "unknown_tests": sorted(set(extra) | observation.unknown),
               "ignored_skipped_tests": ignored_skipped,
               "skip_reasons": observation.skip_reasons,
               "collection_errors": observation.collection_errors,
               "unknown_files": sorted(unknown_files), "unknown_contexts": sorted(unknown_contexts),
               "untraced_tests": sorted(observation.untraced_tests),
               "startup_untraced": observation.startup_untraced, "pytest_exit_code": exit_code,
               "process_events": observation.process_events,
               "process_sites": process_sites,
               "python": platform.python_version(), "platform": platform.system(),
               "dependencies": versions, "skip_integration": "0",
               "environment": {name: os.environ.get(name) for name in (
                   "PYTHON_CPU_COUNT", "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS")},
               "source_scope": args.package,
               "policy": "Incomplete/unknown/startup execution requires all tests; always include untraced_tests."}
    complete = context["complete"] = _complete(context, tests)
    artifact = {"version": 1, "base_commit": head, "tests": tests, "paths": paths, "context": context}
    _json_write(args.output.resolve(), artifact)
    if args.sources_output is not None:
        _json_write(args.sources_output.resolve(), sources)
    print(json.dumps({"coverage": str(args.output), "sources": str(args.sources_output),
                      "complete": complete, "tests": len(tests), "collected": context["collected"],
                      "executed": context["executed"], "passed": len(passed), "failed": len(failed),
                      "skipped": len(skipped), "incomplete": len(incomplete),
                      "untraced_tests": context["untraced_tests"], "paths": len(paths),
                      "measured_paths": sum(bool(row["lines"] or row["startup"]) for row in paths.values())},
                     sort_keys=True))
    return 0 if complete else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--tests-manifest", type=Path)
    parser.add_argument("--output", type=Path, default=Path("maps/coverage.json.gz"))
    parser.add_argument("--sources-output", type=Path,
                        help="optional local source snapshot; omit when runtime reads the pinned checkout directly")
    parser.add_argument("--base-commit", default=BASE_COMMIT)
    parser.add_argument("--checkout-metadata", type=Path,
                        help="verified file hashes from --write-checkout-metadata; avoids needing git inside a container")
    parser.add_argument("--write-checkout-metadata", type=Path,
                        help="verify clean base, write tracked file hashes, then exit without running tests")
    parser.add_argument("--refresh-derived", type=Path,
                        help="verify an existing observation artifact and refresh only static process metadata")
    parser.add_argument("--package", action="append", default=None,
                        help="repository-relative coverage root; repeatable (default: sqlglot and tests)")
    parser.add_argument("--pytest-arg", action="append", default=[],
                        help="extra pytest argument; use --pytest-arg=-k for options")
    args = parser.parse_args()
    args.package = args.package or ["sqlglot", "tests"]
    try:
        return build(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"coverage build failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
