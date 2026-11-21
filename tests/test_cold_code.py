import gzip
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cold_code import BASE_COMMIT, POLICY, ColdCode
from observer_targets import positions_digest


PATH = "sqlglot/example.py"
SOURCE = (
    "def dormant(value=1):\n"
    "    \"\"\"Preserved documentation.\"\"\"\n"
    "    result = value + 2\n"
    "    return result\n"
    "\n"
    "def sibling():\n"
    "    return 9\n"
)
TESTS = [{"number": number, "id": f"tests/test_example.py::test_{number}"} for number in range(5)]


class ColdCodeTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.coverage = self.directory / "coverage.json.gz"
        self.coverage.write_bytes(b"pinned coverage")
        self.map_path = self.directory / "cold_code.json.gz"
        self.positions = {"version": 1, "shared": False, "tests": [2]}
        self.positions_path = self.directory / "positions.json"
        self.positions_path.write_text(json.dumps(self.positions))

    def load(self, source=SOURCE, cold=None, **overrides):
        artifact = {
            "version": 1, "policy": POLICY, "base_commit": BASE_COMMIT, "complete": True,
            "coverage_sha256": hashlib.sha256(self.coverage.read_bytes()).hexdigest(),
            "positions_sha256": positions_digest(self.positions), "tests": TESTS,
            "observers": [1], "untraced": [4], "source_observers": [3], "providers": {
                PATH: {"source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                       "cold": cold if cold is not None else [{"qualname": "dormant", "first_line": 1}]},
            },
        }
        artifact.update(overrides)
        self.map_path.write_bytes(gzip.compress(json.dumps(artifact).encode()))
        return ColdCode.from_files(self.map_path, self.coverage, TESTS)

    def select(self, changed, source=SOURCE, selector=None, **overrides):
        selector = selector or self.load(source)
        arguments = {"position_observers": {2}, "single_file": True}
        arguments.update(overrides)
        return selector.select(PATH, source, changed, {"startup_read": False, "readers": [3]}, **arguments)

    def test_general_body_change_keeps_all_observer_supplements(self):
        self.assertEqual(self.select(SOURCE.replace("value + 2", "value * 7")), {1, 2, 3, 4})
        self.assertEqual(self.select(SOURCE.replace("return result", "return False")), {1, 2, 3, 4})

    def test_a_cold_nested_closure_preserves_its_creation_and_parent(self):
        source = ("def factory(state):\n"
                  "    def inner():\n"
                  "        return state + 1\n"
                  "    return inner\n")
        cold = [{"qualname": "factory.<locals>.inner", "first_line": 2}]
        selector = self.load(source, cold)
        self.assertEqual(self.select(source.replace("state + 1", "state - 2"), source, selector), {1, 2, 3, 4})
        self.assertIsNone(self.select(source.replace("state + 1", "7"), source, selector))

    def test_call_signature_defaults_documentation_and_sibling_are_preserved(self):
        for changed in (SOURCE.replace("value=1", "value=2"),
                        SOURCE.replace("Preserved documentation.", "Changed documentation."),
                        SOURCE.replace('"""Preserved documentation."""', "'''Preserved documentation.'''"),
                        SOURCE.replace("dormant(value=1)", "dormant(value=1, extra=2)"),
                        SOURCE.replace("return 9", "return 8"),
                        SOURCE.replace("value + 2", "value + 3").replace("return 9", "return 8")):
            with self.subTest(changed=changed):
                self.assertIsNone(self.select(changed))

    def test_body_edits_cannot_change_generator_or_closure_creation(self):
        self.assertIsNone(self.select(SOURCE.replace("return result", "yield result")))
        self.assertIsNone(self.select(SOURCE.replace("result = value + 2", "result = (lambda: value + 2)")))
        self.assertIsNone(self.select(SOURCE.replace("return result", "nonlocal missing")))

    def test_layout_comments_outside_body_and_decorators_keep_fallback(self):
        for changed in (SOURCE.replace("result = value + 2", "result = value + 2\n    pass"),
                        SOURCE.replace("\ndef sibling", "\n# source changed\ndef sibling"),
                        SOURCE.replace("return 9", "return 9  # annotation")):
            with self.subTest(changed=changed):
                self.assertIsNone(self.select(changed))
        decorated = "@staticmethod\n" + SOURCE
        selector = self.load(decorated, [{"qualname": "dormant", "first_line": 2}])
        self.assertIsNone(self.select(decorated.replace("value + 2", "value + 3"), decorated, selector))

    def test_missing_or_mismatched_evidence_keeps_fallback(self):
        changed = SOURCE.replace("value + 2", "value + 3")
        self.assertIsNone(self.select(changed, selector=self.load(cold=[])))
        self.assertIsNone(self.select(changed, position_observers=None))
        self.assertIsNone(self.select(changed, single_file=False))
        self.assertIsNone(self.select(changed, selector=self.load(complete=False)))
        self.assertIsNone(self.select(changed, selector=self.load(source_observers=[])))
        self.assertIsNone(self.select(changed, selector=self.load(tests=list(reversed(TESTS)))))
        other = SOURCE.replace("value=1", "value=9")
        self.assertIsNone(self.select(other.replace("value + 2", "value + 3"), other, self.load()))

    def test_invalid_map_numbers_and_broken_gzip_are_declined(self):
        self.assertFalse(self.load(observers=[100]).ready)
        self.assertFalse(self.load(observers=[1, 1]).ready)
        self.map_path.write_bytes(b"invalid gzip")
        self.assertIsNone(ColdCode.from_files(self.map_path, self.coverage, TESTS))


if __name__ == "__main__":
    unittest.main()
