#!/usr/bin/env python3
"""Reproduce the difference between creating, reading and entering a function."""

from importlib.machinery import SourceFileLoader
from pathlib import Path
from tempfile import TemporaryDirectory
from types import CodeType


ORIGINAL = """events = []

def register(value):
    events.append(value)
    return value

def dormant(setting=register("created")):
    events.append("entered")
    return 17
"""


def evaluate(path, source):
    path.write_text(source, encoding="utf-8")
    loader = SourceFileLoader("cold_code_control", str(path))
    code = loader.get_code(loader.name)
    nested = next(value for value in code.co_consts
                  if type(value) is CodeType and value.co_name == "dormant")
    context = {"__name__": loader.name}
    exec(code, context)
    function = context["dormant"]
    assert function.__code__ is nested
    before_call = context["events"].copy()
    value = function()
    return before_call, nested.co_consts, context["events"], value


def main():
    with TemporaryDirectory() as work:
        root = Path(work)
        original = evaluate(root / "before.py", ORIGINAL)
        body_edit = evaluate(root / "body.py", ORIGINAL.replace("return 17", "return 31"))
        default_edit = evaluate(root / "default.py", ORIGINAL.replace('register("created")',
                                                                       'register("different")'))
    assert original[0] == body_edit[0] == ["created"]
    assert original[2] == body_edit[2] == ["created", "entered"]
    assert 17 in original[1] and 31 in body_edit[1]
    assert (original[3], body_edit[3]) == (17, 31)
    assert default_edit[0] == ["different"]
    print("A body edit preserves definition-time effects, but a direct code reader sees it before entry.")
    print("An edited default changes definition-time effects even without a function call.")


if __name__ == "__main__":
    main()
