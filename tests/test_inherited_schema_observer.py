import ast
import inspect
import json
import operator
from pathlib import Path
import tempfile
import unittest

from tools.build_inherited_schema import GLOBAL, InheritedObservation, pass_classes, write_map
from tools.build_schema_coverage import InheritedInstrument


class FixtureBase:
    arg_types = {"this": True}
    required_args = {"this"}


class FixtureLeaf(FixtureBase):
    pass


class FixtureOverride(FixtureBase):
    arg_types = {"this": False}


class ObservationBase:
    def __init__(self, config):
        self.active = 13
        self.stack = []
        self.reads = []

    @staticmethod
    def classkey(cls):
        return cls.__name__

    def record(self, key):
        self.reads.append((key, self.active))

    def getattr(self, obj, name, *default, _allowed_initializer=False):
        return getattr(obj, name, *default)

    def validation_getattr(self, obj, name, *, _runtime_certified=False):
        return getattr(obj, name)


class Observer(InheritedObservation, ObservationBase):
    pass


class TestInheritedSchemaObserver(unittest.TestCase):
    def setUp(self):
        self.observer = Observer({"inherited_candidates": ["FixtureLeaf"]})

    def execute(self, source, **namespace):
        path = "tests/observer_fixture.py"
        tree = ast.parse(source)
        tree = InheritedInstrument(path, tree).visit(tree)
        namespace.update(_br_dispatch=self.observer)
        exec(compile(ast.fix_missing_locations(tree), path, "exec"), namespace)
        return namespace["result"]

    def test_namespace_presence_can_change_without_any_schema_value_read(self):
        lookup = type.__getattribute__
        namespace = {"lookup": lookup, "target": FixtureLeaf}
        source = 'result = "arg_types" in lookup(target, "__dict__")'
        self.assertFalse(self.execute(source, **namespace))
        self.assertEqual(self.observer.reads, [("FixtureLeaf", 13)])
        self.assertIn("FixtureLeaf", self.observer.inherited_blockers)
        self.assertTrue(self.execute(source, lookup=lookup, target=FixtureOverride))

    def test_namespace_lookups_use_real_callable_identity(self):
        for getter in (vars, dir, inspect.getmembers, inspect.getmembers_static,
                       inspect.classify_class_attrs, operator.attrgetter("__dict__")):
            with self.subTest(getter=getter):
                self.observer.reads.clear()
                self.assertEqual(self.execute("result = alias(target)", alias=getter, target=FixtureLeaf),
                                 getter(FixtureLeaf))
                self.assertEqual(self.observer.reads, [("FixtureLeaf", 13)])
        for getter in (object.__getattribute__, type.__getattribute__, inspect.getattr_static, getattr, hasattr):
            with self.subTest(getter=getter):
                self.observer.reads.clear()
                target = FixtureLeaf() if getter is object.__getattribute__ else FixtureLeaf
                result = self.execute('result = alias(target, "arg_types")', alias=getter, target=target)
                self.assertEqual(result, getter(target, "arg_types"))
                self.assertEqual(self.observer.reads, [("FixtureLeaf", 13)])

    def test_bound_getter_and_dynamic_attrgetter(self):
        getter = FixtureLeaf().__getattribute__
        self.assertIs(self.execute('result = alias("required_args")', alias=getter), FixtureLeaf.required_args)
        self.assertEqual(self.observer.reads, [("FixtureLeaf", 13)])
        host = type("Host", (), {"expr": FixtureLeaf})()
        nested = operator.attrgetter("expr.arg_types")
        self.assertIs(self.execute("result = alias(host)", alias=nested, host=host), FixtureLeaf.arg_types)
        self.assertIn(GLOBAL, self.observer.inherited_blockers)

    def test_vars_and_dir_without_arguments_keep_frame_access(self):
        self.assertEqual(self.execute("def run():\n    marker = 7\n    return vars()['marker'], 'marker' in dir()\n"
                                      "result = run()"), (7, True))
        self.assertFalse(self.observer.inherited_blockers)

    def test_exact_validator_is_kept_and_startup_receiver_blocks(self):
        required = self.observer.validation_getattr(FixtureLeaf(), "required_args", _runtime_certified=True)
        self.assertEqual(required, {"this"})
        self.assertEqual(self.observer.reads, [("FixtureLeaf", 13)])
        self.assertFalse(self.observer.inherited_blockers)
        self.observer.active = None
        self.observer.validation_getattr(FixtureLeaf(), "required_args", _runtime_certified=True)
        self.assertIn("validator before or outside numbered test",
                      self.observer.inherited_blockers["FixtureLeaf"])

    def test_exact_derivation_uses_the_inherited_value_and_rejects_subclasses(self):
        class ExactLeaf(FixtureBase):
            pass

        observer = Observer({"inherited_candidates": ["ExactLeaf"]})
        observed = observer.inherited_required_set_source(ExactLeaf, "arg_types")
        self.assertIs(observed, ExactLeaf.arg_types)
        self.assertEqual(observer.inherited_schemas, {"ExactLeaf": [["this", True]]})
        self.assertFalse(observer.inherited_blockers)

        class ExportedChild(ExactLeaf):
            pass

        observer.inherited_required_set_source(ExportedChild, "arg_types")
        self.assertEqual(observer.inherited_blockers, {"ExactLeaf": {"observed subclass"}})

    def test_only_top_level_plain_pass_classes_and_ordered_schema(self):
        path = "sqlglot/expressions/example.py"
        tree = ast.parse("class Eligible(Base): pass\n"
                         "@wrap\nclass Wrapped(Base): pass\n"
                         "class Meta(Base, metaclass=Other): pass\n"
                         "class Outer: \n    class Inner(Base): pass\n")
        self.assertEqual(pass_classes(path, tree), [f"{path}|Eligible|*"])
        self.assertFalse(pass_classes("tests/fixture.py", tree))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "inherited.json"
            write_map(output, {"schema": {"this": True, "case": False}})
            self.assertEqual(list(json.loads(output.read_text())["schema"]), ["this", "case"])


if __name__ == "__main__":
    unittest.main()
