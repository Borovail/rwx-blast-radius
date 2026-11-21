import ast
import sys
import unittest

from handlers import callback_tests


MODULE = '''
def make(enabled):
    def handle(value):
        return value if enabled else 0
    return handle

class Root:
    HANDLERS = {
        "toggle": make(True),
        "increment": lambda x: int(
            x + 1
        ),
    }

class Child(Root):
    pass

class Copy:
    HANDLERS = {**Root.HANDLERS}
'''


def observe(source):
    namespace = {}
    context = None
    lines, startup = {}, set()

    def trace(frame, event, unused):
        if frame.f_code.co_filename == "base.py" and event == "line":
            if context is None:
                startup.add(frame.f_lineno)
            else:
                lines.setdefault(frame.f_lineno, set()).add(context)
        return trace

    original = sys.gettrace()
    sys.settrace(trace)
    try:
        exec(compile(source, "base.py", "exec", dont_inherit=True), namespace)
        for context, (cls, key) in enumerate(CASES):
            namespace[cls].HANDLERS[key](1)
    finally:
        sys.settrace(original)
    return {"lines": lines, "startup": startup, "source": source}


CASES = [("Root", "toggle"), ("Child", "toggle"), ("Copy", "increment")]


class HandlerTest(unittest.TestCase):
    def choose(self, old, new, record=None):
        return callback_tests(ast.parse(old), ast.parse(new), record or observe(old), {}.__getitem__)

    def test_observed_consumers_match_full_differential_rerun(self):
        for old, new, expected in [("make(True)", "make(False)", {0, 1}), ("x + 1", "x + 2", {2})]:
            changed = MODULE.replace(old, new)
            selected = self.choose(MODULE, changed)
            results = []
            for src in (MODULE, changed):
                ns = {}
                exec(compile(src, "base.py", "exec"), ns)
                results.append([ns[cls].HANDLERS[key](1) for cls, key in CASES])
            self.assertEqual({i for i, pair in enumerate(zip(*results)) if pair[0] != pair[1]}, expected)
            self.assertEqual(selected, expected)

    def test_foreign_factory_uses_original_implementation_lines(self):
        source = "from sqlglot.example import make\nTABLE = {'t': make(True)}\n"
        selected = callback_tests(ast.parse(source), ast.parse(source.replace("True", "False")),
                                  {}, {"sqlglot/example.py": observe(MODULE)}.__getitem__)
        self.assertEqual(selected, {0, 1})

    def test_mixed_change_and_changed_binding_use_full_fallback(self):
        for changed in (MODULE.replace("True", "False").replace("Root):", "Copy):"),
                        MODULE.replace("make(True)", "make(unknown())"),
                        MODULE.replace("make(True)", "make(other=False)")):
            self.assertIsNone(self.choose(MODULE, changed))

    def test_eager_factory_and_startup_consumer_use_full_fallback(self):
        eager = MODULE.replace("def handle(value):", "repr(enabled)\n    def handle(value):")
        self.assertIsNone(self.choose(eager, eager.replace("True", "False")))
        source = MODULE + "\nloaded = Root.HANDLERS['toggle'](1)\n"
        self.assertIsNone(self.choose(source, source.replace("True", "False")))
