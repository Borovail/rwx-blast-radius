import ast
import tempfile
from pathlib import Path
import unittest

from tools.build_schema_coverage import Instrument, SchemaObserver, add_startup_safety


VALIDATOR = (
    "    def error_messages(self):\n"
    "        errors = None\n"
    "        for k in self.required_args:\n"
    "            v = self.args.get(k)\n"
    "            if v is None or (isinstance(v, list) and not v):\n"
    "                if errors is None:\n"
    "                    errors = []\n"
    "                errors.append(f\"Required keyword: '{k}' missing for {self.__class__}\")\n"
    "        return errors or []\n"
)

VALIDATED_SCHEMA = (
    "class Root:\n"
    "    arg_types = {'this': True, 'extra': False}\n"
    "    def __init_subclass__(cls):\n"
    "        cls.required_args = {k for k, v in cls.arg_types.items() if v}\n"
    "    def __init__(self, **args):\n"
    "        self.args = args\n" + VALIDATOR +
    "class Child(Root):\n    pass\n"
)


class SchemaObservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.observer = SchemaObserver({"repo": str(self.directory), "observations": str(self.directory),
                                        "modules": {"fixture": "fixture.py"}})

    def tearDown(self):
        self.observer.finish()
        self.temporary.cleanup()

    def execute(self, source):
        tree = ast.parse(source)
        tree = Instrument("fixture.py", tree).visit(tree)
        ast.fix_missing_locations(tree)
        namespace = {"__name__": "fixture", "_br_dispatch": self.observer}
        exec(compile(tree, "fixture.py", "exec"), namespace)
        return namespace

    def selected(self, key):
        pending, visited, tests = [key], set(), set()
        while pending:
            key = pending.pop()
            if key in visited:
                continue
            visited.add(key)
            tests.update(self.observer.reads.get(key, ()))
            pending.extend(self.observer.edges.get(key, ()))
        return tests, bool(visited & self.observer.blocked)

    def test_inherited_required_args_and_dict_copy_keep_providers(self):
        namespace = self.execute(
            "class Root:\n"
            "    arg_types = {'this': True}\n"
            "    def __init_subclass__(cls):\n"
            "        cls.required_args = {k for k, v in cls.arg_types.items() if v}\n"
            "class Inherited(Root):\n    pass\n"
            "class Copied(Root):\n    arg_types = {**Root.arg_types, 'extra': False}\n"
            "class Override(Root):\n    arg_types = {'that': False}\n"
            "def read(cls):\n    return cls.required_args\n")
        self.observer.active = 1
        self.assertEqual(namespace["read"](namespace["Inherited"]), {"this"})
        self.observer.active = 2
        self.assertEqual(namespace["read"](namespace["Copied"]), {"this"})
        self.observer.active = 3
        self.assertEqual(namespace["read"](namespace["Override"]), set())
        self.assertEqual(self.selected("fixture.py|Root|arg_types"), ({1, 2}, False))
        self.assertEqual(self.selected("fixture.py|Override|arg_types"), ({3}, False))

    def test_direct_values_and_namespace_inspection_are_observed(self):
        namespace = self.execute(
            "class Root:\n    arg_types = {'this': True}\n"
            "def direct():\n    return Root.arg_types['this']\n"
            "def dynamic():\n    return getattr(Root, 'arg_types')['this']\n"
            "def dictionary():\n    return vars(Root)['arg_types']['this']\n"
            "def namespace():\n    return Root.__dict__['arg_types']['this']\n")
        for number, name in enumerate(("direct", "dynamic", "dictionary", "namespace"), 1):
            self.observer.active = number
            self.assertTrue(namespace[name]())
        self.assertEqual(self.selected("fixture.py|Root|arg_types"), ({1, 2, 3, 4}, False))

    def test_plain_key_consumers_do_not_block_at_import(self):
        namespace = self.execute(
            "class Root:\n    arg_types = {'this': True}\n"
            "KEYS = set(Root.arg_types)\n"
            "SIZE = len(Root.arg_types)\n"
            "MEMBER = 'this' in Root.arg_types\n"
            "COMPREHENSION = [key for key in Root.arg_types]\n"
            "KEY_VIEW = Root.arg_types.keys()\n"
            "for key in Root.arg_types:\n    pass\n")
        self.assertEqual(namespace["SIZE"], 1)
        self.assertEqual(self.selected("fixture.py|Root|arg_types"), (set(), False))

    def test_shadowed_builtin_and_unknown_startup_value_use_block(self):
        self.execute(
            "class Root:\n    arg_types = {'this': True}\n"
            "def len(value):\n    return value['this']\n"
            "VALUE = len(Root.arg_types)\n")
        self.assertEqual(self.selected("fixture.py|Root|arg_types"), (set(), True))

    def test_values_copy_outside_class_blocks(self):
        self.execute("class Root:\n    arg_types = {'this': True}\nVALUE = dict(Root.arg_types)\n")
        self.assertEqual(self.selected("fixture.py|Root|arg_types"), (set(), True))

    def test_required_set_reads_outside_test_block(self):
        self.execute(
            "class Root:\n    arg_types = {'this': True}\n"
            "    def __init_subclass__(cls):\n"
            "        cls.required_args = {k for k, v in cls.arg_types.items() if v}\n"
            "class Child(Root):\n    pass\n"
            "VALUE = Child.required_args\n")
        self.assertEqual(self.selected("fixture.py|Root|arg_types"), (set(), True))

    def test_cached_class_template_value_read_blocks(self):
        self.execute(
            "class Root:\n    arg_types = {'this': True}\n"
            "    def __init_subclass__(cls):\n"
            "        cls.required_args = {k for k, v in cls.arg_types.items() if v}\n"
            "class Child(Root):\n    pass\n"
            "def parse_template():\n    return tuple(Child.required_args)\n"
            "class Generator:\n    TEMPLATE = parse_template()\n")
        self.assertEqual(self.selected("fixture.py|Root|arg_types"), (set(), True))

    def test_unknown_schema_consumer_in_class_body_blocks(self):
        self.execute(
            "class Root:\n    arg_types = {'this': True}\n"
            "def consume(value):\n    return value['this']\n"
            "class Generator:\n    consume(Root.arg_types)\n")
        self.assertEqual(self.selected("fixture.py|Root|arg_types"), (set(), True))

    def test_key_result_in_chained_comparison_keeps_value_dependency(self):
        self.execute(
            "class Root:\n    arg_types = {'this': True}\n"
            "VALUE = 'this' in Root.arg_types == {'this': True}\n")
        self.assertEqual(self.selected("fixture.py|Root|arg_types"), (set(), True))

    def startup_artifact(self, keys):
        self.observer.finish()
        artifact = {"attributes": {key: {"blocked": self.selected(key)[1],
                                         "tests": sorted(self.selected(key)[0])} for key in keys},
                    "context": {}}
        return add_startup_safety(artifact, [self.directory])

    def test_populated_startup_validation_retains_block_and_publishes_witness(self):
        self.execute(VALIDATED_SCHEMA +
                     "class Generator:\n"
                     "    TEMPLATE = Child(this='x', extra=False).error_messages()\n")
        key = "fixture.py|Root|arg_types"
        artifact = self.startup_artifact([key])
        self.assertTrue(artifact["attributes"][key]["blocked"])
        self.assertEqual(artifact["attributes"][key]["startup_safe_args"], ["extra", "this"])
        self.assertEqual(artifact["context"]["startup_policy"], "required-argument-witnesses-v2")

    def test_startup_intersection_includes_copied_and_inherited_consumers(self):
        self.execute(VALIDATED_SCHEMA +
                     "class Copied(Root):\n"
                     "    arg_types = {**Root.arg_types, 'other': False}\n"
                     "class Generator:\n"
                     "    ONE = Child(this='x', extra=0).error_messages()\n"
                     "    TWO = Copied(this='x', other='').error_messages()\n")
        key = "fixture.py|Root|arg_types"
        artifact = self.startup_artifact([key])
        self.assertEqual(artifact["attributes"][key]["startup_safe_args"], ["this"])

    def test_any_unknown_startup_read_overrides_populated_witness(self):
        self.execute(VALIDATED_SCHEMA +
                     "class Generator:\n"
                     "    TEMPLATE = Child(this='x', extra=1).error_messages()\n"
                     "UNKNOWN = Child.required_args\n")
        key = "fixture.py|Root|arg_types"
        self.assertNotIn("startup_safe_args", self.startup_artifact([key])["attributes"][key])

    def test_missing_baseline_required_arg_keeps_hard_block(self):
        self.execute(VALIDATED_SCHEMA +
                     "class Generator:\n"
                     "    TEMPLATE = Child(extra=1).error_messages()\n")
        key = "fixture.py|Root|arg_types"
        self.assertNotIn("startup_safe_args", self.startup_artifact([key])["attributes"][key])

    def test_runtime_validation_keeps_numbered_reader(self):
        namespace = self.execute(VALIDATED_SCHEMA)
        self.observer.active = 17
        self.assertEqual(namespace["Child"](this='x', extra=1).error_messages(), [])
        key = "fixture.py|Root|arg_types"
        self.assertEqual(self.selected(key), ({17}, False))
        self.assertEqual(self.observer.startup_validations, {})

    def test_changed_validator_and_shadowed_builtin_cannot_supply_witness(self):
        for source in (VALIDATED_SCHEMA.replace("errors.append(", "print(k)\n                errors.append("),
                       "def isinstance(value, cls):\n    return False\n" + VALIDATED_SCHEMA):
            namespace = self.execute(source)
            self.observer.stack.append("fixture.py|Consumer|cached")
            namespace["Child"](this='x', extra=1).error_messages()
            self.observer.stack.pop()
        self.assertEqual(self.observer.startup_validations, {})

    def test_populated_predicate_matches_validator_without_custom_container_calls(self):
        class Example:
            def __init__(self, args):
                self.args = args

        class StrangeList(list):
            def __len__(self):
                raise AssertionError("observer must not invoke custom list methods")

        args = {"zero": 0, "false": False, "text": "", "dict": {}, "list": [1],
                "empty": [], "none": None, "custom": StrangeList([1])}
        safe = SchemaObserver.validation_safe_args(Example(args), set())
        self.assertEqual(safe, {"zero", "false", "text", "dict", "list"})
        self.assertIsNone(SchemaObserver.validation_safe_args(Example(args), {"empty"}))

    def test_optional_custom_class_and_attribute_getters_are_never_consulted(self):
        class Example:
            def __init__(self, args):
                self.args = args

        class CustomClass:
            @property
            def __class__(self):
                raise AssertionError("observer must not inspect optional custom __class__")

        class CustomGetter:
            def __getattribute__(self, name):
                raise AssertionError("observer must not execute optional custom getter")

        class Ordinary:
            pass

        args = {"this": "x", "class": CustomClass(), "getter": CustomGetter(), "plain": Ordinary()}
        self.assertEqual(SchemaObserver.validation_safe_args(Example(args), {"this"}), {"this", "plain"})
        self.assertIsNone(SchemaObserver.validation_safe_args(Example(args), {"class"}))
        self.assertIsNone(SchemaObserver.validation_safe_args(Example(args), {"getter"}))

    def test_old_or_incomplete_protocol_has_no_startup_bypass(self):
        self.execute(VALIDATED_SCHEMA +
                     "class Generator:\n"
                     "    TEMPLATE = Child(this=1, extra=1).error_messages()\n")
        self.observer.finish()
        from tools.build_dispatch import read_json, write_json
        done = self.directory / f"{self.observer.identity}.done.json"
        data = read_json(done)
        data.pop("hard_blocked")
        write_json(done, data)
        key = "fixture.py|Root|arg_types"
        self.assertNotIn("startup_safe_args", self.startup_artifact([key])["attributes"][key])
        done.unlink()
        with self.assertRaisesRegex(ValueError, "incomplete processes"):
            self.startup_artifact([key])


class SchemaRuntimeWitnessTests(unittest.TestCase):
    setUp = SchemaObservationTests.setUp
    tearDown = SchemaObservationTests.tearDown
    execute = SchemaObservationTests.execute

    def execute_trusted(self, source, trusted_source=VALIDATED_SCHEMA):
        import hashlib
        from unittest.mock import patch
        from tools import build_schema_coverage as collector
        tree = ast.parse(trusted_source)
        owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Root")
        method = next(node for node in owner.body if isinstance(node, ast.FunctionDef)
                      and node.name == "error_messages")
        context = {"path": "fixture.py", "scope": owner.name, "method": method.name,
                   "line": method.lineno, "column": method.col_offset,
                   "sha256": hashlib.sha256(ast.dump(method, include_attributes=False).encode()).hexdigest()}
        with patch.object(collector, "_VALIDATION_METHOD_CONTEXT", context):
            return self.execute(source)

    def test_inherited_pinned_method_records_per_test_safe_intersections(self):
        namespace = self.execute_trusted(VALIDATED_SCHEMA)
        self.observer.active = 1
        self.assertEqual(namespace["Child"](this=False, extra=0).error_messages(), [])
        self.assertEqual(namespace["Child"](this="", extra=None).error_messages(), [])
        self.observer.active = 2
        self.assertEqual(namespace["Child"](this=1, extra=[0]).error_messages(), [])
        self.assertFalse(self.observer.opaque_reads)
        self.assertTrue(self.observer.runtime_validations)
        for proofs in self.observer.runtime_validations.values():
            self.assertEqual(proofs[1], {"this"})
            self.assertEqual(proofs[2], {"this", "extra"})

    def test_missing_original_required_argument_is_opaque(self):
        namespace = self.execute_trusted(VALIDATED_SCHEMA)
        self.observer.active = 1
        self.assertTrue(namespace["Child"](extra=1).error_messages())
        self.assertFalse(self.observer.runtime_validations)
        self.assertTrue(self.observer.opaque_reads)

    def test_trailing_loop_variables_and_locals_make_method_untrusted(self):
        for tail in ("return k", "return v", "return locals()"):
            with self.subTest(tail=tail):
                self.observer.runtime_validations.clear()
                self.observer.opaque_reads.clear()
                namespace = self.execute_trusted(VALIDATED_SCHEMA.replace("return errors or []", tail))
                self.observer.active = 1
                namespace["Child"](this=1, extra=1).error_messages()
                self.assertFalse(self.observer.runtime_validations)
                self.assertTrue(self.observer.opaque_reads)
                self.observer.active = None

    def test_copied_override_of_exact_loop_is_opaque(self):
        namespace = self.execute_trusted(VALIDATED_SCHEMA + "class Override(Child):\n" + VALIDATOR)
        self.observer.active = 1
        self.assertEqual(namespace["Override"](this=1, extra=1).error_messages(), [])
        self.assertFalse(self.observer.runtime_validations)
        self.assertTrue(self.observer.opaque_reads)

    def test_earlier_assertion_and_other_readers_remain_opaque(self):
        # Even a trusted complete method must retain its separate value reader.
        source = VALIDATED_SCHEMA.replace("        errors = None\n",
            "        assert self.arg_types['this'] is True\n        errors = None\n")
        namespace = self.execute_trusted(source, trusted_source=source)
        self.observer.active = 1
        self.assertEqual(namespace["Child"](this=1, extra=1).error_messages(), [])
        self.assertTrue(self.observer.runtime_validations)
        self.assertEqual(self.observer.opaque_reads["fixture.py|Root|arg_types"], {1})

    def test_override_calling_super_and_inspecting_required_set_is_opaque(self):
        source = VALIDATED_SCHEMA + (
            "class Override(Child):\n"
            "    def error_messages(self):\n"
            "        snapshot = tuple(self.required_args)\n"
            "        return super().error_messages()\n")
        namespace = self.execute_trusted(source)
        self.observer.active = 1
        self.assertEqual(namespace["Override"](this=1, extra=1).error_messages(), [])
        self.assertTrue(self.observer.runtime_validations)
        self.assertTrue(self.observer.opaque_reads)

    def test_proof_merge_includes_copy_and_every_runtime_consumer(self):
        from tools.build_schema_coverage import add_runtime_safety
        source = VALIDATED_SCHEMA + (
            "class Copied(Child):\n"
            "    arg_types = {**Root.arg_types, 'tail': False}\n"
            "def opaque():\n    return Root.arg_types['extra']\n")
        namespace = self.execute_trusted(source)
        self.observer.active = 1
        namespace["Copied"](this=1, extra=1).error_messages()
        self.observer.active = 2
        namespace["Copied"](this=1, extra=1).error_messages()
        namespace["opaque"]()
        self.observer.finish()
        artifact = {"tests": [{"number": 1}, {"number": 2}],
                    "attributes": {"fixture.py|Root|arg_types": {"tests": [1, 2], "blocked": False}},
                    "context": {"skipped": [], "shared_pool_groups": []}}
        add_runtime_safety(artifact, [self.directory])
        groups = artifact["attributes"]["fixture.py|Root|arg_types"]["always_present"]
        self.assertEqual(groups, [{"args": ["extra", "this"], "tests": [1]}])


class SchemaRuntimeMergeTests(unittest.TestCase):
    def artifact(self):
        return {"tests": [{"number": n} for n in (1, 2, 3)],
                "attributes": {"base|arg_types": {"tests": [1, 2, 3], "blocked": False}},
                "context": {"skipped": [3], "shared_pool_groups": [{"tests": [1, 2]}]}}

    def write(self, directory, data, *, child=1):
        from tools.build_schema_coverage import RUNTIME_POLICY
        import json
        Path(directory, "process.start.json").write_text(json.dumps({"test": child}), encoding="utf-8")
        Path(directory, "process.done.json").write_text(json.dumps({
            "runtime_policy": RUNTIME_POLICY, "runtime_validations": {}, "opaque_reads": {},
            "reads": {}, "edges": {}, "process_errors": [], **data}), encoding="utf-8")

    def test_child_safe_and_opaque_evidence_both_follow_shared_pool(self):
        from tools.build_schema_coverage import add_runtime_safety
        for opaque in (False, True):
            with self.subTest(opaque=opaque), tempfile.TemporaryDirectory() as directory:
                artifact = self.artifact()
                self.write(directory, {"edges": {"base|arg_types": ["reader"]},
                    "reads": {"reader": [1]}, "runtime_validations": {"reader": {"1": ["this"]}},
                    "opaque_reads": {"reader": [1]} if opaque else {}})
                add_runtime_safety(artifact, [directory])
                row = artifact["attributes"]["base|arg_types"]
                self.assertEqual(row.get("always_present", []), [] if opaque else
                                 [{"args": ["this"], "tests": [1, 2]}])

    def test_unwitnessed_reachable_consumer_prevents_omission(self):
        from tools.build_schema_coverage import add_runtime_safety
        with tempfile.TemporaryDirectory() as directory:
            artifact = self.artifact()
            self.write(directory, {"edges": {"base|arg_types": ["known", "unknown"]},
                "reads": {"known": [1], "unknown": [1]},
                "runtime_validations": {"known": {"1": ["this"]}}})
            add_runtime_safety(artifact, [directory])
            self.assertNotIn("always_present", artifact["attributes"]["base|arg_types"])

    def test_legacy_process_without_runtime_attestation_rejected(self):
        from tools.build_schema_coverage import add_runtime_safety
        with tempfile.TemporaryDirectory() as directory:
            self.write(directory, {"runtime_policy": "legacy"})
            with self.assertRaises(ValueError):
                add_runtime_safety(self.artifact(), [directory])


if __name__ == "__main__":
    unittest.main()
