import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from mango.authoring.apt_indexes import canonicalize, normalized_policy, uri_filename


class IndexTests(unittest.TestCase):
    mappings = [{"author_uri": "https://snapshot.ubuntu.com/ubuntu/20261112T000000Z/dists/jammy/",
                 "runtime_uri": "http://archive.ubuntu.com/ubuntu/dists/jammy/"}]

    def make(self, path, entries):
        with tarfile.open(path, "w") as archive:
            for name, data, kind in entries:
                item = tarfile.TarInfo(name)
                item.type = kind
                item.mode, item.uid, item.gid, item.mtime = 0o640, 42, 43, 123
                item.size = len(data) if kind == tarfile.REGTYPE else 0
                archive.addfile(item, io.BytesIO(data) if kind == tarfile.REGTYPE else None)

    def test_update_only_transaction(self):
        from mango.authoring import packages as p
        stage = p.PackageStage(operation="apt_update")
        request = p.LockRequest(manager="apt", base="docker.io/library/ubuntu@sha256:" + "a" * 64, sources=("deb https://example.org/ubuntu noble main",), stages=(stage,))
        script = p.apt_script(request)
        self.assertEqual(script.count("update >&2"), 1)
        self.assertIn(p.STAGE_MARKER + "0", script)
        self.assertNotIn("install ", script)
        with self.assertRaises(ValueError):
            p.PackageStage(operation="apt_update", packages=("curl",))

    def test_policy_preserves_release_priority_associations(self):
        author = "Package files:\n 500 https://snapshot.ubuntu.com/ubuntu/20261112T000000Z jammy/main amd64 Packages\n     release a=jammy,c=main\n     origin snapshot.ubuntu.com\nPinned packages:\n"
        runtime = author.replace("https://snapshot.ubuntu.com/ubuntu/20261112T000000Z", "http://archive.ubuntu.com/ubuntu").replace("origin snapshot.ubuntu.com", "origin archive.ubuntu.com")
        self.assertEqual(normalized_policy(author, self.mappings, author=True), normalized_policy(runtime, self.mappings, author=False))
        self.assertNotEqual(normalized_policy(author, self.mappings, author=True), normalized_policy(runtime.replace("500 ", "100 "), self.mappings, author=False))
        with self.assertRaises(ValueError):
            normalized_policy(author.replace("origin snapshot.ubuntu.com", "origin wrong.test"), self.mappings, author=True)

    def test_encoding(self):
        self.assertEqual(uri_filename("https://example.org/a_b/dists/stable/"),
                         "example.org_a%5fb_dists_stable_")
        for uri in ["https://user:secret@example.org/a", "https://example.org/a?token=abc", "file:/tmp/a"]:
            with self.assertRaises(ValueError):
                uri_filename(uri)

    def test_remap_bytes_owners_and_determinism(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            name = uri_filename(self.mappings[0]["author_uri"]) + "InRelease"
            self.make(root / "raw.tar", [("./", b"", tarfile.DIRTYPE),
                                        ("./" + name, b"signed release bytes", tarfile.REGTYPE)])
            first = canonicalize(root / "raw.tar", root / "one.gz", self.mappings)
            second = canonicalize(root / "raw.tar", root / "two.gz", self.mappings)
            self.assertEqual(first, second)
            with tarfile.open(root / "one.gz") as archive:
                member = archive.getmember("archive.ubuntu.com_ubuntu_dists_jammy_InRelease")
                self.assertEqual((member.mode, member.uid, member.gid, member.mtime), (0o640, 42, 43, 0))
                self.assertEqual(archive.extractfile(member).read(), b"signed release bytes")

    def test_reject_unsafe_unknown_and_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            known = uri_filename(self.mappings[0]["author_uri"]) + "InRelease"
            for entries in [[("../escape", b"", tarfile.REGTYPE)],
                            [(known, b"", tarfile.SYMTYPE)],
                            [("unknown_InRelease", b"x", tarfile.REGTYPE)],
                            [(known, b"x", tarfile.REGTYPE), ("./" + known, b"y", tarfile.REGTYPE)]]:
                self.make(root / "raw.tar", entries)
                with self.assertRaises(ValueError):
                    canonicalize(root / "raw.tar", root / "output.gz", self.mappings)
                self.assertFalse((root / "output.gz").exists())
