import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from m2_dispatch import BASE_COMMIT, DispatchSelector, declarations
from tools.build_dispatch import Instrument, Observer, merge_artifacts, refresh_dependencies


class DispatchSelectionTests(unittest.TestCase):
    def selector(self, source, rows):
        return DispatchSelector({
            "version": 1, "base_commit": BASE_COMMIT,
            "tests": [{"number": 1, "id": "one"}, {"number": 2, "id": "two"}],
            "context": {"complete": True},
            "paths": {"fixture.py": {"sha256": hashlib.sha256(source.encode()).hexdigest()}},
            "attributes": {f"fixture.py|{key}": value for key, value in rows.items()},
        })

    def test_literal_class_flag(self):
        source = "class A:\n    FLAG = True\n    def f(self):\n        return self.FLAG\n"
        selector = self.selector(source, {"A|FLAG": {"tests": [1], "blocked": False}})
        self.assertEqual(selector.select("fixture.py", source, source.replace("True", "False")), {1})

    def test_dict_schema_values_preserve_keys_and_shape(self):
        source = "class A:\n    arg_types = {'this': True}\n"
        selector = self.selector(source, {"A|arg_types": {"tests": [2], "blocked": False}})
        self.assertEqual(selector.select("fixture.py", source, source.replace("True", "False")), {2})
        self.assertIsNone(selector.select("fixture.py", source, source.replace("True}", "True, 'extra': False}")))
        self.assertIsNone(selector.select("fixture.py", source, source.replace("'this'", "'other'")))

    def test_scalar_type_changes_are_unsupported(self):
        source = "class A:\n    FLAG = True\n"
        selector = self.selector(source, {"A|FLAG": {"tests": [1], "blocked": False}})
        self.assertIsNone(selector.select("fixture.py", source, source.replace("True", "1")))

    def test_changed_arithmetic_initializer_requires_fallback(self):
        source = "class A:\n    FACTOR = 1 / 2\n"
        selector = self.selector(source, {"A|FACTOR": {"tests": [1], "blocked": False}})
        self.assertIsNone(selector.select("fixture.py", source, source.replace("1 / 2", "1 / 0")))

    def test_method_change_and_initializer_call_are_unsupported(self):
        source = "class A:\n    FLAG = dict(x=True)\n    def f(self):\n        return 1\n"
        selector = self.selector(source, {"A|FLAG": {"tests": [1], "blocked": False}})
        self.assertIsNone(selector.select("fixture.py", source, source.replace("True", "False")))
        self.assertIsNone(selector.select("fixture.py", source, source.replace("return 1", "return 2")))

    def test_blocked_source_and_incomplete_map_fallback(self):
        source = "class A:\n    FLAG = True\n"
        selector = self.selector(source, {"A|FLAG": {"tests": [], "blocked": True}})
        self.assertIsNone(selector.select("fixture.py", source, source.replace("True", "False")))
        selector.artifact["context"]["complete"] = False
        self.assertIsNone(DispatchSelector(selector.artifact).select("fixture.py", source, source.replace("True", "False")))

    def test_merge_requires_completed_protocol_and_known_children(self):
        source = "class A:\n    FLAG = True\n"
        artifact = self.selector(source, {"A|FLAG": {"tests": [1], "blocked": False}}).artifact
        artifact["context"].update({"passed": [1], "failed": [2], "missing": [], "skipped": [],
                                     "processes": 1})
        with self.assertRaises(ValueError):
            merge_artifacts([artifact])
        repair = json.loads(json.dumps(artifact))
        repair["context"].update({"passed": [2], "failed": [], "missing": [1]})
        result = merge_artifacts([artifact, repair])
        self.assertTrue(result["context"]["complete"])
        self.assertEqual(result["context"]["repaired"], [2])
        repair["context"]["unobserved_children"] = [123]
        with self.assertRaises(ValueError):
            merge_artifacts([artifact, repair])

    def test_shared_pool_widens_later_submitters_and_keeps_unrelated_process_separate(self):
        source = "class A:\n    FLAG = True\n    OTHER = True\n    PARENT = True\n"
        artifact = self.selector(source, {name: {"tests": [], "blocked": False}
                                         for name in ("A|FLAG", "A|OTHER", "A|PARENT")}).artifact
        artifact["tests"].append({"number": 3, "id": "three"})
        artifact["context"]["skipped"] = []
        coverage = {"base_commit": BASE_COMMIT, "tests": artifact["tests"],
                    "paths": artifact["paths"], "context": {"complete": True, "process_sites": [
                        {"file": "shared.py", "construction_only": True, "tests": []},
                        {"file": "shared.py", "construction_only": False, "tests": [1, 2]},
                        {"file": "unrelated.py", "construction_only": True, "tests": [3]},
                        {"file": "unrelated.py", "construction_only": False, "tests": [3]},
                    ]}}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for pid, test, key in ((10, 1, "FLAG"), (20, 3, "OTHER"), (30, None, "PARENT")):
                (directory / f"{pid}.start.json").write_text(json.dumps({"pid": pid, "test": test}))
                (directory / f"{pid}.done.json").write_text(json.dumps({
                    "reads": {f"fixture.py|A|{key}": [test or 1]}, "edges": {}, "blocked": []}))
            refreshed = refresh_dependencies(artifact, coverage, [directory])
        self.assertEqual(refreshed["attributes"]["fixture.py|A|FLAG"]["tests"], [1, 2])
        self.assertEqual(refreshed["attributes"]["fixture.py|A|OTHER"]["tests"], [3])
        self.assertEqual(refreshed["attributes"]["fixture.py|A|PARENT"]["tests"], [1])


class DispatchObservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.observer = Observer({"repo": str(root), "observations": str(root),
                                  "modules": {"fixture": "fixture.py"}})

    def tearDown(self):
        self.observer.finish()
        self.temporary.cleanup()

    def execute(self, source):
        tree = ast.parse(source)
        transformed = Instrument("fixture.py", tree).visit(tree)
        ast.fix_missing_locations(transformed)
        namespace = {"__name__": "fixture", "_br_dispatch": self.observer}
        exec(compile(transformed, "fixture.py", "exec"), namespace)
        return namespace

    def test_inherited_owner_and_override_are_distinct(self):
        namespace = self.execute("class A:\n    FLAG = True\nclass B(A):\n    pass\nclass C(A):\n    FLAG = False\n")
        self.observer.active = 1
        self.assertTrue(self.observer.getattr(namespace["B"](), "FLAG"))
        self.observer.active = 2
        self.assertFalse(self.observer.getattr(namespace["C"](), "FLAG"))
        self.assertEqual(self.observer.reads["fixture.py|A|FLAG"], {1})
        self.assertEqual(self.observer.reads["fixture.py|C|FLAG"], {2})

    @unittest.skipUnless(os.name == "posix", "production child-process instrumentation uses POSIX hooks")
    def test_spawned_interpreter_supplies_completed_attribute_trace(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "fixture.py").write_text(
                "class A:\n    FLAG = True\ndef read():\n    return A.FLAG\n", encoding="utf-8")
            observations = directory / "observations"
            observations.mkdir()
            config = {"repo": str(directory), "observations": str(observations),
                      "modules": {"fixture": "fixture.py"}, "process_hooks": True}
            config_path = directory / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            (directory / "sitecustomize.py").write_text(
                "from build_dispatch import child_install\nchild_install()\n", encoding="utf-8")
            script = (
                "import json,os,subprocess,sys\n"
                "from build_dispatch import install\n"
                "os.environ['BR_DISPATCH_TEST']='1'\n"
                f"os.environ['BR_DISPATCH_CONFIG']={str(config_path)!r}\n"
                f"observer=install(json.load(open({str(config_path)!r})))\n"
                "subprocess.run([sys.executable,'-c','import fixture; assert fixture.read()'],check=True)\n"
                "observer.finish()\n")
            environment = dict(os.environ)
            environment.pop("BR_DISPATCH_CONFIG", None)
            environment["PYTHONPATH"] = os.pathsep.join([str(directory), str(root / "tools"), str(root)])
            result = subprocess.run([sys.executable, "-c", script], env=environment,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            starts = list(observations.glob("*.start.json"))
            dones = list(observations.glob("*.done.json"))
            self.assertEqual(len(starts), 2)
            self.assertEqual(len(dones), 2)
            traces = [json.loads(path.read_text()) for path in dones]
            self.assertTrue(any(row["reads"].get("fixture.py|A|FLAG") == [1] for row in traces))
            started = {str(json.loads(path.read_text())["pid"]) for path in starts}
            children = {pid for row in traces for pid in row["children"]}
            self.assertLessEqual(children, started)
            self.assertFalse([error for row in traces for error in row["process_errors"]])

    def test_required_args_initializer_creates_class_dependency(self):
        namespace = self.execute(
            "class A:\n"
            "    arg_types = {}\n"
            "    def __init_subclass__(cls):\n"
            "        super().__init_subclass__()\n"
            "        cls.required_args = {k for k, v in cls.arg_types.items() if v}\n"
            "class B(A):\n"
            "    arg_types = {'this': True}\n")
        self.assertIn("fixture.py|B|*", self.observer.edges["fixture.py|B|arg_types"])
        self.observer.active = 1
        self.assertEqual(self.observer.getattr(namespace["B"](), "required_args"), {"this"})
        self.assertEqual(self.observer.reads["fixture.py|B|*"], {1})
        self.assertNotIn("fixture.py|B|arg_types", self.observer.blocked)

    def test_class_local_copy_creates_initializer_dependency(self):
        self.execute("class A:\n    MAP = {'x': 1}\n    DERIVED = MAP.copy()\n")
        self.assertIn("fixture.py|A|DERIVED", self.observer.edges["fixture.py|A|MAP"])

    def test_unknown_startup_observer_blocks_attribute(self):
        self.execute("class A:\n    FLAG = True\nvalue = A.FLAG\n")
        self.assertIn("fixture.py|A|FLAG", self.observer.blocked)

    def test_type_cache_does_not_invoke_custom_metaclass_hash(self):
        namespace = self.execute(
            "class Meta(type):\n"
            "    def __hash__(cls):\n"
            "        return hash(cls.__name__)\n"
            "class A(metaclass=Meta):\n"
            "    FLAG = True\n")
        self.observer.active = 1
        self.assertTrue(self.observer.getattr(namespace["A"], "FLAG"))
        self.assertEqual(self.observer.reads["fixture.py|A|FLAG"], {1})


if __name__ == "__main__":
    unittest.main()
