#!/usr/bin/env python3
"""Measure exact baseline function origins inspected by direct __code__ sites.

Run in the pinned Linux image with locked pytest dependencies. Every observer
test comes from positions.json and receives a fresh process. Source hashes,
passing setup/call/teardown, static site support, actual site observations and
absence of child processes are required. No candidate patches or labels enter.
An incomplete index deliberately disables narrowing at runtime.
"""

from __future__ import annotations

import argparse
import ast
import dis
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import FunctionType, MethodType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trivia import _POSITION_ATTRS, _SOURCE_APIS, observer_lines
from observer_targets import positions_digest

BASE_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
PROCESS_EVENTS = {"subprocess.Popen", "os.fork", "os.posix_spawn", "_winapi.CreateProcess"}


def supported_code_site(source: str, line: int) -> bool:
    """Only direct function.__code__ reads are supported; no frame/source APIs."""
    tree = ast.parse(source)
    aliases = set(_SOURCE_APIS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"inspect", "traceback", "linecache", "dis", "sys"}:
            aliases.update(alias.asname or alias.name for alias in node.names if alias.name in _SOURCE_APIS)
    nodes = [node for node in ast.walk(tree) if getattr(node, "lineno", None) == line]
    attributes = [node for node in nodes if isinstance(node, ast.Attribute)
                  and node.attr in _POSITION_ATTRS | _SOURCE_APIS]
    return (bool(attributes) and all(node.attr == "__code__" and isinstance(node.ctx, ast.Load) for node in attributes)
            and not any(isinstance(node, ast.Name) and node.id in aliases for node in nodes)
            and not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id in {"getattr", "setattr", "delattr"} for node in nodes))


def observe(repo: Path, test_id: str, sites: set[tuple[str, int]]) -> dict:
    """Run inside a fresh interpreter, recording audited actual function objects."""
    import pytest

    repo = repo.resolve()
    state = {"collected": [], "outcomes": [], "observations": [], "errors": [], "opcode_reads": 0}
    active = False
    busy = False
    tracking = False
    instruction_cache = {}
    site_filenames = {os.path.normcase(str((repo / file).resolve())): file for file, _ in sites}

    def trace(frame, event, arg):
        if event == "call":
            if os.path.normcase(frame.f_code.co_filename) not in site_filenames:
                return None
            instruction_cache.setdefault(frame.f_code, {
                instruction.offset for instruction in dis.get_instructions(frame.f_code)
                if instruction.opname == "LOAD_ATTR" and instruction.argval == "__code__"
            })
            frame.f_trace_opcodes = True
            return trace
        frame.f_trace_opcodes = True
        if event == "opcode" and frame.f_lasti in instruction_cache.get(frame.f_code, ()):
            name = site_filenames[os.path.normcase(frame.f_code.co_filename)]
            if (name, frame.f_lineno) in sites:
                state["opcode_reads"] += 1
        return trace

    def audit(event, args):
        nonlocal busy
        if busy:
            return
        busy = True
        try:
            if event in PROCESS_EVENTS:
                state["errors"].append("child process")
            if tracking and event in {"sys.settrace", "sys.setprofile", "sys.addaudithook"}:
                state["errors"].append("tracing changed")
            if event != "object.__getattr__" or len(args) != 2 or args[1] != "__code__":
                return
            frame = sys._getframe(1)
            caller = site_filenames.get(os.path.normcase(frame.f_code.co_filename))
            if caller is None:
                return
            site = caller, frame.f_lineno
            if site not in sites:
                return
            if not active:
                state["errors"].append("observer outside test protocol")
                return
            function = args[0]
            if isinstance(function, MethodType):
                function = function.__func__
            if type(function) is not FunctionType:
                state["errors"].append("unsupported observed object")
                return
            try:
                target = Path(function.__code__.co_filename).resolve().relative_to(repo).as_posix()
            except ValueError:
                state["errors"].append("target outside baseline")
                return
            state["observations"].append([caller, site[1], target])
        finally:
            busy = False

    class Plugin:
        def pytest_collection_modifyitems(self, items):
            state["collected"] = [item.nodeid for item in items]

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item, nextitem):
            nonlocal active
            active = item.nodeid == test_id
            try:
                return (yield)
            finally:
                active = False

        def pytest_runtest_logreport(self, report):
            state["outcomes"].append([report.nodeid, report.when, report.outcome])

    sys.addaudithook(audit)
    # Request opcode dispatch before installation and renew it on trace events.
    sys._getframe().f_trace_opcodes = True
    sys.settrace(trace)
    tracking = True
    try:
        result = pytest.main([test_id, "-q", "-p", "no:cacheprovider"], plugins=[Plugin()])
        if sys.gettrace() is not trace:
            state["errors"].append("trace hook replaced")
    finally:
        tracking = False
        sys.settrace(None)
    state["returncode"] = int(result)
    return state


def validate_observation(report: dict, test_id: str, sites: set[tuple[str, int]], paths: set[str]) -> set[str]:
    if (report["returncode"] != 0 or report["collected"] != [test_id] or report["errors"]
            or type(report["opcode_reads"]) is not int
            or report["opcode_reads"] != len(report["observations"])
            or set(map(tuple, report["outcomes"])) != {(test_id, phase, "passed") for phase in ("setup", "call", "teardown")}):
        raise ValueError("isolated baseline test did not pass all guards")
    observed = set()
    targets = set()
    for file, line, target in report["observations"]:
        if (file, line) not in sites or target not in paths:
            raise ValueError("unknown site or target")
        observed.add((file, line))
        targets.add(target)
    if observed != sites or not targets:
        raise ValueError("observer site was not audited")
    return targets


def build_index(repo: Path, coverage_path: Path, positions_path: Path, observations: Path) -> dict:
    raw = coverage_path.read_bytes()
    coverage = json.loads(gzip.decompress(raw) if coverage_path.suffix == ".gz" else raw)
    pos_raw = positions_path.read_bytes()
    positions = json.loads(pos_raw)
    if (coverage["version"] != 1 or coverage["base_commit"] != BASE_COMMIT
            or coverage["context"]["complete"] is not True
            or coverage["context"]["startup_untraced"] is not False
            or positions["version"] != 1 or positions["shared"] is not False
            or positions["coverage_sha256"] != hashlib.sha256(raw).hexdigest()):
        raise ValueError("baseline maps are incomplete or inconsistent")
    sources = {}
    for name, record in coverage["paths"].items():
        target = (repo / name).resolve()
        if not target.is_relative_to(repo.resolve()):
            raise ValueError("unsafe baseline path")
        data = target.read_bytes()
        try:
            normalized = data.decode("utf-8").replace("\r\n", "\n").encode()
        except UnicodeDecodeError:
            normalized = data
        if hashlib.sha256(normalized).hexdigest() != record["sha256"]:
            raise ValueError("baseline source mismatch: " + name)
        if name.endswith(".py"):
            sources[name] = normalized.decode()
    # Independently reproduce the static source sites, so a stale/incomplete
    # positions list cannot silently omit a known observer.
    actual_sites = {(name, line) for name, source in sources.items() for line in observer_lines(source)}
    if actual_sites != {(site["file"], site["line"]) for site in positions["sites"]}:
        raise ValueError("observer site inventory mismatch")
    tests = {row["number"]: row["id"] for row in coverage["tests"]}
    rows, failures = [], []
    observations.mkdir(parents=True, exist_ok=True)
    for number in positions["tests"]:
        test_id = tests[number]
        sites = {(site["file"], site["line"]) for site in positions["sites"] if test_id in site["tests"]}
        try:
            if not sites or any(not supported_code_site(sources[file], line) for file, line in sites):
                raise ValueError("unsupported observer site")
            request = {"repo": str(repo.resolve()), "test_id": test_id, "sites": sorted(sites)}
            request_path = observations / (str(number) + ".request.json")
            report_path = observations / (str(number) + ".report.json")
            request_path.write_text(json.dumps(request))
            env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(repo.resolve()), os.environ.get("PYTHONPATH", "")]),
                       PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0",
                       PYTHON_CPU_COUNT="2", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
            with (observations / (str(number) + ".log")).open("w") as log:
                process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--observe", str(request_path),
                                          "--output", str(report_path)], cwd=repo, env=env,
                                         stdout=log, stderr=subprocess.STDOUT, timeout=180)
            if process.returncode:
                raise ValueError("observer child failed")
            report = json.loads(report_path.read_text())
            targets = validate_observation(report, test_id, sites, set(coverage["paths"]))
            rows.append({"number": number, "id": test_id, "targets": sorted(targets), "guards_passed": True,
                         "sites": [{"file": file, "line": line} for file, line in sorted(sites)],
                         "observations": len(report["observations"]), "opcode_reads": report["opcode_reads"]})
        except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
            failures.append({"number": number, "error": str(error)})
    for name, record in coverage["paths"].items():
        data = (repo / name).read_bytes()
        try:
            normalized = data.decode("utf-8").replace("\r\n", "\n").encode()
        except UnicodeDecodeError:
            normalized = data
        if hashlib.sha256(normalized).hexdigest() != record["sha256"]:
            failures.append({"number": None, "error": "baseline source changed during observation: " + name})
    return {"version": 1, "base_commit": BASE_COMMIT, "coverage_sha256": hashlib.sha256(raw).hexdigest(),
            "positions_sha256": positions_digest(positions), "complete": not failures,
            "tests": rows, "failures": failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--coverage", type=Path)
    parser.add_argument("--positions", type=Path)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--observe", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.observe:
        request = json.loads(args.observe.read_text())
        value = observe(Path(request["repo"]), request["test_id"], set(map(tuple, request["sites"])))
    else:
        value = build_index(args.repo, args.coverage, args.positions or args.coverage.with_name("positions.json"),
                            args.observations or args.output.with_suffix(".observations"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    print(json.dumps({"complete": value.get("complete"), "tests": len(value.get("tests", [])),
                      "failures": value.get("failures", [])}))


if __name__ == "__main__":
    main()
