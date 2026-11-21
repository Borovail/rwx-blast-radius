import ast
from enum import Enum
import hashlib
import unittest

from impact import BASE_COMMIT
from m2_body import BodyWitnesses, comparison_changes, comparison_site, narrow_body, observed_outcome
from tools.build_body_worker import Instrumenter, _snapshot


class BodyTests(unittest.TestCase):
    def setUp(self):
        self.old = "def f(x):\n    if x is None:\n        return 1\n    return 2\n"
        self.new = self.old.replace("is None", "== None")
        self.before, self.after = ast.parse(self.old), ast.parse(self.new)
        self.node = next(n for n in ast.walk(self.before) if isinstance(n, ast.Compare))
        self.record = {"lines": {2: {1, 2}, 3: {1}, 4: {2, 3}}, "startup": set()}
        self.tests = {1: "a", 2: "b", 3: "c"}

    def witnesses(self, observations, source=None):
        return BodyWitnesses({"version": 1, "base_commit": BASE_COMMIT, "complete": True,
                              "tests": [{"number": n, "id": i} for n, i in self.tests.items()],
                              "files": {"a.py": {"sha256": hashlib.sha256((source or self.old).encode()).hexdigest(),
                                          "sites": {comparison_site(self.node): {
                                              "ast": ast.dump(self.node), "tests": observations}}}}}, self.tests)

    def observation(self, left, right, result):
        a, b = _snapshot(left), _snapshot(right)
        return [list(a[1]), list(b[1]), a[0] == b[0], result]

    def narrow(self, witness=None):
        return narrow_body("a.py", self.old, self.new, self.before, self.after, self.record, witness)

    def test_comparison_uses_expression_coverage(self):
        self.assertEqual(self.narrow(), {1, 2})

    def test_only_positive_witness_removes_test(self):
        self.assertEqual(self.narrow(self.witnesses({"1": [self.observation(None, None, True)],
                                                   "2": [None]})), {2})

    def test_missing_witness_keeps_test(self):
        self.assertEqual(self.narrow(self.witnesses({"1": [self.observation(None, None, True)]})), {2})

    def test_equal_values_with_distinct_identity_keep_test(self):
        item = [["scalar", "str", "same"], ["scalar", "str", "same"], False, False]
        self.assertEqual(self.narrow(self.witnesses({"1": [item]})), {1, 2})

    def test_original_nonidentity_operator_requires_exception_evidence(self):
        self.old, self.new = self.new, self.old
        self.before, self.after = ast.parse(self.old), ast.parse(self.new)
        self.node = next(n for n in ast.walk(self.before) if isinstance(n, ast.Compare))
        witness = self.witnesses({"1": [self.observation(None, None, True)]})
        self.assertEqual(self.narrow(witness), {1, 2})

    def test_hash_mismatch_keeps_test(self):
        self.assertEqual(self.narrow(self.witnesses({"1": [self.observation(None, None, True)]}, self.old + "\n")), {1, 2})

    def test_startup_declines(self):
        self.record["startup"].add(2)
        self.assertIsNone(self.narrow())

    def test_unobserved_expression_declines(self):
        self.record["lines"] = {4: {1, 2}}
        self.assertIsNone(self.narrow())

    def test_operand_or_binding_change_declines(self):
        self.assertIsNone(comparison_changes(self.before, ast.parse(self.new.replace("None", "0"))))
        self.assertIsNone(comparison_changes(self.before, ast.parse(self.new.replace("x ==", "y =="))))

    def test_chained_comparison_declines(self):
        self.assertIsNone(comparison_changes(ast.parse("def f(x):\n return 1 < x < 3"),
                                             ast.parse("def f(x):\n return 1 <= x < 3")))

    def test_incomplete_index_has_no_positive_evidence(self):
        self.assertFalse(BodyWitnesses({}, self.tests).equivalent("a.py", self.old, self.node, "Eq", 1))

    def test_scalar_and_enum_outcomes(self):
        class Kind(str, Enum):
            A = "a"
        for left, right in ((None, None), (object(), None), (2, 2), (Kind.A, Kind.A)):
            item = self.observation(left, right, left is right)
            self.assertEqual(observed_outcome(item, "Is"), observed_outcome(item, "Eq"))
        item = self.observation(Kind.A, "a", False)
        self.assertNotEqual(observed_outcome(item, "Is"), observed_outcome(item, "Eq"))

    def test_custom_comparison_never_executes(self):
        class Hostile:
            def __eq__(self, other):
                raise AssertionError("must not execute")
            def __ne__(self, other):
                raise AssertionError("must not execute")
        item = self.observation(Hostile(), None, False)
        self.assertIsNone(observed_outcome(item, "Eq"))
        self.assertFalse(observed_outcome(item, "Is"))

    def test_container_element_comparison_never_executes(self):
        class Hostile:
            def __eq__(self, other):
                raise AssertionError("must not execute")
        item = self.observation([Hostile()], [Hostile()], False)
        self.assertIsNone(observed_outcome(item, "Eq"))

    def test_metaclass_hook_never_executes(self):
        class Meta(type):
            def __getattribute__(self, name):
                raise AssertionError("must not execute")
        class Hostile(metaclass=Meta):
            pass
        self.assertFalse(observed_outcome(self.observation(Hostile(), None, False), "Eq"))

    def test_comparison_descriptor_hash_never_executes(self):
        class Descriptor:
            def __hash__(self):
                raise AssertionError("must not execute")
        class Hostile:
            __eq__ = Descriptor()
        self.assertIsNone(observed_outcome(self.observation(Hostile(), None, False), "Eq"))

    def test_metaclass_equality_never_executes(self):
        class Meta(type):
            def __eq__(self, other):
                raise AssertionError("must not execute")
            def __hash__(self):
                raise AssertionError("must not execute")
        class Hostile(metaclass=Meta):
            pass
        self.assertFalse(observed_outcome(self.observation(Hostile(), None, False), "Eq"))

    def test_instrumentation_evaluates_operands_once(self):
        source = "def f(a, b):\n return a() < b()\n"
        tree = ast.parse(source)
        sites, values, calls = {}, [], []
        instrumented = Instrumenter("a.py", sites).visit(tree)
        ast.fix_missing_locations(instrumented)
        def probe(path, site, side, value):
            values.append((side, value))
            return value
        namespace = {"__blast_body_operand_7a932e5__": probe}
        exec(compile(instrumented, "a.py", "exec"), namespace)
        def operand(value):
            calls.append(value)
            return value
        self.assertTrue(namespace["f"](lambda: operand(1), lambda: operand(2)))
        self.assertEqual(calls, [1, 2])
        self.assertEqual(values, [(0, 1), (1, 2), (2, True)])
        self.assertIn("Call(func=Name(id='a'", next(iter(sites.values()))["ast"])

    def test_instrumentation_global_name_survives_class_scope(self):
        source = "class C:\n def f(self, x):\n  return x is None\n"
        tree, sites = ast.parse(source), {}
        tree = Instrumenter("a.py", sites).visit(tree)
        ast.fix_missing_locations(tree)
        namespace = {"__blast_body_operand_7a932e5__": lambda path, site, side, value: value}
        exec(compile(tree, "a.py", "exec"), namespace)
        self.assertTrue(namespace["C"]().f(None))


if __name__ == "__main__":
    unittest.main()
