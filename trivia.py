"""Practical per-file Python trivia prediction for the pinned source tree.

This module never executes patched code. A positive result means that tokens,
syntax and compiled instructions agree after removing ordinary comments and
layout. It is not a proof against arbitrary source/position reflection in third
party code. The caller must retain file readers, test-file owners, untraced tests
and known source/position observers, and must process every other patched file.
"""

from __future__ import annotations

import ast
import codecs
from dataclasses import dataclass
import io
import re
import tokenize
from types import CodeType
from typing import Any


_DIRECTIVE = re.compile(
    r"(?i)(coding\s*[:=]|type\s*:|noqa\b|pylint\b|pyright\b|mypy\b|ruff\b|"
    r"fmt\s*:|isort\b|pragma\b|doctest\s*:|nosec\b|yapf\s*:|autopep8\s*:|^#!)"
)
_LAYOUT = {tokenize.ENCODING, tokenize.COMMENT, tokenize.NL}
_NORMALIZE = {tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.ENDMARKER}
_POSITION_ATTRS = {
    "__code__", "__firstlineno__", "__traceback__", "co_firstlineno", "co_linetable", "co_lnotab",
    "co_lines", "co_positions", "f_lineno", "f_code", "tb_lineno", "tb_frame",
    "gi_frame", "cr_frame", "ag_frame", "lineno", "end_lineno", "col_offset",
    "end_col_offset",
}
_SOURCE_APIS = {
    "getsource", "getsourcelines", "getsourcefile", "getfile", "findsource",
    "getframeinfo", "getouterframes", "getinnerframes", "currentframe", "_getframe",
    "settrace", "setprofile", "extract_stack", "extract_tb", "format_exc",
    "format_stack", "format_tb", "print_exc", "print_stack", "print_tb",
    "walk_stack", "walk_tb", "getline", "getlines", "findlinestarts",
    "get_instructions",
}
_OBSERVER_MODULES = {"inspect", "traceback", "linecache", "dis"}


@dataclass(frozen=True)
class PythonEquivalence:
    """Evidence about one file; positions require separate observer retention."""

    locations_changed: bool


def _bytes(source: str | bytes) -> bytes:
    # Base source assets contain UTF-8-decoded file bytes. Re-encode exactly;
    # re-encoding to the cookie's declared encoding would silently repair a bad
    # cookie. A bytes caller can also supply a real non-UTF-8 original directly.
    if isinstance(source, bytes):
        return source
    if isinstance(source, str):
        return source.encode("utf-8")
    raise TypeError("source must be UTF-8 text or original bytes")


def _tokens(source: bytes) -> tuple[Any, ...]:
    encoding, _ = tokenize.detect_encoding(io.BytesIO(source).readline)
    significant, directives = [], []
    for token in tokenize.tokenize(io.BytesIO(source).readline):
        if token.type == tokenize.ERRORTOKEN:
            raise ValueError("invalid source token")
        if token.type == tokenize.COMMENT and _DIRECTIVE.search(token.string):
            # Preserve directive spelling and attachment, including encoding
            # cookies, type/coverage ignores and tool pragmas.
            directives.append((token.start, token.string))
        if token.type not in _LAYOUT:
            significant.append((token.type, "" if token.type in _NORMALIZE else token.string))
    return codecs.lookup(encoding).name, tuple(significant), tuple(directives)


def _code_signature(code: CodeType, locations: bool = False) -> tuple[Any, ...]:
    constants = tuple(
        _code_signature(value, locations) if isinstance(value, CodeType)
        else (type(value).__name__, repr(value))
        for value in code.co_consts
    )
    result = (
        code.co_code, constants, code.co_names, code.co_varnames, code.co_freevars,
        code.co_cellvars, code.co_flags, code.co_argcount, code.co_posonlyargcount,
        code.co_kwonlyargcount, code.co_stacksize, code.co_name,
        getattr(code, "co_qualname", code.co_name), getattr(code, "co_exceptiontable", b""),
    )
    if locations:
        table = code.co_linetable if hasattr(code, "co_linetable") else code.co_lnotab
        result += (code.co_firstlineno, table)
    return result


def equivalent_python(
    old: str | bytes, new: str | bytes, filename: str = "module.py",
) -> PythonEquivalence | None:
    """Predict layout/comment equivalence for one Python file, or return None.

    Both original and changed bytes are tokenized and compiled with independent
    compiler flags. Literals (including SQL fixture text and docstrings) must
    retain their exact token spelling. Compiler-invalid ASTs, changed directives
    and changes to executable syntax always return None.
    """
    try:
        old_bytes, new_bytes = _bytes(old), _bytes(new)
        if _tokens(old_bytes) != _tokens(new_bytes):
            return None
        flags = ast.PyCF_ONLY_AST | ast.PyCF_TYPE_COMMENTS
        old_tree = compile(old_bytes, filename, "exec", flags=flags, dont_inherit=True, optimize=0)
        new_tree = compile(new_bytes, filename, "exec", flags=flags, dont_inherit=True, optimize=0)
        if ast.dump(old_tree) != ast.dump(new_tree):
            return None
        old_code = compile(old_bytes, filename, "exec", dont_inherit=True, optimize=0)
        new_code = compile(new_bytes, filename, "exec", dont_inherit=True, optimize=0)
        locations_changed = (
            _code_signature(old_code, True) != _code_signature(new_code, True)
            or ast.dump(old_tree, include_attributes=True) != ast.dump(new_tree, include_attributes=True)
        )
        if _code_signature(old_code) != _code_signature(new_code):
            # CPython 3.13 emits a class's source line into its constant pool
            # for __firstlineno__. Replay the identical AST at the original
            # coordinates to distinguish that from an instruction change.
            for old_node, new_node in zip(ast.walk(old_tree), ast.walk(new_tree)):
                for name in old_node._attributes:
                    if hasattr(old_node, name):
                        setattr(new_node, name, getattr(old_node, name))
            relocated = compile(new_tree, filename, "exec", dont_inherit=True, optimize=0)
            if _code_signature(old_code, True) != _code_signature(relocated, True):
                return None
        return PythonEquivalence(locations_changed)
    except (SyntaxError, UnicodeError, LookupError, ValueError, TypeError, tokenize.TokenError,
            RecursionError, MemoryError, OverflowError):
        return None


def observer_lines(source: str) -> set[int]:
    """Find known project source/position observers for the baseline dependency map.

    This deliberately does not classify ordinary globals/locals or every getattr
    as observers. Dynamic reflection and external library observations remain an
    operational prediction risk, documented by the caller rather than attested
    away by an unavailable formal certificate.
    """
    tree = ast.parse(source)
    apis = set(_SOURCE_APIS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _OBSERVER_MODULES | {"sys"}:
            apis.update(alias.asname or alias.name for alias in node.names
                        if alias.name in _SOURCE_APIS)
    lines = set()
    for node in ast.walk(tree):
        sensitive = isinstance(node, ast.Attribute) and (
            node.attr in _POSITION_ATTRS or node.attr in _SOURCE_APIS
        )
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in apis:
            sensitive = True
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in {"getattr", "setattr", "delattr"}
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _POSITION_ATTRS):
            sensitive = True
        if sensitive:
            lines.add(node.lineno)
    return lines
