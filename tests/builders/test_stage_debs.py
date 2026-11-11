"""Real .deb metadata determines offline apt archive names, never input names."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "builders/stage-debs.sh"


@unittest.skipUnless(shutil.which("dpkg-deb"), "needs dpkg-deb: run in tests/support/deb-shell.nix")
class StageDebsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root / "archives"

    def package(self, name, contents="same", version="2:1.0~rc1-3"):
        tree = self.root / (name + "-tree")
        (tree / "DEBIAN").mkdir(parents=True)
        (tree / "DEBIAN/control").write_text(
            f"Package: pinned-fixture\nVersion: {version}\nArchitecture: all\n"
            "Maintainer: Fixture <fixture@example.invalid>\nDescription: Test\n"
        )
        (tree / "payload").write_text(contents)
        output = self.root / name
        subprocess.run(
            ["dpkg-deb", "--root-owner-group", "--build", str(tree), str(output)],
            check=True,
            capture_output=True,
        )
        return output

    def stage(self, *inputs):
        return subprocess.run(
            ["sh", str(SCRIPT), str(self.destination), *map(str, inputs)],
            capture_output=True,
            text=True,
        )

    def test_epoch_tilde_and_sanitized_filename(self):
        source = self.package("unrelated-hash.deb")
        self.assertEqual(self.stage(source).returncode, 0)
        expected = self.destination / "pinned-fixture_2%3a1.0~rc1-3_all.deb"
        self.assertEqual(expected.read_bytes(), source.read_bytes())
        self.assertTrue((self.destination / "partial").is_dir())

    def test_identical_duplicates_and_repeated_call(self):
        source = self.package("first.deb")
        other = self.root / "copy.deb"
        other.write_bytes(source.read_bytes())
        self.assertEqual(self.stage(source, other).returncode, 0)
        self.assertEqual(self.stage(other).returncode, 0)
        self.assertEqual(len(list(self.destination.glob("*.deb"))), 1)

    def test_conflict_does_not_publish_partial_set(self):
        first = self.package("first.deb")
        second = self.package("second.deb", contents="different")
        result = self.stage(first, second)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicting Debian archive", result.stderr)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_invalid_archive_preserves_existing_files(self):
        source = self.package("valid.deb")
        self.assertEqual(self.stage(source).returncode, 0)
        previous = {p.name: p.read_bytes() for p in self.destination.glob("*.deb")}
        bad = self.root / "invalid.deb"
        bad.write_text("not a deb")
        self.assertNotEqual(self.stage(bad).returncode, 0)
        self.assertEqual(
            previous, {p.name: p.read_bytes() for p in self.destination.glob("*.deb")}
        )


if __name__ == "__main__":
    unittest.main()
