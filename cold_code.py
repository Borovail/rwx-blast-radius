"""Recognize body edits inside a pinned callable that never started execution."""

from __future__ import annotations

import ast
import gzip
import hashlib
import inspect
import json
from pathlib import Path, PurePosixPath
from types import CodeType
import warnings
import zlib

from observer_targets import positions_digest


BASE_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
POLICY = "pinned-cold-code-v1"


def _functions(tree):
    result = {}

    class Index(ast.NodeVisitor):
        prefix = ""

        def visit_ClassDef(self, node):
            previous = self.prefix
            self.prefix += node.name + "."
            for statement in node.body:
                self.visit(statement)
            self.prefix = previous

        def visit_FunctionDef(self, node):
            qualified = self.prefix + node.name
            key = qualified, node.lineno
            result[key] = node if key not in result else None
            previous = self.prefix
            self.prefix = qualified + ".<locals>."
            for statement in node.body:
                self.visit(statement)
            self.prefix = previous

        visit_AsyncFunctionDef = visit_FunctionDef

    Index().visit(tree)
    return result


def _offset(source, line, column):
    lines = source.split(b"\n")
    return sum(len(value) + 1 for value in lines[:line - 1]) + column


def _outside_body(before, after, old_bytes, new_bytes, old_tree, new_tree):
    if (type(before) is not type(after) or before.decorator_list or after.decorator_list
            or getattr(before, "type_params", ()) or getattr(after, "type_params", ())
            or before.lineno != after.lineno or before.col_offset != after.col_offset
            or before.end_lineno != after.end_lineno
            or ast.get_docstring(before, clean=False) != ast.get_docstring(after, clean=False)):
        return False
    first, new_first = before.body[0], after.body[0]
    if (first.lineno != new_first.lineno or first.col_offset != new_first.col_offset
            or len(old_bytes.split(b"\n")) != len(new_bytes.split(b"\n"))):
        return False
    start = _offset(old_bytes, first.lineno, first.col_offset)
    new_start = _offset(new_bytes, new_first.lineno, new_first.col_offset)
    end = _offset(old_bytes, before.end_lineno, before.end_col_offset)
    new_end = _offset(new_bytes, after.end_lineno, after.end_col_offset)
    if old_bytes[:start] != new_bytes[:new_start] or old_bytes[end:] != new_bytes[new_end:]:
        return False
    if ast.get_docstring(before, clean=False) is not None:
        old_doc_end = _offset(old_bytes, first.end_lineno, first.end_col_offset)
        new_doc_end = _offset(new_bytes, new_first.end_lineno, new_first.end_col_offset)
        if old_bytes[start:old_doc_end] != new_bytes[new_start:new_doc_end]:
            return False
    saved = after.body
    try:
        after.body = before.body
        return ast.dump(old_tree, include_attributes=False) == ast.dump(new_tree, include_attributes=False)
    finally:
        after.body = saved


def _creation(code):
    parameter_count = code.co_argcount + code.co_kwonlyargcount
    parameter_count += bool(code.co_flags & inspect.CO_VARARGS)
    parameter_count += bool(code.co_flags & inspect.CO_VARKEYWORDS)
    return (code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount,
            code.co_varnames[:parameter_count], code.co_flags,
            code.co_freevars, code.co_cellvars, code.co_name,
            code.co_qualname, code.co_firstlineno, code.co_filename)


def _constant(value, target):
    if type(value) is CodeType:
        return _signature(value, target)
    if type(value) is tuple:
        return "tuple", tuple(_constant(item, target) for item in value)
    if type(value) is frozenset:
        return "frozenset", frozenset(_constant(item, target) for item in value)
    return type(value).__name__, repr(value)


def _signature(code, target):
    if (code.co_qualname, code.co_firstlineno) == target:
        return "cold", _creation(code)
    return ("code", _creation(code), code.co_code,
            tuple(_constant(item, target) for item in code.co_consts), code.co_names,
            code.co_varnames, code.co_nlocals, code.co_stacksize,
            tuple(code.co_lines()), code.co_exceptiontable)


def _code_matches(before, after, target):
    def matches(code):
        found = []
        pending = [code]
        while pending:
            current = pending.pop()
            if (current.co_qualname, current.co_firstlineno) == target:
                found.append(current)
            pending.extend(value for value in current.co_consts if type(value) is CodeType)
        return found

    old, new = matches(before), matches(after)
    if len(old) != 1 or len(new) != 1:
        return False
    deferred = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR
    if (old[0].co_flags | new[0].co_flags) & deferred:
        return False
    return _signature(before, target) == _signature(after, target)


class ColdCode:
    def __init__(self, artifact, tests, coverage_sha256, positions_sha256):
        self.ready = False
        self.providers = {}
        tests = tests["tests"] if isinstance(tests, dict) else tests
        try:
            self.numbers = {row["number"] for row in tests}
            if (any(type(number) is not int or number < 0 for number in self.numbers)
                    or len(self.numbers) != len(tests)
                    or len({row["id"] for row in tests}) != len(tests)
                    or type(artifact["version"]) is not int or artifact["version"] != 1
                    or artifact["policy"] != POLICY or artifact["base_commit"] != BASE_COMMIT
                    or artifact["complete"] is not True
                    or artifact["coverage_sha256"] != coverage_sha256
                    or artifact["positions_sha256"] != positions_sha256
                    or artifact["tests"] != [{"number": row["number"], "id": row["id"]} for row in tests]):
                return
            self.observers = self._numbers(artifact["observers"])
            self.untraced = self._numbers(artifact["untraced"])
            self.source_observers = self._numbers(artifact["source_observers"])
            self.owners = {}
            for row in tests:
                self.owners.setdefault(row["id"].split("::")[0], set()).add(row["number"])
            for path, row in artifact["providers"].items():
                if (type(path) is not str or not path.startswith("sqlglot/") or not path.endswith(".py")
                        or str(PurePosixPath(path)) != path or "\\" in path
                        or ".." in PurePosixPath(path).parts
                        or type(row["source_sha256"]) is not str or len(row["source_sha256"]) != 64
                        or any(char not in "0123456789abcdef" for char in row["source_sha256"])
                        or type(row["cold"]) is not list):
                    return
                cold = set()
                for entry in row["cold"]:
                    key = entry["qualname"], entry["first_line"]
                    if (type(key[0]) is not str or not key[0] or type(key[1]) is not int
                            or key[1] < 1 or key in cold):
                        return
                    cold.add(key)
                self.providers[path] = row["source_sha256"], cold
            self.ready = True
        except (AttributeError, KeyError, TypeError, ValueError):
            return

    def _numbers(self, values):
        values = list(values)
        if any(type(number) is not int or number not in self.numbers for number in values):
            raise ValueError("unknown test in cold-code evidence")
        if len(values) != len(set(values)):
            raise ValueError("duplicate test in cold-code evidence")
        return set(values)

    @classmethod
    def from_files(cls, map_path, coverage_path, tests):
        try:
            map_path, coverage_path = Path(map_path), Path(coverage_path)
            positions = json.loads(coverage_path.with_name("positions.json").read_bytes())
            if positions["shared"] is not False:
                return None
            raw = map_path.read_bytes()
            artifact = json.loads(gzip.decompress(raw) if map_path.suffix == ".gz" else raw)
            return cls(artifact, tests, hashlib.sha256(coverage_path.read_bytes()).hexdigest(),
                       positions_digest(positions))
        except (EOFError, OSError, ValueError, TypeError, KeyError, zlib.error, RecursionError):
            return None

    def select(self, path, old_source, new_source, record, *, position_observers, single_file=False):
        if (not self.ready or not single_file or path not in self.providers
                or position_observers is None or record.get("startup_read") is not False):
            return None
        try:
            digest, cold = self.providers[path]
            readers = self._numbers(record["readers"])
            if not readers <= self.source_observers:
                return None
            old_bytes, new_bytes = old_source.encode("utf-8"), new_source.encode("utf-8")
            if old_bytes == new_bytes or hashlib.sha256(old_bytes).hexdigest() != digest:
                return None
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                old_tree, new_tree = ast.parse(old_bytes, path), ast.parse(new_bytes, path)
                old_code = compile(old_bytes, path, "exec", dont_inherit=True, optimize=0)
                new_code = compile(new_bytes, path, "exec", dont_inherit=True, optimize=0)
            before, after = _functions(old_tree), _functions(new_tree)
            for key in cold:
                left, right = before.get(key), after.get(key)
                if left is None or right is None:
                    continue
                if not _outside_body(left, right, old_bytes, new_bytes, old_tree, new_tree):
                    continue
                if not _code_matches(old_code, new_code, key):
                    continue
                return (self.observers | self.untraced | self.owners.get(path, set())
                        | readers | self._numbers(record.get("untraced", ()))
                        | self._numbers(position_observers))
        except (AttributeError, IndexError, KeyError, SyntaxError, Warning, TypeError, ValueError,
                RecursionError, MemoryError, OverflowError):
            return None
        return None
