"""Validate exact baseline startup test-source read facts."""
from __future__ import annotations
import ast
from collections import defaultdict
import gzip
import hashlib
import json
from pathlib import Path
from typing import Mapping
from observer_targets import positions_digest

METHOD = "exact-pytest-rewrite-test-source-reads-v1"
BASE_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"

def _dump(node):
    return ast.dump(node, include_attributes=False)


def _methods(tree):
    """Index uniquely named methods of uniquely named top-level classes."""
    classes = {}
    duplicate_classes = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if node.name in classes:
                duplicate_classes.add(node.name)
            classes[node.name] = node
    result = {}
    for name, cls in classes.items():
        if name in duplicate_classes:
            continue
        methods = {}
        duplicate_methods = set()
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in methods:
                    duplicate_methods.add(node.name)
                methods[node.name] = node
        result.update(
            ((name, method), node)
            for method, node in methods.items()
            if method not in duplicate_methods
        )
    return result


class TestMethodReads:
    def __init__(self, artifact, tests, coverage_sha256, positions_sha256):
        tests = tests["tests"] if isinstance(tests, Mapping) else tests
        self.ready = False
        self.paths = {}
        self.tests = tests
        self._baseline_cache = {}
        try:
            self.numbers = {row["number"] for row in tests}
            self.numbers_by_id = {row["id"]: row["number"] for row in tests}
            if len(self.numbers) != len(tests) or len(self.numbers_by_id) != len(tests):
                return
            owners_by_path = defaultdict(list)
            for row in tests:
                owners_by_path[row["id"].split("::")[0]].append(row["number"])
            context = artifact["context"]
            known, unknown = context["known_only"], context["unknown_startup"]
            if (
                artifact["version"] != 1
                or artifact["base_commit"] != BASE_COMMIT
                or artifact["tests"] != [
                    {"number": row["number"], "id": row["id"]} for row in tests
                ]
                or context["complete"] is not True
                or context["method"] != METHOD
                or context["coverage_sha256"] != coverage_sha256
                or context["positions_sha256"] != positions_sha256
                or not isinstance(known, list)
                or len(set(known)) != len(known)
                or not isinstance(unknown, list)
                or len(set(unknown)) != len(unknown)
                or set(known) & set(unknown)
                or context["unfinished"]
                or context["process_errors"]
                or context["unobserved_children"]
            ):
                return
            self.skipped = set(context["skipped"])
            if any(type(n) is not int or n not in self.numbers for n in self.skipped):
                return
            for path in known:
                row = artifact["paths"][path]
                if (
                    not path.startswith("tests/")
                    or not path.endswith(".py")
                    or type(row["known_startup_count"]) is not int
                    or row["known_startup_count"] <= 0
                    or row["unknown_startup"]
                    or row["owners"] != sorted(owners_by_path[path])
                    or any(type(n) is not int or n not in self.numbers for n in row["readers"])
                ):
                    return
                self.paths[path] = row
            self.ready = True
        except (KeyError, TypeError, ValueError, AttributeError):
            self.paths = {}

    @classmethod
    def from_files(cls, artifact_path, coverage_path, tests):
        try:
            coverage_path = Path(coverage_path)
            positions_path = coverage_path.with_name("positions.json")
            positions = json.loads(positions_path.read_bytes())
            if positions["shared"] is not False:
                return None
            artifact_path = Path(artifact_path)
            raw = artifact_path.read_bytes()
            if artifact_path.suffix == ".gz":
                raw = gzip.decompress(raw)
            artifact = json.loads(raw)
            return cls(
                artifact, tests,
                hashlib.sha256(coverage_path.read_bytes()).hexdigest(),
                positions_digest(positions),
            )
        except (EOFError, OSError, ValueError, TypeError, KeyError):
            return None

    def _baseline(self, path, old_source):
        if path not in self._baseline_cache:
            digest = hashlib.sha256(old_source.encode()).hexdigest()
            if digest != self.paths[path]["sha256"]:
                return None
            tree = ast.parse(old_source)
            self._baseline_cache[path] = old_source, tree, _methods(tree)
        cached_source, tree, methods = self._baseline_cache[path]
        if cached_source != old_source:
            return None
        return tree, methods
