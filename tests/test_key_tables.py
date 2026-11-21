import gzip
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from key_tables import BASE_COMMIT, POLICY, KeyTables
from observer_targets import positions_digest


PATH = "sqlglot/dialects/dialect.py"
WIDTH_PATH = "sqlglot/generators/duckdb.py"
SOURCE = (
    "# č\n"
    "class Dialect:\n"
    "    SET_OP_DISTINCT_BY_DEFAULT: dict[type, bool] = {\n"
    "        exp.Except: True,\n"
    "        exp.Intersect: True,\n"
    "        exp.Union: True,\n"
    "    }\n"
)
WIDTH = "# č\n_SEQ_BYTE_WIDTH = {exp.Seq1: 1, exp.Seq2: 2, exp.Seq4: 4, exp.Seq8: 8}\n"
TESTS = [{"number": number, "id": f"tests/test_fixture.py::TestFixture::test_{number}"} for number in range(9)]
TESTS[5] = {"number": 5, "id": PATH + "::TestFixture::test_owner"}
POSITIONS = {"shared": False, "tests": [7]}


def artifact(source=SOURCE, path=PATH):
    width = path == WIDTH_PATH
    return {
        "version": 1, "policy": POLICY, "complete": True, "base_commit": BASE_COMMIT,
        "coverage_sha256": hashlib.sha256(b"coverage").hexdigest(),
        "positions_sha256": positions_digest(POSITIONS), "tests": TESTS,
        "untraced": [8], "shared_pool_groups": [[0, 3], [3, 4]],
        "providers": {path: {
            "provider": "duckdb._SEQ_BYTE_WIDTH" if width else "dialect.Dialect",
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "keys": {"Seq1": [], "Seq2": [1], "Seq4": [], "Seq8": [0]} if width else {
                "Except": [0], "Intersect": [1], "Union": [2],
            }, "blocked_keys": [],
        }},
    }


def selector(data):
    return KeyTables(data, TESTS, hashlib.sha256(b"coverage").hexdigest(), positions_digest(POSITIONS))


class KeyTableTests(unittest.TestCase):
    def select(self, old=SOURCE, new=None, path=PATH, data=None, **options):
        rule = selector(artifact(old, path) if data is None else data)
        return rule.select(path, old, new, {"readers": [6], "startup_read": False},
                           position_observers={7}, single_file=True, **options)

    def test_exact_key_readers_keep_owners_readers_observers_and_pool_closure(self):
        self.assertEqual(self.select(new=SOURCE.replace("Except: True", "Except: False")),
                         {0, 3, 4, 5, 6, 7, 8})
        self.assertEqual(self.select(new=SOURCE.replace("Intersect: True", "Intersect: False")),
                         {1, 5, 6, 7, 8})

    def test_inline_width_edit_supports_bounded_integers(self):
        self.assertEqual(self.select(old=WIDTH, new=WIDTH.replace("Seq8: 8", "Seq8: 9"), path=WIDTH_PATH),
                         {0, 3, 4, 6, 7, 8})
        for replacement in ("0", "17", "-1", "True", "9.0", "(9)", "8 + 1"):
            with self.subTest(replacement=replacement):
                self.assertIsNone(self.select(old=WIDTH, new=WIDTH.replace("Seq8: 8", "Seq8: " + replacement),
                                              path=WIDTH_PATH))
        inline_unicode = WIDTH.replace("# č\n", "prefix = 'č'; ")
        self.assertEqual(self.select(old=inline_unicode,
                                     new=inline_unicode.replace("Seq8: 8", "Seq8: 9"), path=WIDTH_PATH),
                         {0, 3, 4, 6, 7, 8})

    def test_only_one_value_token_changes_and_other_bytes_are_identical(self):
        changed = SOURCE.replace("Except: True", "Except: False")
        for candidate in (SOURCE, changed.replace("Intersect: True", "Intersect: False"),
                          changed + "# comment\n", changed.replace("# č", "# c"),
                          changed.replace("Except: False", "Except: (False)"),
                          changed.replace("exp.Except", "exp.Union"),
                          changed.replace("exp.Intersect: True,\n", ""),
                          changed.replace("# č", "# č\n"),
                          changed.replace("Except: False", "Except: None")):
            with self.subTest(candidate=candidate):
                self.assertIsNone(self.select(new=candidate))

    def test_missing_or_incomplete_facts_preserve_broad_selection(self):
        changed = SOURCE.replace("Except: True", "Except: False")
        for field, value in (("complete", False), ("version", True), ("base_commit", "other"),
                             ("policy", "other"), ("untraced", [99]),
                             ("shared_pool_groups", [[0, True]]), ("tests", TESTS[::-1])):
            data = artifact()
            data[field] = value
            with self.subTest(field=field):
                self.assertIsNone(self.select(new=changed, data=data))
        for field, value in (("keys", {"Except": [0]}), ("source_sha256", "0" * 64),
                             ("provider", "clickhouse.ClickHouse"), ("blocked_keys", ["Except"])):
            data = artifact()
            data["providers"][PATH][field] = value
            with self.subTest(field=field):
                self.assertIsNone(self.select(new=changed, data=data))

    def test_unknown_context_or_multiple_files_cannot_narrow(self):
        rule = selector(artifact())
        changed = SOURCE.replace("Except: True", "Except: False")
        for record, observers, single in (({"readers": [], "startup_read": True}, {7}, True),
                                          ({"readers": [], "startup_read": False}, None, True),
                                          ({"readers": [], "startup_read": False}, {7}, False),
                                          ({"readers": [99], "startup_read": False}, {7}, True)):
            with self.subTest(record=record, observers=observers, single=single):
                self.assertIsNone(rule.select(PATH, SOURCE, changed, record,
                                             position_observers=observers, single_file=single))

    def test_loader_binds_both_maps_and_tolerates_corrupt_gzip(self):
        with TemporaryDirectory() as directory:
            directory = Path(directory)
            coverage = directory / "coverage.json.gz"
            coverage.write_bytes(b"coverage")
            positions = directory / "positions.json"
            positions.write_text(json.dumps(POSITIONS))
            path = directory / "key_tables.json.gz"
            path.write_bytes(gzip.compress(json.dumps(artifact()).encode()))
            self.assertTrue(KeyTables.from_files(path, coverage, TESTS).ready)
            coverage.write_bytes(b"changed")
            self.assertFalse(KeyTables.from_files(path, coverage, TESTS).ready)
            path.write_bytes(b"\x1f\x8b\x08invalid")
            self.assertIsNone(KeyTables.from_files(path, coverage, TESTS))


if __name__ == "__main__":
    unittest.main()
