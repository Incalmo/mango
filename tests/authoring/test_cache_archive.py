import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from mango.authoring.cache_archive import pin_cache_archive


class CacheArchiveTests(unittest.TestCase):
    def archive(self, path, name="metadata", kind=tarfile.REGTYPE):
        with tarfile.open(path, "w:gz") as archive:
            member = tarfile.TarInfo(name)
            member.mode = 0o600
            member.type = kind
            if kind == tarfile.REGTYPE:
                member.size = 2
                archive.addfile(member, io.BytesIO(b"{}"))
            else:
                member.linkname = "/outside"
                archive.addfile(member)

    def test_hash_metadata_and_destination_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.archive(root / "source.tar.gz")
            result = pin_cache_archive(
                root / "source.tar.gz", root / "out", prefix="npm/cache"
            )
            self.assertEqual(
                Path(result["author_source"]).read_bytes(),
                (root / "source.tar.gz").read_bytes(),
            )
            self.assertEqual(result["members"][0]["mode"], 0o600)
            self.assertEqual(
                result["path"], "npm/cache/" + result["sha256"] + ".tar.gz"
            )

    def test_unsafe_links_paths_and_size_are_rejected_without_publication(self):
        for name, kind in [
            ("../escape", tarfile.REGTYPE),
            ("/absolute", tarfile.REGTYPE),
            ("link", tarfile.SYMTYPE),
        ]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.archive(root / "source.tar.gz", name, kind)
                with self.assertRaises(ValueError):
                    pin_cache_archive(
                        root / "source.tar.gz", root / "out", prefix="npm/cache"
                    )
                self.assertFalse((root / "out").exists())

    def test_duplicate_entries_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with tarfile.open(root / "source.tar.gz", "w:gz") as archive:
                for name in ("metadata", "./metadata"):
                    archive.addfile(tarfile.TarInfo(name))
            with self.assertRaisesRegex(ValueError, "duplicate"):
                pin_cache_archive(
                    root / "source.tar.gz", root / "out", prefix="npm/cache"
                )
