import ast
import unittest

from guards import guard_tests


class GuardTests(unittest.TestCase):
    source = "def f(x, y):\n    if x and y:\n        return 1\n    return 0\n"

    def select(self, new, startup=()):
        record = {"startup": set(startup), "lines": {2: {1, 2}}}
        return guard_tests(ast.parse(self.source), ast.parse(new), {2}, set(), record)

    def test_boolean_guard_uses_covered_header(self):
        self.assertEqual(self.select(self.source.replace("and", "or")), {1, 2})

    def test_bindings_in_other_code_and_shared_guards_fall_back(self):
        for new in (self.source.replace("x and y", "(z := y)"),
                    self.source.replace("and", "or").replace("return 0", "return 2")):
            self.assertIsNone(self.select(new))
        self.assertIsNone(self.select(self.source.replace("and", "or"), [2]))

    def test_preserves_new_tree_for_subsequent_analysis(self):
        new = ast.parse(self.source.replace("and", "or"))
        snapshot = ast.dump(new)
        guard_tests(ast.parse(self.source), new, {2}, set(), {"startup": set(), "lines": {2: {3}}})
        self.assertEqual(ast.dump(new), snapshot)
