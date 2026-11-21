import ast
import tempfile
from pathlib import Path
import unittest

from tools.build_schema_coverage import Instrument, SchemaObserver


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


if __name__ == "__main__":
    unittest.main()
