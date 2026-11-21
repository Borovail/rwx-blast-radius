#!/usr/bin/env python3
"""Collect factual comparison operands from isolated pinned-base tests.

No candidate patches or outcome labels are accepted. All binary comparisons in
imported base modules are instrumented. Every Python descendant must attest
startup, clean exit and its witness report before that test supplies evidence.
The optional test subset is selected by manifest duration, never patch content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from refine_coverage import BASE_COMMIT, _RUNNER, fingerprint, read_json, write_json


def observe(args, test, stamp):
    run = args.observations / str(test["number"]) / uuid.uuid4().hex
    run.mkdir(parents=True)
    trace = Path(__file__).resolve().parent / "trace_worker" / "sitecustomize.py"
    with tempfile.TemporaryDirectory(prefix="blast-body-hooks-") as hooks:
        Path(hooks, "sitecustomize.py").write_text(
            "from pathlib import Path\n"
            f"exec(compile(Path({str(trace)!r}).read_text(), {str(trace)!r}, 'exec'), "
            "{'__name__': 'blast_body_trace'})\n"
            "from build_body_worker import install\ninstall()\n")
        env = os.environ.copy()
        env.update({
            "PYTHONPATH": os.pathsep.join([hooks, str(ROOT / "tools"), str(ROOT),
                                          str(args.repo), env.get("PYTHONPATH", "")]),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHON_CPU_COUNT": str(args.workers), "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1", "SKIP_INTEGRATION": "0",
            "BLAST_TRACE_DIR": str(run), "BLAST_TRACE_REPO": str(args.repo),
            "BLAST_TRACE_TOKEN": run.name, "BLAST_TEST_ID": test["id"],
            "BLAST_PYTEST_REPORT": str(run / "pytest.json"), "BLAST_BODY_DIR": str(run),
            "BLAST_BODY_OPERATORS": args.operators,
        })
        started = time.monotonic()
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
        # A resource tracker can finish shortly after its owning process.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            starts = {p.name.split(".")[0] for p in run.glob("*.start.json")}
            done = {p.name.split(".")[0] for p in run.glob("*.done.json")}
            bodies = {p.name.split(".")[0] for p in run.glob("*.body.json")}
            if starts and starts == done == bodies:
                break
            time.sleep(0.05)
    write_json(run / "run.json", {"version": 1, "base_commit": BASE_COMMIT,
               "base_fingerprint": stamp, "test": test, "pid": process.pid,
               "token": run.name, "returncode": returncode, "timed_out": timed_out,
               "wall_seconds": round(time.monotonic() - started, 3),
               "repo": str(args.repo) + os.sep, "platform": sys.platform,
               "python": sys.version})
    write_json(run.parent / "latest.json", {"run": run.name})
    return run


def validate(run, test, stamp, coverage):
    metadata = read_json(run / "run.json")
    if (metadata["base_commit"] != BASE_COMMIT or metadata["base_fingerprint"] != stamp
            or metadata["test"]["number"] != test["number"]
            or metadata["test"]["id"] != test["id"]
            or metadata["platform"] != "linux" or metadata["timed_out"]
            or metadata["returncode"] != 0):
        raise ValueError("isolated test lacks a clean matching base run")
    report = read_json(run / "pytest.json")
    phases = {phase for nodeid, phase, outcome in report["outcomes"]
              if nodeid == test["id"] and outcome == "passed"}
    if (report["exit_code"] != 0 or report["collected"] != [test["id"]]
            or phases != {"setup", "call", "teardown"}
            or any(nodeid != test["id"] or outcome != "passed"
                   for nodeid, phase, outcome in report["outcomes"])):
        raise ValueError("incomplete/failed/skipped pytest protocol")
    starts = {int(p.name.split(".")[0]): read_json(p) for p in run.glob("*.start.json")}
    done = {int(p.name.split(".")[0]): read_json(p) for p in run.glob("*.done.json")}
    bodies = {int(p.name.split(".")[0]): read_json(p) for p in run.glob("*.body.json")}
    expected, checked, files, unknown = {metadata["pid"]}, set(), {}, set()
    while expected - checked:
        pid = min(expected - checked)
        start, record, body = starts.get(pid), done.get(pid), bodies.get(pid)
        if start is None or record is None or body is None:
            raise ValueError("missing process startup/exit/witness report")
        for value in (start, record, body):
            if value["version"] != 1 or value["pid"] != pid or value["token"] != metadata["token"]:
                raise ValueError("process attestation mismatch")
        if (start["repo"] != metadata["repo"] or record["repo"] != metadata["repo"]
                or not record["complete"] or not record["trace_installed"] or record["errors"]
                or not body["complete"] or body["errors"]):
            raise ValueError("incomplete process tracing/instrumentation")
        expected.update(int(child) for child in record["children"])
        for path, lines in record["lines"].items():
            if path not in coverage["paths"]:
                if path.endswith(".py") and "__pycache__/" not in path:
                    raise ValueError("unknown executed base source")
                continue
            count = coverage["paths"][path]["line_count"]
            if any(type(line) is not int or not 1 <= line <= count for line in lines):
                raise ValueError("invalid observed base line")
        unknown.update(body["unknown"])
        for path, item in body["files"].items():
            if path not in coverage["paths"] or item["sha256"] != coverage["paths"][path]["sha256"]:
                raise ValueError("witness source differs from pinned base")
            output = files.setdefault(path, {"sha256": item["sha256"], "sites": {}})
            for site, value in item["sites"].items():
                if not isinstance(value["observations"], list):
                    raise ValueError("invalid operand observations")
                previous = output["sites"].setdefault(site, value.copy())
                if previous["ast"] != value["ast"] or previous["operator"] != value["operator"]:
                    raise ValueError("comparison site changed within observation")
                if previous is not value:
                    combined = {json.dumps(item, sort_keys=True) for item in previous["observations"]}
                    combined.update(json.dumps(item, sort_keys=True) for item in value["observations"])
                    previous["observations"] = ([None] if len(combined) > 64 else
                                                [json.loads(item) for item in sorted(combined)])
        checked.add(pid)
    if set(starts) != checked or set(done) != checked or set(bodies) != checked:
        raise ValueError("unaccounted process in witness directory")
    for path in unknown:
        files.pop(path, None)
    return files, len(checked), metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test-number", type=int, action="append")
    parser.add_argument("--max-tests", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--operators", default="Is,IsNot",
                        help="comma-separated original operators to observe")
    parser.add_argument("--merge-only", action="store_true")
    args = parser.parse_args()
    if not set(args.operators.split(",")).issubset({"Is", "IsNot"}):
        parser.error("witness omission currently supports original Is/IsNot only")
    if sys.platform != "linux" or sys.version_info[:2] != (3, 13):
        parser.error("use pinned Linux Python 3.13")
    args.repo, args.observations = args.repo.resolve(), args.observations.resolve()
    if args.output.resolve().is_relative_to(args.repo) or args.observations.is_relative_to(args.repo):
        parser.error("output and observations must stay outside the pinned base")
    coverage = read_json(args.coverage)
    manifest = read_json(args.manifest)["tests"]
    tests = {row["number"]: row for row in manifest}
    if (coverage["version"] != 1 or coverage["base_commit"] != BASE_COMMIT
            or {r["number"]: r["id"] for r in coverage["tests"]}
            != {n: r["id"] for n, r in tests.items()}):
        raise ValueError("unsupported base coverage/manifest")
    stamp = fingerprint(args.repo, coverage)
    numbers = (set(args.test_number) if args.test_number is not None else
               {row["number"] for row in sorted(manifest, key=lambda r: (-r["seconds"], r["number"]))[:args.max_tests]})
    files, results = {}, []
    for number in sorted(numbers, key=lambda n: (-tests[n]["seconds"], n)):
        test = tests[number]
        try:
            if args.merge_only:
                token = read_json(args.observations / str(number) / "latest.json")["run"]
                if (not isinstance(token, str) or len(token) != 32
                        or any(ch not in "0123456789abcdef" for ch in token)):
                    raise ValueError("invalid observation token")
                run = args.observations / str(number) / token
            else:
                run = observe(args, test, stamp)
            observed, processes, metadata = validate(run, test, stamp, coverage)
            for path, record in observed.items():
                output = files.setdefault(path, {"sha256": record["sha256"], "sites": {}})
                for site, value in record["sites"].items():
                    if value["operator"] not in args.operators.split(","):
                        continue
                    target = output["sites"].setdefault(site, {"ast": value["ast"], "tests": {}})
                    target["tests"][str(number)] = value["observations"]
            result = {"number": number, "verified": True, "processes": processes,
                      "wall_seconds": metadata["wall_seconds"], "paths": len(observed)}
        except (OSError, KeyError, ValueError, TypeError) as error:
            result = {"number": number, "verified": False, "reason": str(error)}
        results.append(result)
        print(json.dumps(result, sort_keys=True), flush=True)
    if fingerprint(args.repo, coverage) != stamp:
        raise ValueError("base source changed during observations")
    files = {path: value for path, value in files.items() if value["sites"]}
    write_json(args.output, {"version": 1, "base_commit": BASE_COMMIT, "complete": True,
               "coverage_sha256": hashlib.sha256(args.coverage.read_bytes()).hexdigest(),
               "tests": coverage["tests"], "files": files,
               "method": "isolated-base-identity-operands-v1",
               "base_fingerprint": stamp,
               "observed_tests": sorted(row["number"] for row in results if row["verified"])})
    write_json(args.output.with_suffix(args.output.suffix + ".report.json"), {"results": results})


if __name__ == "__main__":
    main()
