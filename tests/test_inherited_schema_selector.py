import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from inherited_schema import METHOD, InheritedSchemaSelector
from m2_dispatch import BASE_COMMIT


SOURCE = (
    "class Base:\n"
    "    arg_types = {'z': True, 'a': False}\n"
    "\n"
    "class Derived(Base):\n"
    "    pass  # retained source comment\n"
)
PATH = "sqlglot/expressions/example.py"
TESTS = [{"number": 0, "id": "test_one"}, {"number": 1, "id": "test_two"}]
VALID = "arg_types = {'z': False, 'a': False}"


class InheritedSchemaSelectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.coverage = directory / "coverage.gz"
        self.coverage.write_bytes(b"fixture coverage")
        self.asset = directory / "inherited_schema.json.gz"

    def load(self, source=SOURCE, schema=None, readers=None, tests=TESTS, **context_overrides):
        digest = hashlib.sha256(self.coverage.read_bytes()).hexdigest()
        artifact = {
            "base_commit": BASE_COMMIT,
            "tests": TESTS,
            "context": {
                "method": METHOD,
                "complete": True,
                "coverage_sha256": digest,
                **context_overrides,
            },
            "paths": {
                PATH: {
                    "sha256": hashlib.sha256(source.encode()).hexdigest(),
                    "classes": {
                        "Derived": {
                            "schema": {"z": True, "a": False} if schema is None else schema,
                            "tests": [1] if readers is None else readers,
                        },
                    },
                },
            },
        }
        self.asset.write_bytes(gzip.compress(json.dumps(artifact).encode()))
        return InheritedSchemaSelector.from_file(self.asset, tests, self.coverage)

    def test_selects_fresh_set_of_independently_observed_test_numbers(self):
        selector = self.load(tests={"tests": TESTS})
        replacement = SOURCE.replace("pass", VALID)
        picked = selector.select(PATH, SOURCE, replacement, 5)
        self.assertEqual(picked, {1})
        picked.clear()
        self.assertEqual(selector.select(PATH, SOURCE, replacement, 5), {1})
        self.assertEqual(selector.select(PATH, SOURCE, SOURCE.replace("pass", "arg_types = {'z': True, 'a': True}"), 5), {1})

    def test_requires_ordered_unique_keys_and_exactly_one_bool_flip(self):
        selector = self.load()
        for invalid in (
            "arg_types = {'a': False, 'z': False}",
            "arg_types = {'z': False, 'z': True, 'a': False}",
            "arg_types = {'z': False, 'b': False}",
            "arg_types = {'z': 0, 'a': False}",
            "arg_types = {'z': False, 'a': True}",
            "arg_types = {'z': True, 'a': False}",
            "arg_types = {'z': False, **base}",
            "arg_types = dict(z=False, a=False)",
            "arg_types = also_assigned = {'z': False, 'a': False}",
            "ａｒｇ_ｔｙｐｅｓ = {'z': False, 'a': False}",
        ):
            with self.subTest(invalid=invalid):
                self.assertIsNone(selector.select(PATH, SOURCE, SOURCE.replace("pass", invalid), 5))

    def test_requires_an_exact_change_at_the_declared_source_site(self):
        selector = self.load()
        replacement = SOURCE.replace("pass", VALID)
        for path, old, new, line in (
            ("other.py", SOURCE, replacement, 5),
            (PATH, SOURCE, replacement, 4),
            (PATH, SOURCE, replacement, True),
            (PATH, SOURCE + "\n", replacement + "\n", 5),
            (PATH, SOURCE, replacement.replace("retained", "changed"), 5),
            (PATH, SOURCE, replacement.replace("class Base", "class Other"), 5),
            (PATH, SOURCE, SOURCE.replace("pass", VALID + "\n    EXTRA = False"), 5),
            (PATH, SOURCE, SOURCE.replace("pass", VALID + "; EXTRA = False"), 5),
        ):
            with self.subTest(path=path, old=old, new=new, line=line):
                self.assertIsNone(selector.select(path, old, new, line))

    def test_declines_nontrivial_and_ambiguous_class_definitions(self):
        for source in (
            SOURCE.replace("class Derived", "@decorate\nclass Derived"),
            SOURCE.replace("Derived(Base)", "Derived(Base, metaclass=Meta)"),
            SOURCE.replace("Derived(Base)", "Derived(Base, config=True)"),
            SOURCE + "    EXTRA = False\n",
            SOURCE + "class Derived(Base):\n    pass\n",
            SOURCE.replace("class Derived(Base):\n    pass", "class Outer:\n    class Derived(Base):\n        pass"),
        ):
            with self.subTest(source=source):
                selector = self.load(source)
                line = next(i for i, text in enumerate(source.splitlines(), 1) if "pass" in text)
                self.assertIsNone(selector.select(PATH, source, source.replace("pass", VALID, 1), line))

    def test_rejects_unbound_or_invalid_evidence(self):
        for overrides in (
            {"tests": [{**TESTS[0], "id": "other"}, TESTS[1]]},
            {"tests": [TESTS[0], TESTS[0]]},
            {"coverage_sha256": "stale"},
            {"complete": False},
            {"method": "other"},
        ):
            with self.subTest(overrides=overrides):
                self.assertIsNone(self.load(**overrides))
        replacement = SOURCE.replace("pass", VALID)
        for overrides in (
            {"schema": {"z": 1, "a": False}},
            {"schema": {"a": False, "z": True}},
            {"schema": {"z": True}},
            {"readers": []},
            {"readers": [1, 1]},
            {"readers": [False]},
            {"readers": [99]},
        ):
            with self.subTest(overrides=overrides):
                self.assertIsNone(self.load(**overrides).select(PATH, SOURCE, replacement, 5))

    def test_missing_and_corrupt_assets_disable_the_optional_selector(self):
        self.assertIsNone(InheritedSchemaSelector.from_file(self.asset, TESTS, self.coverage))
        gzip_header = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"
        self.asset.write_bytes(gzip_header + b"\xff" * 20)
        self.assertIsNone(InheritedSchemaSelector.from_file(self.asset, TESTS, self.coverage))


if __name__ == "__main__":
    unittest.main()
