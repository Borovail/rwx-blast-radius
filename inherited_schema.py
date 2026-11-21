"""Use pinned evidence for a one-flag override of an inherited argument schema."""

from __future__ import annotations

import ast
import gzip
import hashlib
import json
import warnings
import zlib
from pathlib import Path

from m2_dispatch import BASE_COMMIT


METHOD = "inherited-argument-schemas-v1"


def _unique_dict(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate key in inherited schema evidence")
    return result


def _test_ids(rows):
    by_number = {}
    seen = set()
    for row in rows:
        number, nodeid = row["number"], row["id"]
        if (type(number) is not int or number < 0 or number in by_number
                or not isinstance(nodeid, str) or not nodeid or nodeid in seen):
            raise ValueError("invalid test identity in inherited schema evidence")
        by_number[number] = nodeid
        seen.add(nodeid)
    return by_number


def _single_statement(node):
    if node is not None and not node.decorator_list and not node.keywords and len(node.body) == 1:
        return node.body[0]
    return None


def _new_flags(statement):
    if not (isinstance(statement, ast.Assign) and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == "arg_types"
            and isinstance(statement.value, ast.Dict)):
        return None
    flags = {}
    for key, value in zip(statement.value.keys, statement.value.values):
        if not (isinstance(key, ast.Constant) and type(key.value) is str and key.value not in flags
                and isinstance(value, ast.Constant) and type(value.value) is bool):
            return None
        flags[key.value] = value.value
    return flags


class InheritedSchemaSelector:
    def __init__(self, paths, numbers):
        self._paths = paths
        self._numbers = numbers

    @classmethod
    def from_file(cls, path, tests, coverage_path):
        try:
            path = Path(path)
            with (gzip.open if path.suffix == ".gz" else open)(path, "rt", encoding="utf-8") as file:
                artifact = json.load(file, object_pairs_hook=_unique_dict)
            context, paths = artifact["context"], artifact["paths"]
            if (artifact["base_commit"] != BASE_COMMIT or context["method"] != METHOD
                    or context["complete"] is not True or not isinstance(paths, dict)):
                return None
            expected = _test_ids(tests["tests"] if isinstance(tests, dict) else tests)
            if not expected or _test_ids(artifact["tests"]) != expected:
                return None
            if hashlib.sha256(Path(coverage_path).read_bytes()).hexdigest() != context["coverage_sha256"]:
                return None
            return cls(paths, set(expected))
        except (OSError, EOFError, KeyError, TypeError, ValueError, RecursionError, MemoryError, zlib.error):
            return None

    def select(self, path, old_source, new_source, changed_line) -> set[int] | None:
        if type(changed_line) is not int or changed_line < 1:
            return None
        try:
            path_row = self._paths[path]
            old_bytes, new_bytes = old_source.encode("utf-8"), new_source.encode("utf-8")
            if hashlib.sha256(old_bytes).hexdigest() != path_row["sha256"]:
                return None

            old_lines, new_lines = old_bytes.split(b"\n"), new_bytes.split(b"\n")
            index = changed_line - 1
            if (len(old_lines) != len(new_lines) or index >= len(old_lines)
                    or old_lines[:index] != new_lines[:index]
                    or old_lines[index + 1:] != new_lines[index + 1:]):
                return None

            with warnings.catch_warnings():
                warnings.simplefilter("error", SyntaxWarning)
                old_tree, new_tree = ast.parse(old_bytes, path), ast.parse(new_bytes, path)

            old_classes = [node for node in old_tree.body if isinstance(node, ast.ClassDef)]
            candidates = [
                node for node in old_classes
                if isinstance(_single_statement(node), ast.Pass)
                and node.body[0].lineno == node.body[0].end_lineno == changed_line
            ]
            if len(candidates) != 1:
                return None
            node = candidates[0]
            if sum(other.name == node.name for other in old_classes) != 1:
                return None
            row = path_row["classes"][node.name]
            schema, readers = row["schema"], row["tests"]
            if (not isinstance(schema, dict) or not schema
                    or not all(type(key) is str and type(value) is bool for key, value in schema.items())
                    or not isinstance(readers, list) or not readers
                    or not all(type(number) is int and number in self._numbers for number in readers)
                    or len(set(readers)) != len(readers)):
                return None

            after = new_tree.body[old_tree.body.index(node)]
            if not isinstance(after, ast.ClassDef) or after.name != node.name:
                return None
            before, assignment = node.body[0], _single_statement(after)
            flags = _new_flags(assignment)
            if (flags is None or tuple(flags) != tuple(schema)
                    or sum(flags[key] is not value for key, value in schema.items()) != 1):
                return None

            original_line, replacement_line = old_lines[index], new_lines[index]
            if (assignment.lineno != changed_line or assignment.end_lineno != changed_line
                    or assignment.col_offset != before.col_offset):
                return None
            target = assignment.targets[0]
            if (original_line[before.col_offset:before.end_col_offset] != b"pass"
                    or replacement_line[target.col_offset:target.end_col_offset] != b"arg_types"
                    or original_line[:before.col_offset] != replacement_line[:assignment.col_offset]
                    or original_line[before.end_col_offset:] != replacement_line[assignment.end_col_offset:]):
                return None

            with warnings.catch_warnings():
                warnings.simplefilter("error", SyntaxWarning)
                compile(new_tree, path, "exec", dont_inherit=True)
            return set(readers)
        except (AttributeError, IndexError, KeyError, SyntaxError, SyntaxWarning, TypeError,
                ValueError, RecursionError, MemoryError, OverflowError):
            return None
