import ast
import copy
import hashlib
import unittest

from m2_body import BASE_COMMIT, BodyWitnesses, comparison_site
from tools.merge_suite_body import join_witnesses


class SuiteOperandTests(unittest.TestCase):
    def setUp(self):
        self.tests = {1: "test_one", 2: "test_two", 3: "test_three"}
        self.source = "def f(x): return x is None\n"
        self.node = next(node for node in ast.walk(ast.parse(self.source)) if isinstance(node, ast.Compare))
        self.site = comparison_site(self.node)
        self.equal = [["identity"], ["identity"], True, True]
        self.other = [["scalar", "str", "a"], ["identity"], False, False]
        self.differs = [["scalar", "str", "a"], ["scalar", "str", "a"], False, False]
        self.base = {
            "version": 1, "base_commit": BASE_COMMIT, "base_fingerprint": "abc",
            "coverage_sha256": "xyz", "complete": True, "files": {},
            "tests": [{"number": n, "id": name} for n, name in self.tests.items()],
            "method": "isolated-base-identity-operands-v1", "observed_tests": [1],
        }
        self.isolated = {**self.base, "observed_tests": [1, 2, 3],
                         "method": "isolated-base-identity-operands-certified-types-v1",
                         "files": {"a.py": self.file({"tests": {"1": [self.equal], "2": [self.other]}})}}
        self.suite = {**self.base, "method": "complete-suite-base-identity-operands-v1",
                      "observed_tests": [1, 2, 3], "files": {"a.py": self.file({
                          "operator": "Is", "observations": [self.equal, self.other]})}}

    def file(self, site):
        return {"sha256": hashlib.sha256(self.source.encode()).hexdigest(),
                "sites": {self.site: {"ast": ast.dump(self.node), **site}}}

    def results(self, merged=None, operator="Eq"):
        if merged is None:
            merged = join_witnesses(self.base, self.isolated, self.suite, self.suite)
        witnesses = BodyWitnesses(merged, self.tests, "xyz")
        return witnesses.equivalent_tests("a.py", self.source, self.node, operator, {1, 2, 3})

    def test_only_attested_numbers_with_suite_observations_qualify(self):
        self.assertEqual(self.results(), {1, 2})
        self.suite["files"]["a.py"]["sites"][self.site]["observations"] = [self.equal]
        self.assertEqual(self.results(), {1})

    def test_one_different_result_in_shared_state_preserves_all_dependencies(self):
        self.suite["files"]["a.py"]["sites"][self.site]["observations"].append(self.differs)
        self.assertEqual(self.results(), set())

    def test_changing_facts_between_complete_suite_runs_preserve_dependencies(self):
        repeat = copy.deepcopy(self.suite)
        repeat["files"]["a.py"]["sites"][self.site]["observations"].append(self.differs)
        merged = join_witnesses(self.base, self.isolated, self.suite, repeat)
        self.assertEqual(self.results(merged), set())

    def test_unknown_in_either_context_preserves_dependency(self):
        self.isolated["files"]["a.py"]["sites"][self.site]["tests"]["1"].append(None)
        self.assertEqual(self.results(), {2})
        self.suite["files"]["a.py"]["sites"][self.site]["observations"].append(None)
        self.assertEqual(self.results(), set())

    def test_inverting_identity_never_removes_a_reached_test(self):
        self.assertEqual(self.results(operator="IsNot"), set())

    def test_original_isolated_evidence_survives_unobserved_suite_site(self):
        self.base["files"] = {"a.py": self.file({"tests": {"3": [self.equal]}})}
        self.suite["files"] = {}
        self.assertEqual(self.results(), {3})

    def test_unrecognized_suite_policy_and_invalid_test_numbers_are_ignored(self):
        merged = join_witnesses(self.base, self.isolated, self.suite, self.suite)
        self.assertEqual(self.results({**merged, "suite_policy": "other"}), set())
        merged["files"]["a.py"]["sites"][self.site]["suite"]["numbers"] = [True, "2"]
        self.assertEqual(self.results(merged), set())

    def test_mismatched_or_incomplete_base_cannot_be_joined(self):
        for key, value in (("observed_tests", [1, 2]), ("coverage_sha256", "bad"),
                           ("complete", False), ("base_fingerprint", "bad")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                join_witnesses(self.base, self.isolated, {**self.suite, key: value}, self.suite)
        bad = copy.deepcopy(self.isolated)
        bad["files"]["a.py"]["sites"][self.site]["ast"] = "different node"
        with self.assertRaises(ValueError):
            join_witnesses(self.base, bad, self.suite, self.suite)

    def test_legacy_isolated_snapshots_cannot_qualify_additional_tests(self):
        legacy = {**self.isolated, "method": self.base["method"]}
        with self.assertRaises(ValueError):
            join_witnesses(self.base, legacy, self.suite, self.suite)


if __name__ == "__main__":
    unittest.main()
