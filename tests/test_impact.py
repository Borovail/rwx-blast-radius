import difflib
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from impact import BASE_COMMIT, ImpactSelector


SOURCE = (
    "VALUE = 1\n"
    "\n"
    "def choose(flag):\n"
    "    if flag:\n"
    "        return VALUE\n"
    "    return 0\n"
    "\n"
    "def other():\n"
    "    return 4\n"
)
TESTS = [
    {"number": 0, "id": "tests/test_a.py::test_true", "file": "tests/test_a.py"},
    {"number": 1, "id": "tests/test_a.py::test_false", "file": "tests/test_a.py"},
    {"number": 2, "id": "tests/test_b.py::test_other", "file": "tests/test_b.py"},
    {"number": 3, "id": "tests/test_b.py::test_unrelated", "file": "tests/test_b.py"},
]


def patch(old, new, path="pkg/module.py", context=3):
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile="a/" + path, tofile="b/" + path, n=context,
        )
    )


def assets(source=SOURCE, path="pkg/module.py", lines=None, **extra):
    record = {
        "sha256": hashlib.sha256(source.encode()).hexdigest(),
        "line_count": len(source.splitlines()),
        "lines": lines if lines is not None else {"4": [0, 1], "5": [0], "6": [1], "9": [2]},
        "startup": [1, 3, 8] if path.endswith(".py") else [],
        "readers": [],
        "startup_read": False,
    }
    record.update(extra)
    return {
        "version": 1,
        "base_commit": BASE_COMMIT,
        "tests": TESTS,
        "context": {"complete": True, "startup_untraced": False, "untraced_tests": []},
        "paths": {path: record},
    }, {path: source}


class ImpactSelectorTests(unittest.TestCase):
    def selector(self, **extra):
        coverage, sources = assets(**extra)
        return ImpactSelector({"tests": TESTS}, coverage, sources)

    def test_missing_map_runs_every_test(self):
        selector = ImpactSelector(TESTS)
        self.assertEqual(selector.select(patch(SOURCE, SOURCE.replace("VALUE\n", "VALUE + 1\n"))), [0, 1, 2, 3])
        self.assertEqual(selector.select(""), [])

    def test_body_edit_selects_entire_compile_scope(self):
        # test_false never covers line 5, but adding a local binding there can
        # affect name resolution on every path through choose().
        changed = SOURCE.replace("        return VALUE", "        VALUE = 2\n        return VALUE")
        self.assertEqual(self.selector().select(patch(SOURCE, changed)), [0, 1])

    def test_other_scope_is_not_selected(self):
        self.assertEqual(self.selector().select(patch(SOURCE, SOURCE.replace("return 4", "return 5"))), [2])

    def test_comment_is_not_assumed_harmless(self):
        changed = SOURCE.replace("        return VALUE", "        return VALUE  # observed source can matter")
        self.assertEqual(self.selector().select(patch(SOURCE, changed)), [0, 1])
        self.assertEqual(self.selector().select(patch(SOURCE, "# module comment\n" + SOURCE)), [0, 1, 2, 3])

    def test_insertion_needs_two_anchors_in_same_function(self):
        middle = SOURCE.replace("        return VALUE", "        pass\n        return VALUE")
        boundary = SOURCE.replace("    return 0", "    return 0\n    pass")
        self.assertEqual(self.selector().select(patch(SOURCE, middle)), [0, 1])
        self.assertEqual(self.selector().select(patch(SOURCE, boundary)), [0, 1, 2, 3])

    def test_trivia_selection_keeps_observers_owners_readers_and_untraced(self):
        path = "tests/test_a.py"
        coverage, sources = assets(path=path, readers=[2])
        change = patch(SOURCE, "# module comment\n" + SOURCE, path)
        self.assertEqual(ImpactSelector(TESTS, coverage, sources, []).select(change), [0, 1, 2])
        self.assertEqual(ImpactSelector(TESTS, coverage, sources, [3]).select(change), [0, 1, 2, 3])
        coverage["context"]["untraced_tests"] = [3]
        self.assertEqual(ImpactSelector(TESTS, coverage, sources, []).select(change), [0, 1, 2, 3])

    def test_trivia_does_not_hide_other_files(self):
        coverage, sources = assets()
        fixture = "tests/data.sql"
        data, source = assets("SELECT 1;\n", path=fixture, lines={}, readers=[2])
        coverage["paths"].update(data["paths"])
        sources.update(source)
        change = patch(SOURCE, "# header\n" + SOURCE) + patch("SELECT 1;\n", "SELECT 2;\n", fixture)
        self.assertEqual(ImpactSelector(TESTS, coverage, sources, [1]).select(change), [1, 2])

    def test_comment_inside_literal_keeps_full_fallback(self):
        sql = 'SQL = """SELECT 1\n# old\n"""\n'
        coverage, sources = assets(sql, lines={}, startup=[1])
        change = patch(sql, sql.replace("# old", "# new"))
        self.assertEqual(ImpactSelector(TESTS, coverage, sources, []).select(change), [0, 1, 2, 3])

    def test_position_index_requires_matching_coverage_and_attributable_observers(self):
        with tempfile.TemporaryDirectory() as tmp:
            coverage = Path(tmp, "coverage.json.gz")
            coverage.write_bytes(b"baseline")
            position_path = Path(tmp, "positions.json")
            index = {"version": 1, "tests": [1], "shared": False,
                     "coverage_sha256": hashlib.sha256(b"baseline").hexdigest()}
            position_path.write_text(json.dumps(index))
            self.assertEqual(ImpactSelector.load_position_observers(coverage), [1])
            position_path.write_text(json.dumps(dict(index, shared=True)))
            self.assertIsNone(ImpactSelector.load_position_observers(coverage))
            position_path.write_text(json.dumps(index))
            coverage.write_bytes(b"other baseline")
            self.assertIsNone(ImpactSelector.load_position_observers(coverage))

    def test_import_time_or_header_edit_runs_all(self):
        for changed in (
            SOURCE.replace("VALUE = 1", "VALUE = 2"),
            SOURCE.replace("def choose(flag)", "def choose(flag=True)"),
            SOURCE.replace("def choose(flag)", "def rename(flag)"),
        ):
            with self.subTest(changed=changed):
                self.assertEqual(self.selector().select(patch(SOURCE, changed)), [0, 1, 2, 3])

    def test_inherited_schema_selection_keeps_source_readers_and_observers(self):
        path = "sqlglot/expressions/example.py"
        source = 'class Parent:\n    arg_types = {"this": False}\n\nclass Leaf(Parent):\n    pass\n'
        changed = source.replace("    pass", '    arg_types = {"this": True}')
        coverage, sources = assets(source, path, lines={}, startup=[1, 2, 4, 5], readers=[1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            coverage_path = root / "coverage.json.gz"
            coverage_path.write_bytes(gzip.compress(json.dumps(coverage).encode()))
            source_path = root / "sources.json"
            source_path.write_text(json.dumps(sources), encoding="utf-8")
            digest = hashlib.sha256(coverage_path.read_bytes()).hexdigest()
            schema = {
                "base_commit": BASE_COMMIT,
                "tests": [{"number": row["number"], "id": row["id"]} for row in TESTS],
                "context": {"complete": True, "coverage_sha256": digest,
                            "method": "inherited-argument-schemas-v1"},
                "paths": {path: {"sha256": coverage["paths"][path]["sha256"],
                                 "classes": {"Leaf": {"schema": {"this": False}, "tests": [0]}}}},
            }
            (root / "inherited_schema.json.gz").write_bytes(gzip.compress(json.dumps(schema).encode()))
            change = patch(source, changed, path)
            self.assertEqual(ImpactSelector.with_assets(TESTS, coverage_path, source_path).select(change),
                             [0, 1, 2, 3])
            positions = {"version": 1, "tests": [2], "shared": False, "coverage_sha256": digest}
            (root / "positions.json").write_text(json.dumps(positions), encoding="utf-8")
            selector = ImpactSelector.with_assets(TESTS, coverage_path, source_path)
            self.assertEqual(selector.select(change), [0, 1, 2])
            key_change = patch(source, source.replace("    pass", '    arg_types = {"other": True}'), path)
            self.assertEqual(selector.select(key_change),
                             [0, 1, 2, 3])

    def test_scope_escape_and_syntax_error_run_all(self):
        for changed in (
            SOURCE.replace("        return VALUE", "VALUE = 3"),
            SOURCE.replace("        return VALUE", "        return ("),
            SOURCE.replace("        return VALUE", "        nonlocal missing\n        return VALUE"),
        ):
            with self.subTest(changed=changed):
                self.assertEqual(self.selector().select(patch(SOURCE, changed)), [0, 1, 2, 3])

    def test_declaration_map_keeps_file_readers_and_untraced_tests_via_runtime_entry(self):
        name = "pkg/settings.py"
        source = "class Settings:\n    MODE = 1\n"
        coverage, sources = assets(source, name, lines={}, startup=[1, 2], readers=[2])
        coverage["context"]["untraced_tests"] = [3]
        artifact = {
            "version": 1, "base_commit": BASE_COMMIT, "context": {"complete": True},
            "tests": TESTS, "paths": {name: {"sha256": coverage["paths"][name]["sha256"]}},
            "attributes": {name + "|Settings|MODE": {"blocked": False, "tests": [0]}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests.json").write_text(json.dumps({"tests": TESTS}))
            (root / "sources.json").write_text(json.dumps(sources))
            (root / "coverage.json.gz").write_bytes(gzip.compress(json.dumps(coverage).encode()))
            (root / "dispatch.json.gz").write_bytes(gzip.compress(json.dumps(artifact).encode()))
            selector = ImpactSelector.from_files(root / "tests.json", root / "coverage.json.gz", root / "sources.json")
            self.assertEqual(selector.select(patch(source, source.replace("= 1", "= 2"), name)), [0, 2, 3])

    def test_function_docstrings_are_import_time_dependencies(self):
        source = 'def f():\n    """Original docstring."""\n    return 1\n'
        coverage, sources = assets(source, lines={"3": [1]}, startup=[1])
        selector = ImpactSelector(TESTS, coverage, sources)
        for changed in (
            source.replace("Original", "Changed"),
            source.replace('    """Original docstring."""\n', ""),
        ):
            self.assertEqual(selector.select(patch(source, changed)), [0, 1, 2, 3])
        self.assertEqual(selector.select(patch(source, source.replace("return 1", "return 2"))), [1])
        added = SOURCE.replace("def other():\n", 'def other():\n    """A new docstring."""\n')
        self.assertEqual(self.selector().select(patch(SOURCE, added)), [0, 1, 2, 3])

    def test_uncovered_scope_or_shared_setup_runs_all(self):
        changed = SOURCE.replace("return 4", "return 5")
        self.assertEqual(self.selector(lines={"5": [0]}).select(patch(SOURCE, changed)), [0, 1, 2, 3])
        self.assertEqual(self.selector(startup=[1, 3, 8, 9]).select(patch(SOURCE, changed)), [0, 1, 2, 3])

    def test_source_readers_and_untraced_tests_are_included(self):
        coverage, sources = assets(readers=[3])
        coverage["context"]["untraced_tests"] = [2]
        selector = ImpactSelector(TESTS, coverage, sources)
        self.assertEqual(selector.select(patch(SOURCE, SOURCE.replace("return VALUE", "return VALUE + 1"))), [0, 1, 2, 3])

    def test_fixture_dependencies_do_not_depend_on_changed_line(self):
        source = "a\nb\nc\n"
        coverage, sources = assets(source, "tests/fixtures/example.txt", lines={}, readers=[1, 3])
        selector = ImpactSelector(TESTS, coverage, sources)
        self.assertEqual(selector.select(patch(source, "new\na\nb\nc\n", "tests/fixtures/example.txt")), [1, 3])

    def test_unmapped_and_startup_read_fixtures_run_all(self):
        source = "a\n"
        for kwargs in ({}, {"readers": [1], "startup_read": True}):
            coverage, sources = assets(source, "fixture.txt", lines={}, **kwargs)
            selector = ImpactSelector(TESTS, coverage, sources)
            self.assertEqual(selector.select(patch(source, "b\n", "fixture.txt")), [0, 1, 2, 3])

    def test_unknown_files_and_incomplete_maps_run_all(self):
        changed = patch(SOURCE, SOURCE.replace("return 4", "return 5"))
        coverage, sources = assets()
        for mutate in (
            lambda c: c.update(base_commit="different"),
            lambda c: c["context"].update(complete=False),
            lambda c: c["context"].update(startup_untraced=True),
            lambda c: c["paths"]["pkg/module.py"].update(sha256="bad"),
            lambda c: c["paths"]["pkg/module.py"].update(line_count=900),
            lambda c: c["paths"]["pkg/module.py"]["lines"].update({"9": [900]}),
            lambda c: c["tests"][0].update(id="wrong test"),
            lambda c: c.pop("context"),
            lambda c: c["context"].pop("complete"),
            lambda c: c["context"].pop("untraced_tests"),
            lambda c: c["context"].pop("startup_untraced"),
            lambda c: c["paths"]["pkg/module.py"].pop("startup"),
            lambda c: c["paths"]["pkg/module.py"].pop("readers"),
            lambda c: c["paths"]["pkg/module.py"].pop("startup_read"),
        ):
            candidate = json.loads(json.dumps(coverage))
            mutate(candidate)
            self.assertEqual(ImpactSelector(TESTS, candidate, sources).select(changed), [0, 1, 2, 3])
        self.assertEqual(self.selector().select(patch(SOURCE, SOURCE.replace("return 4", "return 5"), "unknown.py")), [0, 1, 2, 3])

    def test_test_file_owners_are_included_without_test_source_coverage(self):
        coverage, sources = assets(path="tests/test_a.py", lines={})
        diff = patch(SOURCE, SOURCE.replace("return 4", "return 5"), "tests/test_a.py")
        self.assertEqual(ImpactSelector(TESTS, coverage, sources).select(diff), [0, 1])

    def test_patch_validation_checks_context_counts_and_coordinates(self):
        valid = patch(SOURCE, SOURCE.replace("return 4", "return 5"))
        for invalid in (
            valid.replace("return 4", "return 9"),
            valid.replace("@@ -6,4 +6,4 @@", "@@ -6,5 +6,4 @@"),
            valid.replace("@@ -6,4 +6,4 @@", "@@ -6,4 +5,4 @@"),
            valid + "unexpected\n",
            "GIT binary patch\nliteral 1\n",
            "diff --git a/pkg/module.py b/pkg/module.py\nold mode 100644\nnew mode 100755\n" + valid,
            valid.replace("a/pkg/module.py", "a/../module.py"),
        ):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.selector().select(invalid), [0, 1, 2, 3])

    def test_git_header_crlf_and_multiple_hunks(self):
        changed = SOURCE.replace("return VALUE", "return VALUE + 1").replace("return 4", "return 5")
        diff = "diff --git a/pkg/module.py b/pkg/module.py\nindex 123..456 100644\n" + patch(SOURCE, changed, context=0)
        self.assertEqual(self.selector().select(diff.replace("\n", "\r\n")), [0, 1, 2])

    def test_multiple_files_union_and_unknown_file_fallback(self):
        coverage, sources = assets()
        fixture_coverage, fixture_sources = assets("fixture\n", "fixture.txt", lines={}, readers=[3])
        coverage["paths"].update(fixture_coverage["paths"])
        sources.update(fixture_sources)
        selector = ImpactSelector(TESTS, coverage, sources)
        diff = patch(SOURCE, SOURCE.replace("return 4", "return 5")) + patch("fixture\n", "changed\n", "fixture.txt")
        self.assertEqual(selector.select(diff), [2, 3])
        self.assertEqual(selector.select(diff + patch("a\n", "b\n", "unknown.txt")), [0, 1, 2, 3])

    def test_no_newline_markers_and_empty_context(self):
        source = "def f():\n    return 1"
        coverage, sources = assets(source, lines={"2": [1]}, startup=[1])
        diff = "--- a/pkg/module.py\n+++ b/pkg/module.py\n@@ -2 +2 @@\n-    return 1\n\\ No newline at end of file\n+    return 2\n\\ No newline at end of file\n"
        self.assertEqual(ImpactSelector(TESTS, coverage, sources).select(diff), [1])

    def test_nested_function_selects_outer_binding_scope(self):
        source = "def outer():\n    value = 1\n    def inner():\n        return value\n    return inner\n"
        coverage, sources = assets(source, lines={"2": [0], "4": [1], "5": [2]}, startup=[1])
        diff = patch(source, source.replace("return value", "return value + 1"))
        self.assertEqual(ImpactSelector(TESTS, coverage, sources).select(diff), [0, 1, 2])

    def test_gzipped_assets_can_be_loaded(self):
        coverage, sources = assets()
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            for name, value in (("tests", {"tests": TESTS}), ("coverage", coverage), ("sources", sources)):
                with gzip.open(directory / (name + ".json.gz"), "wt", encoding="utf-8") as stream:
                    json.dump(value, stream)
            selector = ImpactSelector.from_files(*(directory / (name + ".json.gz") for name in ("tests", "coverage", "sources")))
            self.assertEqual(selector.select(patch(SOURCE, SOURCE.replace("return 4", "return 5"))), [2])

    def test_lazy_base_directory_checks_hash_and_missing_files(self):
        coverage, _ = assets()
        diff = patch(SOURCE, SOURCE.replace("return 4", "return 5"))
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            module = directory / "pkg/module.py"
            module.parent.mkdir()
            module.write_bytes(SOURCE.replace("\n", "\r\n").encode())
            self.assertEqual(ImpactSelector(TESTS, coverage, directory).select(diff), [2])
            module.write_text(SOURCE + "# wrong base\n", encoding="utf-8")
            self.assertEqual(ImpactSelector(TESTS, coverage, directory).select(diff), [0, 1, 2, 3])
            module.unlink()
            self.assertEqual(ImpactSelector(TESTS, coverage, directory).select(diff), [0, 1, 2, 3])

    def test_from_files_accepts_source_directory(self):
        coverage, _ = assets()
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            module = directory / "pkg/module.py"
            module.parent.mkdir()
            module.write_bytes(SOURCE.encode())
            (directory / "tests.json").write_text(json.dumps({"tests": TESTS}), encoding="utf-8")
            (directory / "coverage.json").write_text(json.dumps(coverage), encoding="utf-8")
            selector = ImpactSelector.from_files(directory / "tests.json", directory / "coverage.json", directory)
            self.assertEqual(selector.select(patch(SOURCE, SOURCE.replace("return 4", "return 5"))), [2])


if __name__ == "__main__":
    unittest.main()
