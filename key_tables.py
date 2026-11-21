"""Select readers of one exactly matched value in a witnessed baseline table."""

from __future__ import annotations

import ast
import gzip
import hashlib
import io
import json
from pathlib import Path
import tokenize
import warnings
import zlib

from observer_targets import positions_digest


BASE_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
POLICY = "pinned-table-key-reads-v1"
PROVIDERS = {
    "sqlglot/dialects/dialect.py": ("dialect.Dialect", "Dialect", "SET_OP_DISTINCT_BY_DEFAULT", {"Except", "Intersect", "Union"}),
    "sqlglot/dialects/clickhouse.py": ("clickhouse.ClickHouse", "ClickHouse", "SET_OP_DISTINCT_BY_DEFAULT", {"Except", "Intersect", "Union"}),
    "sqlglot/generators/duckdb.py": ("duckdb._SEQ_BYTE_WIDTH", None, "_SEQ_BYTE_WIDTH", {"Seq1", "Seq2", "Seq4", "Seq8"}),
}


def _dump(node):
    return ast.dump(node, include_attributes=False)


def _declaration(tree, owner, name):
    body = tree.body
    if owner is not None:
        classes = [node for node in body if type(node) is ast.ClassDef and node.name == owner]
        if len(classes) != 1:
            return None
        body = classes[0].body
    values = []
    for node in body:
        if type(node) is ast.Assign and len(node.targets) == 1:
            target = node.targets[0]
        elif type(node) is ast.AnnAssign:
            target = node.target
        else:
            continue
        if type(target) is ast.Name and target.id == name:
            values.append(node.value)
    return values[0] if len(values) == 1 and type(values[0]) is ast.Dict else None


def _key(node):
    if (type(node) is ast.Attribute and type(node.value) is ast.Name
            and node.value.id == "exp" and type(node.ctx) is ast.Load):
        return node.attr
    return None


def _offsets(source, node):
    lines = source.split(b"\n")
    start = sum(len(line) + 1 for line in lines[:node.lineno - 1]) + node.col_offset
    return start, start + node.end_col_offset - node.col_offset


def _single_token(fragment, kind):
    tokens = list(tokenize.generate_tokens(io.StringIO(fragment.decode("utf-8")).readline))
    tokens = [token for token in tokens if token.type not in (tokenize.NEWLINE, tokenize.ENDMARKER)]
    return len(tokens) == 1 and tokens[0].type == kind


class KeyTables:
    def __init__(self, artifact, tests, coverage_sha256, positions_sha256):
        self.ready = False
        self.providers = {}
        tests = tests["tests"] if isinstance(tests, dict) else tests
        try:
            self.numbers = {row["number"] for row in tests}
            identities = {row["id"] for row in tests}
            if (any(type(number) is not int for number in self.numbers)
                    or len(self.numbers) != len(tests) or len(identities) != len(tests)
                    or artifact["version"] != 1 or type(artifact["version"]) is not int
                    or artifact["policy"] != POLICY or artifact["base_commit"] != BASE_COMMIT
                    or artifact["complete"] is not True
                    or artifact["coverage_sha256"] != coverage_sha256
                    or artifact["positions_sha256"] != positions_sha256
                    or artifact["tests"] != [{"number": row["number"], "id": row["id"]} for row in tests]):
                return
            self.owners = {}
            for row in tests:
                self.owners.setdefault(row["id"].split("::")[0], set()).add(row["number"])
            self.untraced = self._numbers(artifact["untraced"])
            self.groups = [self._numbers(group) for group in artifact["shared_pool_groups"]]
            for path, row in artifact["providers"].items():
                provider, _, _, expected = PROVIDERS[path]
                if (row["provider"] != provider or type(row["source_sha256"]) is not str
                        or len(row["source_sha256"]) != 64
                        or any(char not in "0123456789abcdef" for char in row["source_sha256"])
                        or set(row["keys"]) != expected or type(row["blocked_keys"]) is not list
                        or any(key not in expected for key in row["blocked_keys"])
                        or len(set(row["blocked_keys"])) != len(row["blocked_keys"])):
                    return
                self.providers[path] = {
                    "source_sha256": row["source_sha256"],
                    "keys": {key: self._numbers(numbers) for key, numbers in row["keys"].items()},
                    "blocked_keys": set(row["blocked_keys"]),
                }
            self.ready = True
        except (AttributeError, KeyError, TypeError, ValueError):
            return

    def _numbers(self, values):
        values = list(values)
        if any(type(number) is not int or number not in self.numbers for number in values):
            raise ValueError("unknown test in table read evidence")
        if len(set(values)) != len(values):
            raise ValueError("duplicate test in table read evidence")
        return set(values)

    @classmethod
    def from_files(cls, map_path, coverage_path, tests):
        try:
            coverage_path, map_path = Path(coverage_path), Path(map_path)
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
            row = self.providers[path]
            old_bytes, new_bytes = old_source.encode("utf-8"), new_source.encode("utf-8")
            if hashlib.sha256(old_bytes).hexdigest() != row["source_sha256"]:
                return None
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                old_tree, new_tree = ast.parse(old_bytes, path), ast.parse(new_bytes, path)
                compile(new_bytes, path, "exec", dont_inherit=True)
            _, owner, name, expected = PROVIDERS[path]
            before, after = _declaration(old_tree, owner, name), _declaration(new_tree, owner, name)
            if before is None or after is None:
                return None
            keys = [_key(key) for key in before.keys]
            if (set(keys) != expected or len(keys) != len(expected)
                    or [_key(key) for key in after.keys] != keys or len(before.values) != len(after.values)):
                return None
            for key, left, right in zip(keys, before.values, after.values):
                if _dump(left) == _dump(right):
                    continue
                if (key in row["blocked_keys"] or type(left) is not ast.Constant
                        or type(right) is not ast.Constant or type(left.value) is not type(right.value)
                        or left.kind != right.kind or left.lineno != left.end_lineno
                        or right.lineno != left.lineno or right.end_lineno != left.lineno
                        or right.col_offset != left.col_offset):
                    return None
                width = owner is None
                if width:
                    if type(right.value) is not int or not 1 <= left.value <= 16 or not 1 <= right.value <= 16:
                        return None
                elif type(right.value) is not bool:
                    return None
                start, end = _offsets(old_bytes, left)
                new_start, new_end = _offsets(new_bytes, right)
                token_type = tokenize.NUMBER if width else tokenize.NAME
                replacement = new_bytes[new_start:new_end]
                if (start != new_start or old_bytes[:start] + replacement + old_bytes[end:] != new_bytes
                        or not _single_token(old_bytes[start:end], token_type)
                        or not _single_token(replacement, token_type)):
                    return None
                right.value = left.value
                if _dump(old_tree) != _dump(new_tree):
                    return None
                selected = (row["keys"][key] | self.untraced | self.owners.get(path, set())
                            | self._numbers(record["readers"]) | self._numbers(record.get("untraced", ()))
                            | self._numbers(position_observers))
                while True:
                    closed = selected.union(*(group for group in self.groups if group & selected))
                    if closed == selected:
                        return selected
                    selected = closed
            return None
        except (AttributeError, IndexError, KeyError, SyntaxError, Warning, TypeError, ValueError,
                RecursionError, MemoryError, OverflowError, tokenize.TokenError):
            return None
