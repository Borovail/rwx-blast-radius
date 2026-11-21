import ast
import unittest

from trivia import _code_signature, equivalent_python, observer_lines


class TriviaTests(unittest.TestCase):
    def test_compiled_frozensets_compare_by_typed_members(self):
        code = compile("pass", "module.py", "exec")
        left = code.replace(co_consts=((frozenset([-1, -2]),),))
        right = code.replace(co_consts=((frozenset([-2, -1]),),))
        self.assertEqual(_code_signature(left), _code_signature(right))
        as_bool = code.replace(co_consts=(frozenset([True]),))
        as_int = code.replace(co_consts=(frozenset([1]),))
        self.assertNotEqual(_code_signature(as_bool), _code_signature(as_int))

    def test_comment_only_same_positions(self):
        result = equivalent_python("x = 1  # old\n", "x = 1  # new\n")
        self.assertIsNotNone(result)
        self.assertFalse(result.locations_changed)

    def test_layout_positions_are_reported(self):
        source = "def f():\n    return 1\n"
        for new in ("# note\n" + source, source.replace("    return", "        return"),
                    source.replace("return 1", "return  1")):
            with self.subTest(new=new):
                result = equivalent_python(source, new)
                self.assertIsNotNone(result)
                self.assertTrue(result.locations_changed)

    def test_comment_after_multiline_literal(self):
        old = 'QUERY = """SELECT 1\n-- SQL fixture comment\n"""\n'
        self.assertIsNotNone(equivalent_python(old, old + "# Python comment\n"))
        self.assertIsNone(equivalent_python(old, old.replace("SQL fixture", "different fixture")))

    def test_class_line_metadata_moves_with_header_comments(self):
        old = "class Dialect:\n    class Tokenizer:\n        WIDTH = 2\n"
        self.assertTrue(equivalent_python(old, "# header\n" + old).locations_changed)
        self.assertEqual(observer_lines("getattr(Dialect, '__firstlineno__')\n"), {1})

    def test_literals_and_docstrings_are_preserved_exactly(self):
        for old, new in (
            ('x = "a"\n', "x = 'a'\n"),
            ('x = "# comment"\n', 'x = "# changed"\n'),
            ('"""docs"""\nx = 1\n', '"""new docs"""\nx = 1\n'),
            ('x = 1\n', 'x = 0x1\n'),
            ('x = r"\\n"\n', 'x = "\\n"\n'),
        ):
            with self.subTest(old=old, new=new):
                self.assertIsNone(equivalent_python(old, new))

    def test_significant_python_changes(self):
        for old, new in (
            ("x = 1\n", "x = 2\n"),
            ("x = True\n", "x = 1\n"),
            ("x = 1\n", "x = 1\npass\n"),
            ("def f():\n    return 1\n", "def f():\n    yield 1\n"),
            ("def f():\n    return 1\n", "def f():\n    return 1\n    pass\n"),
        ):
            with self.subTest(old=old, new=new):
                self.assertIsNone(equivalent_python(old, new))

    def test_directives_are_not_ordinary_comments(self):
        for directive in ("# type: int", "# type: ignore", "# pragma: no cover", "# noqa",
                          "# pylint: disable=all", "# fmt: off", "# doctest: +SKIP", "#!/usr/bin/python"):
            old = "x = 1  " + directive + "\n"
            for new in (old.replace(directive, "# ordinary"), "\n" + old):
                with self.subTest(directive=directive, new=new):
                    self.assertIsNone(equivalent_python(old, new))

    def test_unchanged_directive_allows_other_comment(self):
        old = "# coding: utf-8\nx = 1 # ordinary\n"
        self.assertIsNotNone(equivalent_python(old, old.replace("ordinary", "updated")))

    def test_encoding_original_bytes_are_compiled(self):
        old = b"# coding: latin-1\nx = '\xe9' # old\n"
        self.assertIsNotNone(equivalent_python(old, old.replace(b"old", b"new")))
        # Invalid cookies and mismatched source bytes cannot be repaired by an
        # AST parse on an already-decoded Python string.
        for old in (b"# coding: nonexistent\nx = 1\n", b"# coding: ascii\nx = '\xc3\xa9'\n",
                    b"# coding: utf-8\nx = '\xff'\n"):
            with self.subTest(old=old):
                self.assertIsNone(equivalent_python(old, old + b"# note\n"))
        self.assertIsNone(equivalent_python("# coding: ascii\nx = 'é'\n", "# coding: ascii\nx = 'é'\n# note\n"))

    def test_encoding_cookie_change_is_rejected(self):
        old = b"# coding: utf-8\nx = 1\n"
        self.assertIsNone(equivalent_python(old, old.replace(b"utf-8", b"ascii")))
        self.assertIsNotNone(equivalent_python(b"\xef\xbb\xbfx = 1 # old\n", b"\xef\xbb\xbfx = 1 # new\n"))

    def test_compile_rejects_ast_valid_invalid_programs(self):
        for source in ("return 1\n", "break\n", "await f()\n", "nonlocal missing\n",
                       "def f():\n    nonlocal absent\n"):
            with self.subTest(source=source):
                ast.parse(source)
                self.assertIsNone(equivalent_python(source, source + "# note\n"))

    def test_future_flags_are_preserved(self):
        old = "from __future__ import annotations\ndef f(x: Unknown):\n    return x\n"
        self.assertIsNotNone(equivalent_python(old, old + "# note\n"))
        self.assertIsNone(equivalent_python(old, old.replace("from __future__ import annotations\n", "")))
        invalid = "x = 1\nfrom __future__ import annotations\n"
        self.assertIsNone(equivalent_python(invalid, invalid + "# note\n"))

    def test_compiler_does_not_execute(self):
        source = "raise RuntimeError('must not run')\n"
        self.assertIsNotNone(equivalent_python(source, source + "# comment\n"))

    def test_malformed_token_stream_and_source_types(self):
        for source in ("x = ('unterminated\n", "x = '''unterminated\n", "x = 1\x00\n"):
            self.assertIsNone(equivalent_python(source, source + "# note\n"))
        self.assertIsNone(equivalent_python(None, "x = 1\n"))

    def test_source_and_position_observers_include_aliases(self):
        source = ("import inspect as i\nfrom sys import _getframe as frame\n"
                  "from inspect import getsource as source\n"
                  "i.getsource(f)\nframe().f_lineno\nsource(f)\nf.__code__.co_firstlineno\n"
                  "getattr(f, '__code__')\n")
        self.assertEqual(observer_lines(source), {4, 5, 6, 7, 8})

    def test_ordinary_dynamic_attribute_helpers_are_not_observers(self):
        source = "x = getattr(obj, 'sql_name')\ny = globals()\nz = obj.args.get('this')\n"
        self.assertEqual(observer_lines(source), set())

    def test_ordinary_inspect_apis_are_not_source_observers(self):
        source = ("import inspect\nimport inspect as i\n"
                  "inspect.signature(f)\ni.isclass(f)\ninspect.getfullargspec(f)\n"
                  "inspect.getmembers(f)\n")
        self.assertEqual(observer_lines(source), set())

    def test_linecache_source_read_is_observed(self):
        source = "import linecache as lc\nfrom linecache import getline as line\nlc.getline(p, 1)\nline(p, 2)\n"
        self.assertEqual(observer_lines(source), {3, 4})


if __name__ == "__main__":
    unittest.main()
