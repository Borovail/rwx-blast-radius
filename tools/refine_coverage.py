#!/usr/bin/env python3
"""Refine expensive subprocess supplements using isolated *base-only* tests.

Run this tool inside the pinned Python/Linux image with the exact locked
dependencies. The existing coverage map is never modified; --output is a new
asset. Every target gets a fresh pytest process and a complete Python process
tree attestation. Missing child startup/exit, changed tracing, non-Python
children, timeout, test failures, and changed base files retain the supplement.

Example (mount trace_worker together with this script)::

    python tools/refine_coverage.py --repo /base --manifest /tests.json \
        --coverage /observed.json.gz --observations /out/isolated \
        --test-number 1106 --test-number 1107 --output /out/refined.json.gz

The numbers above are supplied by the caller, never built into the tool. Omit
--test-number to choose the highest-duration untraced tests in the manifest.
Use --observe-only before the full baseline map is ready, and --merge-only to
validate/reuse those observations later. No patches or answer labels are read.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import signal
import subprocess
import sys
import time
import uuid


BASE_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
_RUNNER = '''
import json, os, pytest
class Plugin:
    def __init__(self): self.collected, self.outcomes = [], []
    def pytest_collection_modifyitems(self, items):
        self.collected = [item.nodeid for item in items]
    def pytest_runtest_logreport(self, report):
        self.outcomes.append([report.nodeid, report.when, report.outcome])
p = Plugin()
result = pytest.main([os.environ["BLAST_TEST_ID"], "-q", "-p", "no:cacheprovider"], plugins=[p])
with open(os.environ["BLAST_PYTEST_REPORT"], "w") as stream:
    json.dump({"exit_code": int(result), "collected": p.collected, "outcomes": p.outcomes}, stream)
raise SystemExit(result)
'''


def read_json(path):
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(gzip.compress(raw, mtime=0) if path.suffix == ".gz" else raw + b"\n")


def fingerprint(repo, coverage):
    hashes = {}
    for name, record in coverage["paths"].items():
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != name:
            raise ValueError("invalid base map path")
        entry = repo / name
        target = entry.resolve()
        if not target.is_relative_to(repo):
            raise ValueError("base map path escapes checkout")
        raw = target.read_bytes()
        try:
            content = raw.decode("utf-8").replace("\r\n", "\n").encode()
        except UnicodeDecodeError:
            content = raw
        digest = hashlib.sha256(content).hexdigest()
        if (digest != record["sha256"] and entry.is_symlink()
                and target.relative_to(repo).as_posix() in coverage["paths"]):
            # A Git checkout with core.symlinks=false stores the link text in a
            # regular file. Accept its exact Git blob when the real referent is
            # also fingerprinted by this map.
            digest = hashlib.sha256(os.fsencode(os.readlink(entry))).hexdigest()
        if digest != record["sha256"]:
            raise ValueError("base source mismatch: " + name)
        hashes[name] = digest
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def observe(args, test, stamp):
    import resource
    directory = args.observations / str(test["number"])
    # Each attempt has a unique directory; old reports are never silently reused.
    run = directory / uuid.uuid4().hex
    run.mkdir(parents=True)
    token = run.name
    env = os.environ.copy()
    hooks = Path(__file__).resolve().parent / "trace_worker"
    env.update({
        "PYTHONPATH": os.pathsep.join([str(hooks), str(args.repo), env.get("PYTHONPATH", "")]),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHON_CPU_COUNT": str(args.workers), "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1", "SKIP_INTEGRATION": "0",
        "BLAST_TRACE_DIR": str(run), "BLAST_TRACE_REPO": str(args.repo),
        "BLAST_TRACE_TOKEN": token, "BLAST_TEST_ID": test["id"],
        "BLAST_PYTEST_REPORT": str(run / "pytest.json"),
    })
    started = time.monotonic()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    with open(run / "pytest.log", "wb") as log:
        process = subprocess.Popen([sys.executable, "-c", _RUNNER], cwd=args.repo,
                                   env=env, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        timed_out = False
        try:
            returncode = process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            returncode = process.wait()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    metadata = {"version": 1, "base_commit": BASE_COMMIT, "test": test,
                "base_fingerprint": stamp, "pid": process.pid, "token": token,
                "returncode": returncode, "timed_out": timed_out,
                "wall_seconds": round(time.monotonic() - started, 3),
                "child_cpu_seconds": round(after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime, 3),
                "python": sys.version, "platform": sys.platform,
                "workers": args.workers, "repo": str(args.repo) + os.sep}
    write_json(run / "run.json", metadata)
    write_json(directory / "latest.json", {"run": token})
    # multiprocessing's resource tracker may exit just after its parent.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        starts = {path.stem.split(".")[0] for path in run.glob("*.start.json")}
        done = {path.stem.split(".")[0] for path in run.glob("*.done.json")}
        if starts and starts == done:
            break
        time.sleep(0.05)
    return run


def validate(run, test, stamp, coverage):
    metadata = read_json(run / "run.json")
    if metadata["base_commit"] != BASE_COMMIT or metadata["base_fingerprint"] != stamp:
        raise ValueError("observation uses different base source")
    if metadata["test"]["number"] != test["number"] or metadata["test"]["id"] != test["id"]:
        raise ValueError("observation belongs to another test")
    if metadata["platform"] != "linux" or metadata["timed_out"] or metadata["returncode"] != 0:
        raise ValueError("test did not pass cleanly in Linux")
    report = read_json(run / "pytest.json")
    if report["exit_code"] != 0 or report["collected"] != [test["id"]]:
        raise ValueError("test did not collect/pass exactly once")
    phases = {phase for nodeid, phase, outcome in report["outcomes"]
              if nodeid == test["id"] and outcome == "passed"}
    if phases != {"setup", "call", "teardown"} or any(
        nodeid != test["id"] or outcome != "passed" for nodeid, phase, outcome in report["outcomes"]
    ):
        raise ValueError("incomplete/failed/skipped/subfailed pytest protocol")
    starts = {int(path.name.split(".")[0]): read_json(path) for path in run.glob("*.start.json")}
    done = {int(path.name.split(".")[0]): read_json(path) for path in run.glob("*.done.json")}
    expected_root = metadata.get("repo", starts.get(metadata["pid"], {}).get("repo"))
    if not isinstance(expected_root, str) or not expected_root.endswith("/"):
        raise ValueError("missing source-root attestation")
    expected = {metadata["pid"]}
    checked = set()
    merged_lines, readers = {}, set()
    while expected - checked:
        pid = min(expected - checked)
        start, record = starts.get(pid), done.get(pid)
        if start is None or record is None:
            raise ValueError("missing startup/clean exit for child PID " + str(pid))
        if any(value.get("token") != metadata["token"] or value.get("pid") != pid or value.get("repo") != expected_root
               or value.get("version") != 1 for value in (start, record)):
            raise ValueError("invalid child attestation")
        if not record["complete"] or not record["trace_installed"] or record["errors"]:
            raise ValueError("child trace incomplete: " + repr(record.get("errors")))
        expected.update(int(child) for child in record["children"])
        for name, lines in record["lines"].items():
            if name not in coverage["paths"]:
                # Generated files under the repo are not acceptable baseline inputs.
                if name.endswith(".py") and "__pycache__/" not in name:
                    raise ValueError("unknown executed source: " + name)
                continue
            count = coverage["paths"][name]["line_count"]
            if any(type(line) is not int or not 1 <= line <= count for line in lines):
                raise ValueError("invalid observed line")
            merged_lines.setdefault(name, set()).update(lines)
        readers.update(name for name in record["readers"] if name in coverage["paths"])
        checked.add(pid)
    if set(starts) != checked or set(done) != checked:
        raise ValueError("unaccounted process in trace directory")
    # A supplement must actually have produced an observed child process. The
    # trace is not proof that an unexercised process path would be covered.
    if len(checked) < 2:
        raise ValueError("no child process observed")
    return merged_lines, readers, len(checked), metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test-number", type=int, action="append")
    parser.add_argument("--max-tests", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--observe-only", action="store_true")
    parser.add_argument("--merge-only", action="store_true")
    args = parser.parse_args()
    if sys.platform != "linux" or sys.version_info[:2] != (3, 13):
        parser.error("use the pinned Linux Python 3.13 image")
    args.repo, args.observations = args.repo.resolve(), args.observations.resolve()
    if args.output.resolve() == args.coverage.resolve():
        parser.error("--output must be separate from the input baseline")
    if args.output.resolve().is_relative_to(args.repo) or args.observations.is_relative_to(args.repo):
        parser.error("observations and output must stay outside the pinned checkout")
    coverage = read_json(args.coverage)
    manifest = read_json(args.manifest)["tests"]
    tests = {row["number"]: row for row in manifest}
    if coverage["version"] != 1 or coverage["base_commit"] != BASE_COMMIT:
        raise ValueError("unsupported base coverage")
    if {row["number"]: row["id"] for row in coverage["tests"]} != {number: row["id"] for number, row in tests.items()}:
        raise ValueError("manifest/coverage mismatch")
    stamp = fingerprint(args.repo, coverage)
    supplements = set(coverage["context"]["untraced_tests"])
    numbers = sorted(set(args.test_number), key=lambda number: (-tests[number]["seconds"], number)) if args.test_number is not None else sorted(
        supplements, key=lambda number: (-tests[number]["seconds"], number)
    )[:args.max_tests]
    refined = copy.deepcopy(coverage)
    refined_ids = {entry["number"] for entry in coverage["context"].get("isolated_refinements", [])
                   if entry.get("verified", True) is True}
    results = []
    for number in numbers:
        test = tests[number]
        try:
            if args.merge_only:
                token = read_json(args.observations / str(number) / "latest.json")["run"]
                if not isinstance(token, str) or len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
                    raise ValueError("invalid observation directory")
                run = args.observations / str(number) / token
            else:
                run = observe(args, test, stamp)
            lines, readers, processes, metadata = validate(run, test, stamp, coverage)
            result = {"number": number, "id": test["id"], "verified": True,
                      "processes": processes, "wall_seconds": metadata["wall_seconds"],
                      "paths": len(lines), "readers": len(readers), "run": str(run)}
            if "child_cpu_seconds" in metadata:
                result["child_cpu_seconds"] = metadata["child_cpu_seconds"]
            if not args.observe_only:
                for name, observed in lines.items():
                    record = refined["paths"][name]
                    for line in observed:
                        record["lines"][str(line)] = sorted(set(record["lines"].get(str(line), [])) | {number})
                    for function in record.get("functions", []):
                        if any(function["body_start"] <= line <= function["end"] for line in observed):
                            function["tests"] = sorted(set(function["tests"]) | {number})
                for name in readers:
                    record = refined["paths"][name]
                    record["readers"] = sorted(set(record["readers"]) | {number})
                supplements.discard(number)
                refined_ids.add(number)
        except (OSError, ValueError, KeyError, TypeError) as error:
            result = {"number": number, "id": test["id"], "verified": False, "reason": str(error)}
        results.append(result)
        print(json.dumps(result, sort_keys=True), flush=True)
    if not args.observe_only:
        if fingerprint(args.repo, coverage) != stamp:
            raise ValueError("base source changed during observations")
        refined["context"]["untraced_tests"] = sorted(supplements)
        # Runtime assets contain no UUIDs, host paths, clocks, timing, or PID
        # values. Keep all such provenance in the separate local report.
        refined["context"]["isolated_refinements"] = [
            {"number": number, "id": tests[number]["id"]} for number in sorted(refined_ids)
        ]
        refined["context"]["isolated_refinement_provenance"] = {
            "method": "isolated-cpython-monitoring-process-tree-v1",
            "base_commit": BASE_COMMIT, "base_source_fingerprint": stamp,
        }
        write_json(args.output, refined)
    write_json(args.output.with_suffix(args.output.suffix + ".report.json"), {
        "base_fingerprint": stamp, "base_map_sha256": hashlib.sha256(args.coverage.read_bytes()).hexdigest(),
        "observe_only": args.observe_only, "results": results,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
