"""Deterministic, base-only test impact selection for the pinned SQLGlot tree.

The coverage map is a dependency map, not a prediction of which tests fail.
Anything the map cannot justify conservatively selects the complete manifest.
No patched code is imported or executed by this module.
"""

from __future__ import annotations

import ast
import gzip
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from trivia import equivalent_python
from handlers import callback_tests
from guards import guard_tests
from inherited_schema import InheritedSchemaSelector
from m2_dispatch import DispatchSelector
from m2_body import BodyWitnesses, narrow_body
from observer_targets import ObserverTargets


BASE_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")


class _UnsafePatch(ValueError):
    """The patch or its dependencies do not support a narrow selection."""


@dataclass
class _PatchLine:
    kind: str
    text: str


@dataclass
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[_PatchLine]


@dataclass
class _FilePatch:
    path: str
    hunks: list[_Hunk]


@dataclass(frozen=True)
class _Scope:
    key: tuple[tuple[str, int], ...]
    start: int
    end: int


def _physical_lines(text: str) -> list[str]:
    # splitlines() also splits legal source characters such as form feeds.
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _path(header: str, prefix: str) -> str:
    value = header[len(prefix):].rstrip("\n").split("\t", 1)[0]
    if value.startswith(("a/", "b/")):
        value = value[2:]
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith(('"', "'", "/"))
        or "\\" in value
        or ":" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
        or path.as_posix() != value
    ):
        raise _UnsafePatch("unsupported path")
    return value


def _parse_patch(text: str) -> list[_FilePatch]:
    if not isinstance(text, str) or "\x00" in text or "\r" in text.replace("\r\n", ""):
        raise _UnsafePatch("not a textual unified diff")
    lines = _physical_lines(text.replace("\r\n", "\n"))
    files: list[_FilePatch] = []
    seen: set[str] = set()
    i = 0
    while i < len(lines):
        if lines[i].startswith("diff --git "):
            i += 1
            if i < len(lines) and lines[i].startswith("index "):
                i += 1
        if i >= len(lines) or not lines[i].startswith("--- "):
            raise _UnsafePatch("missing old-file header or unsupported metadata")
        old_path = _path(lines[i], "--- ")
        i += 1
        if i >= len(lines) or not lines[i].startswith("+++ "):
            raise _UnsafePatch("missing new-file header")
        new_path = _path(lines[i], "+++ ")
        i += 1
        if old_path != new_path or old_path in seen:
            raise _UnsafePatch("creation, deletion, rename, or repeated file")
        seen.add(old_path)
        hunks: list[_Hunk] = []
        while i < len(lines) and lines[i].startswith("@@ "):
            match = _HUNK.fullmatch(lines[i].rstrip("\n"))
            if match is None:
                raise _UnsafePatch("invalid hunk header")
            old_start, old_count, new_start, new_count = (
                int(match[1]), int(match[2] or 1), int(match[3]), int(match[4] or 1)
            )
            if (old_count and not old_start) or (new_count and not new_start):
                raise _UnsafePatch("invalid hunk coordinates")
            if not old_count and not new_count:
                raise _UnsafePatch("empty hunk")
            i += 1
            old_seen = new_seen = 0
            body: list[_PatchLine] = []
            while old_seen < old_count or new_seen < new_count:
                if i >= len(lines) or lines[i][:1] not in (" ", "+", "-"):
                    raise _UnsafePatch("truncated hunk")
                raw = lines[i]
                if not raw.endswith("\n"):
                    raise _UnsafePatch("unterminated diff line")
                kind = raw[0]
                body.append(_PatchLine(kind, raw[1:]))
                old_seen += kind != "+"
                new_seen += kind != "-"
                i += 1
                if i < len(lines) and lines[i] == "\\ No newline at end of file\n":
                    body[-1].text = body[-1].text[:-1]
                    i += 1
                if old_seen > old_count or new_seen > new_count:
                    raise _UnsafePatch("hunk counts do not match")
            hunks.append(_Hunk(old_start, old_count, new_start, new_count, body))
        if not hunks:
            raise _UnsafePatch("file has no hunks")
        files.append(_FilePatch(old_path, hunks))
    return files


def _apply_patch(source: str, patch: _FilePatch) -> tuple[str, set[int], set[int]]:
    """Verify every hunk and return new source, removed lines, insertion anchors."""
    source = source.replace("\r\n", "\n")
    lines = _physical_lines(source)
    result: list[str] = []
    removed: set[int] = set()
    insertions: set[int] = set()
    position = 0
    for hunk in patch.hunks:
        start = hunk.old_start - 1 if hunk.old_count else hunk.old_start
        if start < position or start > len(lines) or start + hunk.old_count > len(lines):
            raise _UnsafePatch("overlapping or out-of-range hunks")
        result.extend(lines[position:start])
        expected_new = hunk.new_start - 1 if hunk.new_count else hunk.new_start
        if expected_new != len(result):
            raise _UnsafePatch("inconsistent new hunk coordinates")
        position = start
        block_removed = False
        block_insertions: set[int] = set()

        def finish_block() -> None:
            if not block_removed:
                insertions.update(block_insertions)

        for line in hunk.lines:
            if line.kind == " ":
                finish_block()
                block_removed = False
                block_insertions.clear()
            if line.kind != "+":
                if position >= len(lines) or lines[position] != line.text:
                    raise _UnsafePatch("hunk does not match pinned source")
                if line.kind == "-":
                    removed.add(position + 1)
                    block_removed = True
                position += 1
            else:
                block_insertions.add(position)
            if line.kind != "-":
                result.append(line.text)
        finish_block()
    result.extend(lines[position:])
    return "".join(result), removed, insertions


def _scope_tree(source: str) -> tuple[ast.Module, list[_Scope]]:
    tree = ast.parse(source)
    scopes: list[_Scope] = []

    def visit(body: list[ast.stmt], prefix: tuple[tuple[str, int], ...]) -> None:
        occurrences: dict[str, int] = {}
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                occurrence = occurrences.get(node.name, 0)
                occurrences[node.name] = occurrence + 1
                key = prefix + ((node.name, occurrence),)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Do not select a nested function in isolation: bindings and
                    # closure layouts can affect its entire enclosing function.
                    scopes.append(_Scope(key, node.body[0].lineno, node.end_lineno or node.lineno))
                else:
                    visit(node.body, key)

    visit(tree.body, ())
    return tree, scopes


def _module_shell(value):
    """Compare import-time AST structure without copying ordinary function bodies."""
    if isinstance(value, list):
        return tuple(_module_shell(item) for item in value)
    if not isinstance(value, ast.AST):
        # Keep True, 1 and 1.0 distinguishable, just as ast.dump does.
        return repr(value)
    fields = []
    for name, child in ast.iter_fields(value):
        if name == "body" and isinstance(value, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # The docstring is visible at import even if nobody calls the function.
            child = child[:1] if ast.get_docstring(value, clean=False) is not None else []
        fields.append((name, _module_shell(child)))
    return type(value), tuple(fields)


def _read_json(path: str | Path) -> Any:
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


class ImpactSelector:
    """Select manifest numbers using a coverage map of the pinned base only.

    ``tests`` accepts either the complete tests.json object or its tests list.
    ``coverage`` and ``base_sources`` are decoded JSON objects. Malformed or
    incomplete optional assets safely turn the selector into a run-all policy.
    """

    def __init__(
        self,
        tests: Mapping[str, Any] | Iterable[Mapping[str, Any]],
        coverage: Mapping[str, Any] | None = None,
        base_sources: Mapping[str, str] | str | Path | None = None,
        position_observers: Iterable[int] | None = None,
    ) -> None:
        entries = list(tests["tests"] if isinstance(tests, Mapping) else tests)
        self.tests = {entry["number"]: entry["id"] for entry in entries}
        if any(type(number) is not int or number < 0 for number in self.tests):
            raise ValueError("manifest test numbers must be nonnegative integers")
        self.all_tests = sorted(self.tests)
        self._owners: dict[str, set[int]] = {}
        for entry in entries:
            if isinstance(entry.get("file"), str):
                self._owners.setdefault(entry["file"], set()).add(entry["number"])
        self.coverage = coverage or {}
        self.base_root = Path(base_sources).resolve() if isinstance(base_sources, (str, Path)) else None
        self.base_sources = base_sources if isinstance(base_sources, Mapping) else {}
        self._records: dict[str, dict[str, Any] | None] = {}
        self._scopes: dict[str, tuple[ast.Module, list[_Scope]]] = {}
        self._ready = False
        self._untraced: set[int] = set()
        self._position_observers: set[int] | None = None
        self._observer_targets: ObserverTargets | None = None
        self._dispatch: DispatchSelector | None = None
        self._body: BodyWitnesses | None = None
        self._inherited_schema: InheritedSchemaSelector | None = None
        try:
            mapped_tests = {entry["number"]: entry["id"] for entry in self.coverage["tests"]}
            context = self.coverage.get("context", {})
            self._ready = (
                type(self.coverage["version"]) is int
                and self.coverage["version"] == 1
                and self.coverage["base_commit"] == BASE_COMMIT
                and mapped_tests == self.tests
                and context["complete"] is True
                and context["startup_untraced"] is False
                and isinstance(self.coverage["paths"], Mapping)
            )
            self._untraced = self._numbers(context["untraced_tests"])
            if position_observers is not None:
                self._position_observers = self._numbers(position_observers)
        except (AttributeError, KeyError, TypeError, ValueError):
            self._ready = False

    @classmethod
    def from_files(
        cls,
        tests_path: str | Path,
        coverage_path: str | Path | None = None,
        base_sources_path: str | Path | None = None,
    ) -> "ImpactSelector":
        return cls.with_assets(_read_json(tests_path), coverage_path, base_sources_path)

    @classmethod
    def with_assets(cls, tests, coverage_path, base_sources_path):
        """Load only baseline evidence that matches this manifest and checkout."""
        try:
            coverage = _read_json(coverage_path) if coverage_path is not None else None
            sources = (
                Path(base_sources_path) if base_sources_path is not None and Path(base_sources_path).is_dir()
                else _read_json(base_sources_path) if base_sources_path is not None
                else None
            )
        except (OSError, ValueError, TypeError):
            coverage = sources = None
        observers = cls.load_position_observers(coverage_path) if coverage_path is not None else None
        selector = cls(tests, coverage, sources, observers)
        if selector._ready and coverage_path is not None:
            maps = Path(coverage_path).parent
            selector._dispatch = DispatchSelector.from_file(
                maps / "dispatch.json.gz", tests, schema_path=maps / "schema.json.gz",
                scalar_path=maps / "scalars.json.gz",
            )
            selector._body = BodyWitnesses.from_files(maps / "body.json.gz", selector.tests, coverage_path)
            if observers is not None:
                selector._observer_targets = ObserverTargets.from_files(coverage_path, coverage)
                selector._inherited_schema = InheritedSchemaSelector.from_file(
                    maps / "inherited_schema.json.gz", tests, coverage_path
                )
        return selector

    @staticmethod
    def load_position_observers(coverage_path: str | Path) -> list[int] | None:
        coverage_path = Path(coverage_path)
        try:
            index = _read_json(coverage_path.with_name("positions.json"))
            if index["version"] == 1 and index["shared"] is False and isinstance(index["tests"], list):
                digest = hashlib.sha256(coverage_path.read_bytes()).hexdigest()
                if digest == index["coverage_sha256"]:
                    return index["tests"]
        except (KeyError, OSError, ValueError, TypeError):
            pass
        return None

    def _numbers(self, values: Iterable[int]) -> set[int]:
        numbers = set(values)
        if any(type(number) is not int or number not in self.tests for number in numbers):
            raise _UnsafePatch("unknown test number in dependency map")
        return numbers

    def _record(self, path: str) -> dict[str, Any]:
        if path not in self._records:
            self._records[path] = None
            try:
                if self.base_root is not None:
                    file_path = (self.base_root / path).resolve()
                    if not file_path.is_relative_to(self.base_root):
                        raise _UnsafePatch("source escapes the pinned base directory")
                    source = file_path.read_bytes().decode("utf-8").replace("\r\n", "\n")
                else:
                    source = self.base_sources[path]
                raw = self.coverage["paths"][path]
                if not isinstance(raw, Mapping):
                    raise _UnsafePatch("invalid per-file record")
                if not isinstance(source, str) or hashlib.sha256(source.encode("utf-8")).hexdigest() != raw["sha256"]:
                    raise _UnsafePatch("base source hash mismatch")
                count = len(_physical_lines(source.replace("\r\n", "\n")))
                if type(raw["line_count"]) is not int or raw["line_count"] != count:
                    raise _UnsafePatch("base line count mismatch")
                line_map: dict[int, set[int]] = {}
                for line, tests in raw["lines"].items():
                    number = int(line)
                    if str(number) != str(line) or not 1 <= number <= count:
                        raise _UnsafePatch("invalid line coverage")
                    line_map[number] = self._numbers(tests)
                startup = set(raw["startup"])
                if any(type(line) is not int or not 1 <= line <= count for line in startup):
                    raise _UnsafePatch("invalid startup coverage")
                if type(raw["startup_read"]) is not bool:
                    raise _UnsafePatch("invalid startup read flag")
                self._records[path] = {
                    "source": source,
                    "lines": line_map,
                    "startup": startup,
                    "readers": self._numbers(raw["readers"]),
                    "startup_read": raw["startup_read"],
                }
            except (AttributeError, KeyError, OSError, TypeError, ValueError):
                pass
        record = self._records[path]
        if record is None:
            raise _UnsafePatch("missing or invalid per-file dependencies")
        return record

    def select(self, patch_text: str) -> list[int]:
        """Return sorted, unique test numbers; malformed patches select all."""
        if patch_text == "":
            return []
        if not self._ready:
            return self.all_tests.copy()
        try:
            patches = _parse_patch(patch_text)
            if not patches:
                return []
            selected = set(self._untraced)
            for patch in patches:
                record = self._record(patch.path)
                if record["startup_read"]:
                    raise _UnsafePatch("file was read outside an attributable test")
                new_source, removed, insertions = _apply_patch(record["source"], patch)
                if not removed and not insertions:
                    raise _UnsafePatch("hunk contains no edit")
                selected.update(record["readers"])
                owners = self._owners.get(patch.path, set())
                selected.update(owners)
                if not patch.path.endswith(".py"):
                    if not record["readers"]:
                        raise _UnsafePatch("fixture has no attributable readers")
                    continue
                if self._position_observers is not None and all(
                    not line.text.strip() or line.text.lstrip().startswith("#")
                    for hunk in patch.hunks for line in hunk.lines if line.kind != " "
                ):
                    if equivalent_python(record["source"], new_source, patch.path) is not None:
                        observers = (self._observer_targets.select(patch.path)
                                     if self._observer_targets is not None else self._position_observers)
                        selected.update(observers)
                        continue
                if patch.path not in self._scopes:
                    self._scopes[patch.path] = _scope_tree(record["source"])
                old_tree, scopes = self._scopes[patch.path]
                new_tree, _ = _scope_tree(new_source)
                # Parsing alone accepts, for example, a return outside a function.
                compile(new_tree, patch.path, "exec", dont_inherit=True)
                if _module_shell(old_tree) != _module_shell(new_tree):
                    consumers = callback_tests(old_tree, new_tree, record, self._record)
                    if consumers is None and self._dispatch is not None:
                        consumers = self._dispatch.select(
                            patch.path, record["source"], new_source, single_file=len(patches) == 1
                        )
                    if (consumers is None and self._inherited_schema is not None
                            and self._position_observers is not None and len(patches) == 1
                            and len(removed) == 1 and not insertions):
                        consumers = self._inherited_schema.select(
                            patch.path, record["source"], new_source, next(iter(removed))
                        )
                        if consumers is not None:
                            consumers.update(self._position_observers)
                    if consumers is not None:
                        selected.update(consumers)
                        continue
                    raise _UnsafePatch("import-time module structure changed")
                touched: set[_Scope] = set()
                for line in removed:
                    matching = [scope for scope in scopes if scope.start <= line <= scope.end]
                    if len(matching) != 1 or line in record["startup"]:
                        raise _UnsafePatch("edit outside an attributable function body")
                    touched.add(matching[0])
                for anchor in insertions:
                    matching = [scope for scope in scopes if scope.start <= anchor < scope.end]
                    if len(matching) != 1 or anchor in record["startup"] or anchor + 1 in record["startup"]:
                        raise _UnsafePatch("insertion does not have two safe scope anchors")
                    touched.add(matching[0])
                if self._body is not None and any(
                    re.search(r"\bis\b|[=!<>]=", line.text) for hunk in patch.hunks
                    for line in hunk.lines if line.kind == "-"
                ):
                    observed = narrow_body(patch.path, record["source"], new_source,
                                           old_tree, new_tree, record, self._body)
                    if observed is not None:
                        selected.update(observed)
                        continue
                if len(removed) == 1 and not insertions:
                    line = next(iter(removed))
                    original_line = record["source"].split("\n")[line - 1].lstrip()
                    if original_line.startswith(("if ", "elif ", "while ")):
                        guarded = guard_tests(old_tree, new_tree, removed, insertions, record)
                        if guarded is not None:
                            selected.update(guarded)
                            continue
                for scope in touched:
                    dependencies: set[int] = set()
                    for line, tests in record["lines"].items():
                        if scope.start <= line <= scope.end:
                            if line in record["startup"]:
                                raise _UnsafePatch("function executes outside test context")
                            dependencies.update(tests)
                    if not dependencies and not owners:
                        raise _UnsafePatch("function has no attributable coverage")
                    selected.update(dependencies)
            return sorted(selected)
        except (SyntaxError, ValueError, TypeError, KeyError, RecursionError, MemoryError, OverflowError):
            return self.all_tests.copy()
