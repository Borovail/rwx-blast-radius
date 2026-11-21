import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from impact import BASE_COMMIT
from tools.build_position_index import build_index


class PositionIndexTests(unittest.TestCase):
    def fixture(self, root):
        source = "import inspect\ninspect.getsource(f)\nf.__code__.co_firstlineno\ninspect.signature(f)\n"
        (root / "module.py").write_bytes(source.replace("\n", "\r\n").encode())
        coverage = {
            "version": 1, "base_commit": BASE_COMMIT,
            "context": {"complete": True, "startup_untraced": False},
            "tests": [{"number": 0, "id": "a::test_source"}, {"number": 1, "id": "b::test_position"}],
            "paths": {"module.py": {
                "sha256": hashlib.sha256(source.encode()).hexdigest(), "line_count": 4,
                "lines": {"2": [0], "3": [1], "4": [1]}, "startup": [3],
            }},
        }
        path = root / "coverage.json.gz"
        path.write_bytes(gzip.compress(json.dumps(coverage).encode(), mtime=0))
        return source, path, coverage

    def test_contexts_and_startup_are_bound_to_exact_map(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, path, _ = self.fixture(root)
            index = build_index(root, path)
            self.assertEqual(index["coverage_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(index["tests"], [0, 1])
            self.assertTrue(index["shared"])
            self.assertEqual([site["line"] for site in index["sites"]], [2, 3])
            self.assertEqual([site["line"] for site in index["sites"] if site["shared"]], [3])

    def test_changed_baseline_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, path, _ = self.fixture(root)
            (root / "module.py").write_text(source + "# stale\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                build_index(root, path)

    def test_unexecuted_observers_do_not_retain_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, path, coverage = self.fixture(root)
            record = coverage["paths"]["module.py"]
            record["lines"], record["startup"] = {}, []
            path.write_bytes(gzip.compress(json.dumps(coverage).encode(), mtime=0))
            index = build_index(root, path)
            self.assertEqual(index["tests"], [])
            self.assertFalse(index["shared"])


if __name__ == "__main__":
    unittest.main()
