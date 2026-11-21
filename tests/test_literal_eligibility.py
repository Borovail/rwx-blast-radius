from pathlib import Path
import hashlib
import tempfile
import unittest

from tools.literal_eligibility import audit, BASE_COMMIT

BASE = ("class _Dialect(type):\n"
        "    def __new__(cls, clsname, bases, attrs):\n"
        "        klass = super().__new__(cls, clsname, bases, attrs)\n"
        "        return klass\n"
        "class Dialect(metaclass=_Dialect):\n    FLAG = True\n")
CONCRETE = ("from sqlglot.dialects.dialect import Dialect\n"
            "class Concrete(Dialect):\n    FLAG = True\n    OFFSET = -1\n    TEXT = 'text'\n")


class LiteralEligibilityTests(unittest.TestCase):
    def run_audit(self, concrete=CONCRETE, extra="", base=BASE):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            files = {"sqlglot/dialects/dialect.py": base, "sqlglot/dialects/concrete.py": concrete,
                     "tests/test_custom.py": extra}
            metadata = {"base_commit": BASE_COMMIT, "files": {}}
            for path, source in files.items():
                target = repo / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source, encoding="utf-8", newline="\n")
                metadata["files"][path] = hashlib.sha256(target.read_bytes()).hexdigest()
            return audit(repo, metadata)

    def test_plain_concrete_scalars_eligible_base_excluded(self):
        report = self.run_audit()
        self.assertEqual(report["eligible_classes"], ["sqlglot.dialects.concrete.Concrete"])
        self.assertEqual(len(report["eligible_declarations"]), 3)
        self.assertEqual(report["eligible_declarations"]["sqlglot/dialects/concrete.py|Concrete|OFFSET"]["scalar_type"], "int")

    def test_decorated_or_custom_metaclass_class_is_excluded(self):
        for concrete in (CONCRETE.replace("class Concrete", "@decorator\nclass Concrete"),
                         CONCRETE.replace("Concrete(Dialect)", "Concrete(Dialect, metaclass=Custom)")):
            self.assertEqual(self.run_audit(concrete=concrete)["eligible_declarations"], {})

    def test_external_enum_scalar_is_not_eligible(self):
        report = self.run_audit(extra="from enum import Enum\nclass Foreign(Enum):\n    FLAG = True\n")
        self.assertEqual(len(report["eligible_declarations"]), 3)

    def test_custom_descendant_taints_concrete_provider(self):
        extra = ("from sqlglot.dialects.concrete import Concrete\n"
                 "class Custom(Concrete, metaclass=Meta):\n    pass\n")
        self.assertEqual(self.run_audit(extra=extra)["eligible_declarations"], {})

    def test_dynamic_concrete_descendant_taints_provider(self):
        extra = ("from sqlglot.dialects.concrete import Concrete\n"
                 "Custom = type('Custom', (Concrete,), {})\n")
        self.assertEqual(self.run_audit(extra=extra)["eligible_declarations"], {})

    def test_raw_attrs_consumer_is_rejected(self):
        base = BASE.replace("return klass", "print(attrs)\n        return klass")
        with self.assertRaisesRegex(ValueError, "additional raw attrs"):
            self.run_audit(base=base)


if __name__ == "__main__":
    unittest.main()
