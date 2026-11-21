import hashlib
from pathlib import Path
import tempfile
import unittest

from tools.refine_coverage import fingerprint


def record(data):
    return {"sha256": hashlib.sha256(data).hexdigest()}


class CheckoutFingerprintTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root / "README.md"
        self.source.write_bytes(b"source\n")
        self.link = self.root / "guide"
        try:
            self.link.symlink_to("README.md")
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symlinks unavailable in this checkout: {exc}")
        self.map = {"paths": {"README.md": record(b"source\n"), "guide": record(b"README.md")}}

    def test_real_and_materialized_link_blobs_match_the_same_map(self):
        archive_stamp = fingerprint(self.root, self.map)
        self.link.unlink()
        self.link.write_bytes(b"README.md")
        self.assertEqual(fingerprint(self.root, self.map), archive_stamp)

    def test_map_from_a_native_checkout_can_record_the_dereferenced_bytes(self):
        self.map["paths"]["guide"] = record(b"source\n")
        self.assertTrue(fingerprint(self.root, self.map))

    def test_modified_target_is_still_rejected(self):
        self.source.write_bytes(b"changed\n")
        with self.assertRaises(ValueError):
            fingerprint(self.root, self.map)

    def test_materialized_link_hash_requires_a_separately_verified_target(self):
        del self.map["paths"]["README.md"]
        with self.assertRaises(ValueError):
            fingerprint(self.root, self.map)

    def test_changed_link_target_is_rejected_even_if_contents_match(self):
        (self.root / "another").write_bytes(self.source.read_bytes())
        self.map["paths"]["another"] = record(b"source\n")
        self.link.unlink()
        self.link.symlink_to("another")
        with self.assertRaises(ValueError):
            fingerprint(self.root, self.map)
