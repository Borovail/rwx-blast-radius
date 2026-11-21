import ast
from pathlib import Path
import tempfile
import unittest

from tools.build_literal_coverage import ScalarObserver, ScalarInstrument
from tools.literal_eligibility import scalar_declarations


class ScalarCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.observer = ScalarObserver({"repo": str(self.directory), "observations": str(self.directory),
                                            "modules": {"fixture": "fixture.py"}})

    def tearDown(self):
        self.observer.finish()
        self.temporary.cleanup()

    def execute(self, source):
        tree = ast.parse(source)
        tree = ScalarInstrument("fixture.py", tree).visit(tree)
        ast.fix_missing_locations(tree)
        namespace = {"__name__": "fixture", "_br_dispatch": self.observer}
        exec(compile(tree, "fixture.py", "exec"), namespace)
        return namespace

    def test_get_of_nested_class_does_not_read_every_scalar(self):
        self.execute("class Root:\n    FLAG = True\n    COUNT = 1\n"
                     "class Child(Root):\n    pass\n"
                     "PARSER = Child.__dict__.get('Parser', None)\n")
        self.assertEqual(self.observer.reads, {})
        self.assertEqual(self.observer.blocked, set())
        self.assertEqual(self.observer.edges, {})

    def test_literal_namespace_reads_are_exact_and_not_inherited(self):
        namespace = self.execute("class Root:\n    FLAG = True\n    OTHER = 1\n"
                                 "class Child(Root):\n    pass\n"
                                 "def get():\n    return Root.__dict__.get('FLAG')\n"
                                 "def item():\n    return vars(Root)['FLAG']\n"
                                 "def inherited():\n    return Child.__dict__.get('FLAG')\n")
        self.observer.active = 3
        self.assertTrue(namespace["get"]())
        self.observer.active = 4
        self.assertTrue(namespace["item"]())
        self.observer.active = 5
        self.assertIsNone(namespace["inherited"]())
        self.assertEqual(self.observer.reads, {"fixture.py|Root|FLAG": {3, 4}})
        self.assertFalse(self.observer.blocked)

    def test_runtime_inherited_reads_include_unmapped_concrete_class(self):
        namespace = self.execute("class Root:\n    FLAG = True\n"
                                 "def get(value):\n    return value.FLAG\n")
        child = type("Foreign", (namespace["Root"],), {"__module__": "foreign"})
        self.observer.active = 7
        self.assertTrue(namespace["get"](child()))
        self.assertEqual(self.observer.reads, {"fixture.py|Root|FLAG": {7}})

    def test_actual_startup_scalar_read_keeps_hard_block(self):
        self.execute("class Root:\n    FLAG = True\n"
                     "class Consumer:\n    CACHED = Root.FLAG\n")
        self.assertIn("fixture.py|Root|FLAG", self.observer.blocked)

    def test_unknown_namespace_value_consumer_keeps_all_scalar_dependencies(self):
        self.execute("class Root:\n    FLAG = True\n    OTHER = 1\n"
                     "UNKNOWN = dict(Root.__dict__)\n")
        self.assertEqual(self.observer.blocked, {"fixture.py|Root|FLAG", "fixture.py|Root|OTHER"})

    def test_dynamic_namespace_key_retains_conservative_readers(self):
        namespace = self.execute("class Root:\n    FLAG = True\n    OTHER = 1\n"
                                 "def dynamic(key):\n    return Root.__dict__.get(key)\n")
        self.observer.active = 9
        self.assertTrue(namespace["dynamic"]("FLAG"))
        self.assertEqual(self.observer.reads, {"fixture.py|Root|FLAG": {9}, "fixture.py|Root|OTHER": {9}})

    def test_class_body_scalar_alias_is_not_lost(self):
        self.execute("class Root:\n    FLAG = True\n    COPY = FLAG\n")
        self.assertIn("fixture.py|Root|FLAG", self.observer.blocked)

    def test_namespace_default_evaluated_once_and_before_get_execution(self):
        namespace = self.execute("class Root:\n    FLAG = True\n"
                                 "COUNTER = []\n"
                                 "def default():\n    COUNTER.append(1)\n    del Root.FLAG\n    return 8\n"
                                 "def get():\n    return Root.__dict__.get('FLAG', default())\n")
        self.observer.active = 11
        self.assertEqual(namespace["get"](), 8)
        self.assertEqual(namespace["COUNTER"], [1])

    def test_metaclass_custom_namespace_is_conservative(self):
        namespace = self.execute("class Meta(type):\n"
                                 "    def __getattribute__(cls, name):\n"
                                 "        return super().__getattribute__(name)\n"
                                 "class Root(metaclass=Meta):\n    FLAG = True\n    OTHER = 1\n"
                                 "def get():\n    return Root.__dict__.get('FLAG')\n")
        self.observer.active = 14
        self.assertTrue(namespace["get"]())
        self.assertEqual(self.observer.reads, {"fixture.py|Root|FLAG": {14}, "fixture.py|Root|OTHER": {14}})

    def test_literal_declaration_scope(self):
        declarations = scalar_declarations(ast.parse(
            "class Root:\n    FLAG = True\n    OFFSET = -1\n    TEXT = 'x'\n"
            "    NONE = None\n    FLOAT = 1.0\n    TABLE = {}\n    _PRIVATE = True\n    lower = 1\n"), "fixture.py")
        self.assertEqual(set(declarations), {"fixture.py|Root|FLAG", "fixture.py|Root|OFFSET", "fixture.py|Root|TEXT"})


if __name__ == "__main__":
    unittest.main()
