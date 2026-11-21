"""Validate optional baseline code-observer origin attribution, failing closed."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Mapping


def positions_digest(value: Mapping) -> str:
    """Bind parsed position data independently of JSON layout or host newlines."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class ObserverTargets:
    def __init__(self, targets: dict[str, set[int]]) -> None:
        self._targets = targets

    def select(self, path: str) -> set[int]:
        """Return code observers whose measured baseline target came from path."""
        return set(self._targets.get(path, ()))

    @classmethod
    def from_files(cls, coverage_path: str | Path, coverage: Mapping | None = None) -> ObserverTargets | None:
        """Load a complete index adjacent to coverage; None retains global observers."""
        coverage_path = Path(coverage_path)
        try:
            raw = coverage_path.read_bytes()
            if coverage is None:
                coverage = json.loads(gzip.decompress(raw) if coverage_path.suffix == ".gz" else raw)
            positions_raw = coverage_path.with_name("positions.json").read_bytes()
            positions = json.loads(positions_raw)
            index = json.loads(coverage_path.with_name("observer_targets.json").read_bytes())
            if (index["version"] != 1 or index["complete"] is not True
                    or index["failures"]
                    or index["base_commit"] != coverage["base_commit"]
                    or index["coverage_sha256"] != hashlib.sha256(raw).hexdigest()
                    or index["positions_sha256"] != positions_digest(positions)
                    or positions["shared"] is not False):
                return None
            tests = {row["number"]: row["id"] for row in coverage["tests"]}
            expected = set(positions["tests"])
            seen: set[int] = set()
            targets: dict[str, set[int]] = {}
            for row in index["tests"]:
                number = row["number"]
                if (type(number) is not int or number in seen or number not in expected
                        or tests.get(number) != row["id"] or not row["targets"]
                        or row["guards_passed"] is not True
                        or type(row["observations"]) is not int or row["observations"] <= 0
                        or type(row["opcode_reads"]) is not int or row["opcode_reads"] != row["observations"]):
                    return None
                expected_sites = {(site["file"], site["line"]) for site in positions["sites"] if row["id"] in site["tests"]}
                actual_sites = [(site["file"], site["line"]) for site in row["sites"]]
                if not expected_sites or set(actual_sites) != expected_sites or len(actual_sites) != len(expected_sites):
                    return None
                seen.add(number)
                paths = row["targets"]
                if not isinstance(paths, list) or len(set(paths)) != len(paths):
                    return None
                for name in paths:
                    path = PurePosixPath(name)
                    if (not isinstance(name, str) or path.is_absolute() or "\\" in name
                            or ":" in name or any(p in ("", ".", "..") for p in name.split("/"))
                            or path.as_posix() != name or name not in coverage["paths"]):
                        return None
                    targets.setdefault(name, set()).add(number)
            if seen != expected:
                return None
            return cls(targets)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None
