"""Narrow function edits using base coverage and optional comparison witnesses.

Witnesses contain factual operand snapshots and original comparison results.
They contain no candidate patch data, and patched code is never executed.
Missing witnesses retain the ordinary line dependency.
"""

from __future__ import annotations

import ast
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


COMPARISONS = ("Eq", "NotEq", "Lt", "LtE", "Gt", "GtE", "Is", "IsNot")
BASE_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def _scalar(atom):
    kind, name, value = atom
    if kind not in ("scalar", "scalar_subclass"):
        raise ValueError("not a primitive scalar")
    if name == "none" and value is None:
        return None
    if name == "bool" and type(value) is bool:
        return value
    if name == "int" and type(value) is str and len(value) <= 128:
        return int(value)
    if name == "float" and type(value) is str and len(value) <= 32:
        return float.fromhex(value)
    if name == "str" and type(value) is str and len(value) <= 256:
        return value
    if name == "bytes" and type(value) is str and len(value) <= 256:
        return bytes.fromhex(value)
    raise ValueError("invalid primitive snapshot")


def observed_outcome(observation, operator):
    """Evaluate one actual operator using only recorded base operand facts."""
    try:
        left, right, identical, original_result = observation
        if type(identical) is not bool or type(original_result) is not bool:
            return None
        for atom in (left, right):
            if not isinstance(atom, (list, tuple)) or not atom:
                return None
            if atom[0] in ("identity", "unknown"):
                if len(atom) != 1:
                    return None
            else:
                _scalar(atom)
        if operator == "Is":
            return identical
        if operator == "IsNot":
            return not identical
        if operator in ("Eq", "NotEq") and left[0] != "unknown" and right[0] != "unknown":
            if "identity" in (left[0], right[0]):
                equal = identical
            else:
                equal = _scalar(left) == _scalar(right)
            return equal if operator == "Eq" else not equal
        if left[0] == right[0] == "scalar":
            a, b = _scalar(left), _scalar(right)
            if operator == "Lt":
                return a < b
            if operator == "LtE":
                return a <= b
            if operator == "Gt":
                return a > b
            if operator == "GtE":
                return a >= b
    except (IndexError, KeyError, TypeError, ValueError, OverflowError):
        pass
    return None


def comparison_site(node: ast.Compare) -> str:
    """Coordinates identify a base expression, never a candidate patch."""
    return ":".join(str(n) for n in (node.lineno, node.col_offset,
                                    node.end_lineno, node.end_col_offset))


def _comparison_shell(value: Any) -> Any:
    if isinstance(value, ast.cmpop):
        return ast.cmpop
    if isinstance(value, ast.AST):
        return type(value), tuple((name, _comparison_shell(child))
                                  for name, child in ast.iter_fields(value))
    if isinstance(value, list):
        return tuple(_comparison_shell(child) for child in value)
    return repr(value)


def comparison_changes(old_tree: ast.AST, new_tree: ast.AST) -> list[tuple[ast.Compare, str]] | None:
    """Accept changes confined to operators of existing binary comparisons."""
    if _comparison_shell(old_tree) != _comparison_shell(new_tree):
        return None
    before = [node for node in ast.walk(old_tree) if isinstance(node, ast.Compare)]
    after = [node for node in ast.walk(new_tree) if isinstance(node, ast.Compare)]
    changed = []
    for left, right in zip(before, after):
        if [type(op) for op in left.ops] == [type(op) for op in right.ops]:
            continue
        if len(left.ops) != 1 or type(right.ops[0]).__name__ not in COMPARISONS:
            return None
        changed.append((left, type(right.ops[0]).__name__))
    return changed or None


class BodyWitnesses:
    """Validated positive evidence for individual base tests and expressions."""

    def __init__(self, value: Mapping[str, Any], tests: Mapping[int, str],
                 coverage_sha256: str | None = None) -> None:
        self.files: Mapping[str, Any] = {}
        try:
            mapped = {row["number"]: row["id"] for row in value["tests"]}
            if (type(value["version"]) is not int or value["version"] != 1
                    or value["base_commit"] != BASE_COMMIT
                    or value["complete"] is not True or mapped != tests
                    or (coverage_sha256 is not None
                        and value["coverage_sha256"] != coverage_sha256)
                    or not isinstance(value["files"], Mapping)):
                return
            self.files = value["files"]
        except (KeyError, TypeError, ValueError):
            return

    @classmethod
    def from_files(cls, path: str | Path, tests: Mapping[int, str],
                   coverage_path: str | Path) -> "BodyWitnesses | None":
        try:
            path = Path(path)
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt", encoding="utf-8") as stream:
                value = json.load(stream)
            digest = hashlib.sha256(Path(coverage_path).read_bytes()).hexdigest()
            return cls(value, tests, digest)
        except (OSError, ValueError, TypeError):
            return None

    def equivalent(self, path: str, source: str, node: ast.Compare,
                   operator: str, number: int) -> bool:
        try:
            record = self.files[path]
            if record["sha256"] != hashlib.sha256(source.encode()).hexdigest():
                return False
            site = record["sites"][comparison_site(node)]
            if site["ast"] != ast.dump(node):
                return False
            observations = site["tests"][str(number)]
            if not isinstance(observations, list) or not observations:
                return False
            original = type(node.ops[0]).__name__
            # Identity comparisons themselves cannot raise or call user code.
            # Other original operators need exception observations as well as
            # returned values before equal-result witnesses can justify omission.
            if original not in ("Is", "IsNot"):
                return False
            return all(isinstance(item, list) and len(item) == 4
                       and type(item[3]) is bool
                       and observed_outcome(item, original) is item[3]
                       and observed_outcome(item, operator) is item[3]
                       for item in observations)
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            return False


def narrow_body(path: str, old_source: str, new_source: str, old_tree: ast.AST,
                new_tree: ast.AST, record: Mapping[str, Any],
                witnesses: BodyWitnesses | None = None) -> set[int] | None:
    """Return dependencies for pure comparison edits, or decline with ``None``.

    The caller retains file readers, test owners and globally untraced tests.
    It must first validate the patch, function boundaries and import-time shell.
    A comparison not reached by a base test cannot change that test's execution;
    reached comparisons need an equal-outcome witness before being omitted.
    """
    changes = comparison_changes(old_tree, new_tree)
    if changes is None:
        return None
    selected: set[int] = set()
    for node, operator in changes:
        lines = range(node.lineno, (node.end_lineno or node.lineno) + 1)
        if any(line in record["startup"] for line in lines):
            return None
        covered = {number for line in lines for number in record["lines"].get(line, ())}
        # An absent line is ambiguous (multiline bytecode may attribute its
        # execution elsewhere), so only observed comparison locations narrow.
        if not covered:
            return None
        selected.update(number for number in covered
                        if witnesses is None or not witnesses.equivalent(
                            path, old_source, node, operator, number))
    return selected
