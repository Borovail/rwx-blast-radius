import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from observer_targets import ObserverTargets, positions_digest
from tools.build_observer_targets import supported_code_site, validate_observation


class ObserverTargetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.coverage = self.root / "coverage.json.gz"
        self.coverage.write_bytes(gzip.compress(json.dumps({
            "base_commit": "base", "tests": [{"number": 8, "id": "tests/test_x.py::test_x"}],
            "paths": {"subject.py": {}},
        }).encode()))
        self.positions = self.root / "positions.json"
        self.positions.write_text(json.dumps({"shared": False, "tests": [8], "sites": [
            {"file": "tests/test_x.py", "line": 3, "tests": ["tests/test_x.py::test_x"]}]}))
        self.index = {
            "version": 1, "complete": True, "base_commit": "base", "failures": [],
            "coverage_sha256": hashlib.sha256(self.coverage.read_bytes()).hexdigest(),
            "positions_sha256": positions_digest(json.loads(self.positions.read_bytes())),
            "tests": [{"number": 8, "id": "tests/test_x.py::test_x", "targets": ["subject.py"], "guards_passed": True,
                       "observations": 1, "opcode_reads": 1, "sites": [{"file": "tests/test_x.py", "line": 3}]}],
        }

    def load(self):
        (self.root / "observer_targets.json").write_text(json.dumps(self.index))
        return ObserverTargets.from_files(self.coverage)

    def test_targets_are_limited_to_measured_file(self):
        result = self.load()
        self.assertEqual(result.select("subject.py"), {8})
        self.assertEqual(result.select("other.py"), set())
        selected = result.select("subject.py")
        selected.clear()
        self.assertEqual(result.select("subject.py"), {8})

    def test_corrupt_or_incomplete_index_keeps_global_fallback(self):
        variants = [
            {"complete": False}, {"coverage_sha256": "wrong"}, {"positions_sha256": "wrong"},
            {"tests": []}, {"tests": self.index["tests"] * 2}, {"base_commit": "other"}, {"failures": ["guard"]},
        ]
        for update in variants:
            original = self.index.copy()
            self.index.update(update)
            with self.subTest(update=update):
                self.assertIsNone(self.load())
            self.index = original
        for target in ("unknown.py", "../subject.py", "a\\subject.py", "/subject.py"):
            self.index["tests"][0]["targets"] = [target]
            with self.subTest(target=target):
                self.assertIsNone(self.load())

    def test_missing_and_malformed_assets_keep_global_fallback(self):
        self.assertIsNone(ObserverTargets.from_files(self.coverage))
        (self.root / "observer_targets.json").write_text("not json")
        self.assertIsNone(ObserverTargets.from_files(self.coverage))

    def test_position_digest_survives_crlf_and_json_layout(self):
        original = json.loads(self.positions.read_bytes())
        self.assertIsNotNone(self.load())
        self.positions.write_bytes((json.dumps(original, indent=2) + "\n").replace("\n", "\r\n").encode())
        self.assertEqual(self.load().select("subject.py"), {8})
        parsed_coverage = json.loads(gzip.decompress(self.coverage.read_bytes()))
        self.assertEqual(ObserverTargets.from_files(self.coverage, parsed_coverage).select("subject.py"), {8})

    def test_only_direct_code_reads_are_supported(self):
        self.assertTrue(supported_code_site("a, b = f.__code__, g.__code__\n", 1))
        for source in ("f.__code__.co_firstlineno\n", "getattr(f, '__code__')\n",
                       "inspect.getsource(f)\n", "sys._getframe().f_code\n", "x = 1\n", "f.__code__ = g.__code__\n"):
            with self.subTest(source=source):
                self.assertFalse(supported_code_site(source, 1))
        self.assertFalse(supported_code_site("from inspect import getsource as src\nf.__code__; src(f)\n", 2))

    def test_observation_guards_require_all_sites_and_clean_protocol(self):
        report = {
            "returncode": 0, "collected": ["test"], "errors": [],
            "opcode_reads": 1,
            "outcomes": [["test", phase, "passed"] for phase in ("setup", "call", "teardown")],
            "observations": [["test_x.py", 3, "subject.py"]],
        }
        self.assertEqual(validate_observation(report, "test", {("test_x.py", 3)}, {"subject.py"}), {"subject.py"})
        for update in ({"observations": []}, {"errors": ["child process"]}, {"returncode": 1}, {"opcode_reads": 2},
                       {"outcomes": report["outcomes"] + [["test", "call", "failed"]]},
                       {"collected": ["test", "extra"]}, {"observations": [["test_x.py", 3, "other.py"]]}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate_observation(dict(report, **update), "test", {("test_x.py", 3)}, {"subject.py"})

    def test_audit_observes_actual_function_origin(self):
        (self.root / "subject.py").write_text("def fn():\n    return 1\n")
        (self.root / "test_probe.py").write_text(
            "import subject\ndef test_probe():\n    code = subject.fn.__code__\n    assert code.co_name == 'fn'\n")
        request = self.root / "request.json"
        request.write_text(json.dumps({"repo": str(self.root), "test_id": "test_probe.py::test_probe", "sites": [["test_probe.py", 3]]}))
        report_path = self.root / "report.json"
        script = Path(__file__).resolve().parents[1] / "tools/build_observer_targets.py"
        result = subprocess.run([sys.executable, str(script), "--observe", str(request), "--output", str(report_path)],
                                cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["errors"], [], report)
        self.assertEqual(report["opcode_reads"], len(report["observations"]), report)
        self.assertEqual(validate_observation(report, "test_probe.py::test_probe", {("test_probe.py", 3)}, {"subject.py"}), {"subject.py"})
        # A property can expose __code__ without raising CPython's audit event.
        # Mixing it with a normal function at one site must invalidate the map.
        (self.root / "test_probe.py").write_text(
            "import subject\nclass Fake:\n    @property\n    def __code__(self):\n        return subject.fn.__code__\n"
            "def test_probe():\n    for obj in (subject.fn, Fake()):\n        code = obj.__code__\n        assert code.co_name == 'fn'\n")
        request.write_text(json.dumps({"repo": str(self.root), "test_id": "test_probe.py::test_probe", "sites": [["test_probe.py", 8]]}))
        result = subprocess.run([sys.executable, str(script), "--observe", str(request), "--output", str(report_path)],
                                cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(report_path.read_text())
        self.assertGreater(report["opcode_reads"], len(report["observations"]))
        with self.assertRaises(ValueError):
            validate_observation(report, "test_probe.py::test_probe", {("test_probe.py", 8)}, {"subject.py"})


if __name__ == "__main__":
    unittest.main()
