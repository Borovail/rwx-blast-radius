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
STARTUP_WITNESS_POLICY = "required-argument-witnesses-v2"
RUNTIME_WITNESS_POLICY = "exact-required-validator-always-present-v1"


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


def _changed_required_flags(old: ast.AST, new: ast.AST) -> set[str] | None:
    """Return changed argument names for a value-only edit to a literal schema."""
    if not isinstance(old, ast.Dict) or not isinstance(new, ast.Dict) or len(old.keys) != len(new.keys):
        return None
    seen: set[str] = set()
    changed: set[str] = set()
    for left, right, before, after in zip(old.keys, new.keys, old.values, new.values):
        if not (isinstance(left, ast.Constant) and type(left.value) is str
                and isinstance(right, ast.Constant) and left.value == right.value and left.value not in seen
                and isinstance(before, ast.Constant) and type(before.value) is bool
                and isinstance(after, ast.Constant) and type(after.value) is bool):
            return None
        seen.add(left.value)
        if before.value != after.value:
            changed.add(left.value)
    return changed


def _only_flag_tokens(old_source: str, new_source: str, old: ast.Dict, new: ast.Dict) -> bool:
    """A startup witness covers only the verified boolean tokens at their original locations."""
    # AST column offsets are byte offsets into UTF-8. Work backwards so that a
    # replacement on a line cannot shift another replacement on the same line.
    lines = old_source.encode("utf-8").split(b"\n")
    for before, after in reversed(list(zip(old.values, new.values))):
        if before.value is after.value:
            continue
        line = lines[before.lineno - 1]
        start, end = before.col_offset, before.end_col_offset
        if before.lineno != before.end_lineno or line[start:end] != str(before.value).encode("ascii"):
            return False
        lines[before.lineno - 1] = line[:start] + str(after.value).encode("ascii") + line[end:]
    return b"\n".join(lines) == new_source.encode("utf-8")


def _runtime_schema_tests(tests: set[int], groups: Any, flags: set[str]) -> set[int]:
    """Retain each test unless every changed flag has an unambiguous witness."""
    if not isinstance(groups, list):
        return tests
    seen: set[int] = set()
    removable: set[int] = set()
    for group in groups:
        if not isinstance(group, dict):
            return tests
        args, numbers = group.get("args"), group.get("tests")
        if (not isinstance(args, list) or not args or not all(type(arg) is str for arg in args)
                or len(set(args)) != len(args) or not isinstance(numbers, list) or not numbers
                or not all(type(number) is int and number in tests for number in numbers)):
            return tests
        members = set(numbers)
        if seen & members or len(members) != len(numbers):
            return tests
        seen.update(members)
        if flags.issubset(args):
            removable.update(members)
    return tests - removable


def _same_scalar_type(before: ast.AST, after: ast.AST) -> bool:
    """The baseline and candidate values must both be plain scalar literals."""
    try:
        original = ast.literal_eval(before)
        candidate = ast.literal_eval(after)
    except (ValueError, TypeError, SyntaxError):
        return False
    return type(original) in (bool, int, str) and type(original) is type(candidate)


def _only_changed_literal(before: ast.expr, after: ast.expr, old_source: str, new_source: str) -> bool:
    # Keep byte coordinates used by Python's AST, including non-ASCII source.
    if not (before.lineno == before.end_lineno == after.lineno == after.end_lineno):
        return False
    if before.col_offset != after.col_offset:
        return False
    lines = old_source.encode().split(b"\n")
    proposed = new_source.encode().split(b"\n")
    if len(lines) != len(proposed):
        return False
    index = before.lineno - 1
    line = lines[index]
    new_line = proposed[index]
    literal = new_line[after.col_offset:after.end_col_offset]
    lines[index] = line[:before.col_offset] + literal + line[before.end_col_offset:]
    return lines == proposed


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
        self._scalars: Mapping[str, Any] | None = None
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
    def from_file(cls, path: str | Path, tests: Any = None, *,
                  schema_path: str | Path | None = None, scalar_path: str | Path | None = None) -> "DispatchSelector":
        try:
            path = Path(path)
            with (gzip.open if path.suffix == ".gz" else open)(path, "rt", encoding="utf-8") as f:
                selector = cls(json.load(f), tests)
        except (OSError, ValueError, TypeError):
            return cls({}, tests)
        if schema_path is not None and selector.ready:
            schema = cls.from_file(schema_path, selector.artifact["tests"])
            context = schema.artifact["context"] if schema.ready else {}
            supported_policies = ("only-pure-required-set-and-schema-copies", STARTUP_WITNESS_POLICY)
            if (context.get("method") == "expression-schema-value-reads-v1"
                    and context.get("startup_policy") in supported_policies):
                selector._schema = schema.artifact
        if scalar_path is not None and selector.ready:
            scalars = cls.from_file(scalar_path, selector.artifact["tests"])
            context = scalars.artifact["context"] if scalars.ready else {}
            if (context.get("method") == "literal-config-value-readers-v1"
                    and context.get("namespace_policy") == "literal-class-namespace-reads-v1"
                    and context.get("eligibility_policy") == "audited-concrete-sqlglot-dialect-scalars-v1"):
                selector._scalars = scalars.artifact
        return selector

    def _scalar_readers(self, key: str, path: str, source_hash: str, original: set[int],
                        old: ast.expr, new: ast.expr, before: str, after: str) -> set[int]:
        if self._scalars is None or not _same_scalar_type(old, new):
            return original
        try:
            row = self._scalars["attributes"][key]
            tests = row["tests"]
            if (self._scalars["paths"][path]["sha256"] == source_hash
                    and row["eligible"] is True and row["blocked"] is False
                    and isinstance(tests, list) and all(type(n) is int and n in original for n in tests)
                    and _only_changed_literal(old, new, before, after)):
                return set(tests)
        except (KeyError, IndexError, TypeError, ValueError):
            pass
        return original

    def select(self, path: str, old_source: str, new_source: str, *, single_file: bool = True) -> set[int] | None:
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
                runtime_flags = None
                if dependency["blocked"]:
                    if (self._schema is None or not key.endswith("|arg_types")
                            or self._schema["paths"][path]["sha256"] != raw["sha256"]):
                        return None
                    flags = _changed_required_flags(old_values[key], replacement)
                    if not flags:
                        return None
                    dependency = self._schema["attributes"][key]
                    startup_blocked = dependency["blocked"] is not False
                    has_witnesses = self._schema["context"].get("runtime_policy") == RUNTIME_WITNESS_POLICY
                    isolated_flags = (single_file and len(changed) == 1 and (startup_blocked or has_witnesses)
                                      and _only_flag_tokens(old_source, new_source, old_values[key], replacement))
                    if startup_blocked:
                        # A second edit could change the values present during initialization.
                        if not (isolated_flags and dependency["blocked"] is True
                                and self._schema["context"]["startup_policy"] == STARTUP_WITNESS_POLICY):
                            return None
                        safe = dependency.get("startup_safe_args")
                        if (not isinstance(safe, list) or not all(isinstance(arg, str) for arg in safe)
                                or not flags.issubset(safe)):
                            return None
                    if isolated_flags and has_witnesses:
                        runtime_flags = flags
                values = dependency["tests"]
                if any(type(number) is not int or number not in self.numbers for number in values):
                    return None
                if runtime_flags:
                    values = _runtime_schema_tests(set(values), dependency.get("always_present"), runtime_flags)
                if (self._scalars is not None and single_file and len(changed) == 1
                        and dependency["blocked"] is False and key.rsplit("|", 1)[-1].isupper()):
                    values = self._scalar_readers(key, path, raw["sha256"], set(values),
                                                  old_values[key], replacement, old_source, new_source)
                selected.update(values)
            return selected
        except (KeyError, TypeError, ValueError, SyntaxError, RecursionError, MemoryError):
            return None
