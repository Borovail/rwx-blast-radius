"""Baseline-observed dependencies of declarative class configuration.

The artifact is produced by executing the unmodified pinned source through an
attribute-observing import loader. Candidate source is parsed, never imported.
"""

from __future__ import annotations

import ast
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

BASE_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def key_for(path: str, scope: str, name: str) -> str:
    return f"{path}|{scope}|{name}"


def declarations(tree: ast.Module, path: str) -> dict[str, ast.Assign | ast.AnnAssign]:
    """Only unconditional, uniquely named direct class assignments are eligible."""
    result: dict[str, ast.Assign | ast.AnnAssign] = {}
    duplicate: set[str] = set()

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if not isinstance(node, ast.ClassDef):
                continue
            scope = f"{prefix}.{node.name}" if prefix else node.name
            for child in node.body:
                target = None
                if isinstance(child, ast.Assign) and len(child.targets) == 1:
                    target = child.targets[0]
                elif isinstance(child, ast.AnnAssign) and child.value is not None:
                    target = child.target
                if isinstance(target, ast.Name):
                    key = key_for(path, scope, target.id)
                    if key in result:
                        duplicate.add(key)
                    result[key] = child
            visit(node.body, scope)

    visit(tree.body, "")
    for key in duplicate:
        result.pop(key, None)
    return result


def _dump(tree: ast.AST) -> str:
    return ast.dump(tree, include_attributes=False)


def _required_flag_change(old: ast.AST, new: ast.AST) -> bool:
    """Recognize value-only edits to an ordered schema of named arguments."""
    if not isinstance(old, ast.Dict) or not isinstance(new, ast.Dict):
        return False
    keys = []
    for node in (old, new):
        if not all(isinstance(key, ast.Constant) and type(key.value) is str for key in node.keys):
            return False
        if not all(isinstance(value, ast.Constant) and type(value.value) is bool for value in node.values):
            return False
        keys.append([key.value for key in node.keys])
    return keys[0] == keys[1] and len(set(keys[0])) == len(keys[0])


def _safe_delta(old: ast.AST, new: ast.AST) -> bool:
    """Same-shaped literal changes inside an otherwise identical RHS.

    Calls and deferred function bodies must be identical. A new executable
    expression or renamed reference requires the regular conservative policy.
    """
    if _dump(old) == _dump(new):
        return True
    if type(old) is not type(new):
        return False
    if not isinstance(old, (ast.Constant, ast.List, ast.Tuple, ast.Set, ast.Dict, ast.UnaryOp)):
        return False
    if isinstance(old, ast.Constant):
        return type(old.value) is type(new.value)
    if isinstance(old, ast.Dict):
        # A changed key or container shape can invalidate import-time table
        # construction before any measured downstream reader executes.
        if len(old.keys) != len(new.keys):
            return False
        if any((left is None) != (right is None)
               or (left is not None and _dump(left) != _dump(right))
               for left, right in zip(old.keys, new.keys)):
            return False
    for (name, left), (_, right) in zip(ast.iter_fields(old), ast.iter_fields(new)):
        if isinstance(left, ast.AST):
            if not isinstance(right, ast.AST) or not _safe_delta(left, right):
                return False
        elif isinstance(left, list):
            if not isinstance(right, list) or len(left) != len(right):
                return False
            for a, b in zip(left, right):
                if isinstance(a, ast.AST):
                    if not isinstance(b, ast.AST) or not _safe_delta(a, b):
                        return False
                elif a != b:
                    return False
        elif left != right:
            return False
    return True


class DispatchSelector:
    """Return observed tests for supported declaration edits, otherwise None."""

    def __init__(self, artifact: Mapping[str, Any], tests: Any = None) -> None:
        self.artifact = artifact
        self._schema: Mapping[str, Any] | None = None
        self.ready = False
        self._cache: dict[str, tuple[str, dict[str, ast.AST], str]] = {}
        try:
            rows = tests["tests"] if isinstance(tests, Mapping) else tests
            expected = ({row["number"]: row["id"] for row in rows} if rows is not None
                        else {row["number"]: row["id"] for row in artifact["tests"]})
            mapped = {row["number"]: row["id"] for row in artifact["tests"]}
            self.numbers = set(expected)
            self.ready = (artifact["version"] == 1 and artifact["base_commit"] == BASE_COMMIT
                          and artifact["context"]["complete"] is True and expected == mapped)
        except (KeyError, TypeError, ValueError):
            self.numbers = set()

    @classmethod
    def from_file(cls, path: str | Path, tests: Any = None, *, schema_path: str | Path | None = None) -> "DispatchSelector":
        try:
            path = Path(path)
            with (gzip.open if path.suffix == ".gz" else open)(path, "rt", encoding="utf-8") as f:
                selector = cls(json.load(f), tests)
        except (OSError, ValueError, TypeError):
            return cls({}, tests)
        if schema_path is not None and selector.ready:
            schema = cls.from_file(schema_path, selector.artifact["tests"])
            if schema.ready and schema.artifact["context"].get("method") == "expression-schema-value-reads-v1":
                selector._schema = schema.artifact
        return selector

    def select(self, path: str, old_source: str, new_source: str) -> set[int] | None:
        if not self.ready:
            return None
        try:
            raw = self.artifact["paths"][path]
            old_source = old_source.replace("\r\n", "\n")
            if hashlib.sha256(old_source.encode()).hexdigest() != raw["sha256"]:
                return None
            if path not in self._cache:
                old = ast.parse(old_source)
                old_statements = declarations(old, path)
                old_values = {key: statement.value for key, statement in old_statements.items()}
                for statement in old_statements.values():
                    statement.value = ast.Constant(value="__dispatch_value__")
                self._cache[path] = (old_source, old_values, _dump(old))
            cached_source, old_values, old_shell = self._cache[path]
            if cached_source != old_source:
                return None
            new = ast.parse(new_source)
            compile(new, path, "exec", dont_inherit=True)
            new_decls = declarations(new, path)
            if set(old_values) != set(new_decls):
                return None
            changed = {}
            for key, value in old_values.items():
                other = new_decls[key]
                if _dump(value) != _dump(other.value):
                    # Python can consume special methods and slots without a
                    # traceable attribute read.
                    name = key.rsplit("|", 1)[-1]
                    if name.startswith("__") and name.endswith("__"):
                        return None
                    if not _safe_delta(value, other.value):
                        return None
                    changed[key] = other.value
                other.value = ast.Constant(value="__dispatch_value__")
            if not changed or old_shell != _dump(new):
                return None
            selected: set[int] = set()
            for key, replacement in changed.items():
                dependency = self.artifact["attributes"][key]
                if dependency["blocked"]:
                    if (self._schema is None or not key.endswith("|arg_types")
                            or self._schema["paths"][path]["sha256"] != raw["sha256"]
                            or not _required_flag_change(old_values[key], replacement)):
                        return None
                    dependency = self._schema["attributes"][key]
                    if dependency["blocked"] is not False:
                        return None
                values = dependency["tests"]
                if any(type(number) is not int or number not in self.numbers for number in values):
                    return None
                selected.update(values)
            return selected
        except (KeyError, TypeError, ValueError, SyntaxError, RecursionError, MemoryError):
            return None
