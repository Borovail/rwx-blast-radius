#!/usr/bin/env python3
"""Build source/position observer dependencies from pinned sources and coverage.

No patches or outcome labels are read. Every mapped Python source is checked
against its normalized base hash before the static observer scan is intersected
with measured test and shared-startup line contexts. ``shared`` means at least
one observer executed outside an attributable test, so narrowing must fall back.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from impact import BASE_COMMIT
from trivia import observer_lines


def build_index(repo: str | Path, coverage_path: str | Path) -> dict:
    """Return an index bound to the exact coverage file; reject stale sources."""
    repo = Path(repo).resolve()
    coverage_path = Path(coverage_path)
    raw_bytes = coverage_path.read_bytes()
    payload = gzip.decompress(raw_bytes) if coverage_path.suffix == ".gz" else raw_bytes
    coverage = json.loads(payload)
    if (coverage.get("version") != 1 or coverage.get("base_commit") != BASE_COMMIT
            or coverage.get("context", {}).get("complete") is not True
            or coverage.get("context", {}).get("startup_untraced") is not False):
        raise ValueError("coverage is not a complete pinned baseline")
    tests = {entry["number"]: entry["id"] for entry in coverage["tests"]}
    if (len(tests) != len(coverage["tests"])
            or any(type(number) is not int or number < 0 or not isinstance(test_id, str)
                   for number, test_id in tests.items())
            or len(set(tests.values())) != len(tests)):
        raise ValueError("invalid coverage test identities")
    selected, sites = set(), []
    shared = False
    for name, record in sorted(coverage["paths"].items()):
        if not name.endswith(".py"):
            continue
        path = PurePosixPath(name)
        source_path = (repo / name).resolve()
        if (path.is_absolute() or path.as_posix() != name or "\\" in name or ":" in name
                or any(part in ("", ".", "..") for part in name.split("/"))
                or not source_path.is_relative_to(repo)):
            raise ValueError(f"source path escapes baseline: {name}")
        source = source_path.read_bytes().decode("utf-8").replace("\r\n", "\n")
        if hashlib.sha256(source.encode("utf-8")).hexdigest() != record["sha256"]:
            raise ValueError(f"baseline source hash mismatch: {name}")
        line_count = source.count("\n") + bool(source and not source.endswith("\n"))
        if record["line_count"] != line_count:
            raise ValueError(f"baseline source line count mismatch: {name}")
        startup = set(record["startup"])
        if any(type(line) is not int or not 1 <= line <= line_count for line in startup):
            raise ValueError(f"invalid startup lines: {name}")
        for line in sorted(observer_lines(source)):
            numbers = record["lines"].get(str(line), [])
            if any(type(number) is not int or number not in tests for number in numbers):
                raise ValueError(f"unknown observer test context: {name}:{line}")
            ids = sorted({tests[number] for number in numbers})
            is_shared = line in startup
            selected.update(numbers)
            shared |= is_shared
            sites.append({"file": name, "line": line, "tests": ids, "shared": is_shared})
    return {
        "version": 1,
        "coverage_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "tests": sorted(selected),
        "shared": shared,
        "sites": sites,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--coverage", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    value = build_index(args.repo, args.coverage)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({"observer_tests": len(value["tests"]), "sites": len(value["sites"]),
                      "shared": value["shared"],
                      "shared_sites": [site for site in value["sites"] if site["shared"]]},
                     sort_keys=True))


if __name__ == "__main__":
    main()
