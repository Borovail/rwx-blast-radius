"""Shared body/trace process protocol; extracted without changing V3 validation."""

import json
from refine_coverage import read_json


def validate_processes(run, metadata, coverage):
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
