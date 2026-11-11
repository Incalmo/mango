"""Disposable pinned task archives, including malformed paths and publication races."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import zstandard

from mango.authoring import task_source

#: A registry's canonical manifest format, supplied by the descriptor.
RULES = [{"header": "example-manifest-v1", "exclude": []}]


class TaskSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "task.toml").write_bytes(b"[task]\nname='synthetic'\n")
        (self.source / "script").write_bytes(b"#!/bin/sh\nexit 0\n")
        (self.source / "script").chmod(0o751)
        (self.source / "link").symlink_to("script")
        self.archive = self.root / "input.tar.zst"
        self.target = self.root / "published"
        self.write_archive()

    def write_archive(self, entries=None):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            if entries is None:
                for path in sorted(self.source.iterdir()):
                    archive.add(path, arcname=path.name, recursive=False)
            else:
                for member, content in entries:
                    archive.addfile(member, io.BytesIO(content))
        self.archive.write_bytes(zstandard.ZstdCompressor().compress(stream.getvalue()))
        self.descriptor = {
            "schema_version": 2,
            "archive_sha256": task_source.file_hash(self.archive),
            "registered_digest": task_source.canonical_digests(self.source, RULES)[0],
            "canonical": RULES,
        }

    def test_materialize_preserves_bytes_permissions_links_and_refuses_existing(self):
        self.assertTrue(
            task_source.materialize(self.archive, self.descriptor, self.target)[
                "verified"
            ]
        )
        self.assertEqual(
            (self.target / "script").read_bytes(), (self.source / "script").read_bytes()
        )
        self.assertEqual((self.target / "script").stat().st_mode & 0o777, 0o751)
        self.assertEqual(os.readlink(self.target / "link"), "script")
        self.assertEqual(
            (self.target / "script").stat().st_mtime,
            (self.source / "script").stat().st_mtime,
        )
        with self.assertRaises(FileExistsError):
            task_source.materialize(self.archive, self.descriptor, self.target)
        self.assertFalse(list(self.root.glob(".task-source-*")))

    def test_archive_and_canonical_mismatch_never_publish(self):
        for field, value in (
            ("archive_sha256", "0" * 64),
            ("registered_digest", "sha256:" + "0" * 64),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                task_source.materialize(
                    self.archive, self.descriptor | {field: value}, self.target
                )
            self.assertFalse(self.target.exists())
            self.assertFalse(list(self.root.glob(".task-source-*")))

    def test_traversal_links_duplicate_and_special_entries_rejected(self):
        for name, kind, link in [
            ("../escape", tarfile.REGTYPE, ""),
            ("/absolute", tarfile.REGTYPE, ""),
            ("link", tarfile.SYMTYPE, "../escape"),
            ("link", tarfile.SYMTYPE, "/etc/passwd"),
            ("link", tarfile.LNKTYPE, "../escape"),
            ("fifo", tarfile.FIFOTYPE, ""),
        ]:
            member = tarfile.TarInfo(name)
            member.type, member.linkname = kind, link
            self.write_archive([(member, b"")])
            with self.subTest(name=name, kind=kind), self.assertRaises(ValueError):
                task_source.materialize(self.archive, self.descriptor, self.target)
            self.assertFalse(self.target.exists())
        member = tarfile.TarInfo("same")
        self.write_archive([(member, b""), (member, b"")])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            task_source.materialize(self.archive, self.descriptor, self.target)

    def test_symlink_parent_write_rejected(self):
        link = tarfile.TarInfo("link")
        link.type, link.linkname = tarfile.SYMTYPE, "dir"
        child = tarfile.TarInfo("link/file")
        self.write_archive([(link, b""), (child, b"")])
        with self.assertRaisesRegex(ValueError, "traverses a symlink"):
            task_source.materialize(self.archive, self.descriptor, self.target)

    def test_concurrent_destination_creation_is_not_overwritten(self):
        publish = task_source.publish_no_replace

        def race(source, destination):
            destination.mkdir()
            (destination / "sentinel").write_text("concurrent owner")
            publish(source, destination)

        with (
            patch("mango.authoring.task_source.publish_no_replace", side_effect=race),
            self.assertRaises(FileExistsError),
        ):
            task_source.materialize(self.archive, self.descriptor, self.target)
        self.assertEqual((self.target / "sentinel").read_text(), "concurrent owner")
        self.assertFalse((self.target / "task.toml").exists())

    def test_descriptor_rejects_credentials_unknown_fields_and_bad_versions(self):
        path = self.root / "descriptor.json"
        for document in [
            self.descriptor | {"url": "https://invalid/secret"},
            self.descriptor | {"schema_version": 1},
            self.descriptor | {"canonical": []},
            self.descriptor | {"canonical": [{"header": "a\nb", "exclude": []}]},
            self.descriptor | {"canonical": [{"header": "h", "exclude": "x"}]},
            self.descriptor | {"canonical": [{"header": "h"}]},
            self.descriptor | {"archive_sha256": "invalid"},
        ]:
            path.write_text(json.dumps(document))
            with self.assertRaises(ValueError):
                task_source.read_descriptor(path)
        path.write_text(json.dumps(self.descriptor))
        self.assertEqual(task_source.read_descriptor(path), self.descriptor)

    def test_fixed_output_archive_reuses_explicit_store_input(self):
        archive_hash = self.descriptor["archive_sha256"]
        named = self.root / f"task-{archive_hash}.tar.zst"
        shutil.copyfile(self.archive, named)
        added = subprocess.check_output(
            ["nix-store", "--add-fixed", "sha256", str(named)],
            text=True,
        ).strip()
        harness = Path(__file__).resolve().parents[2]
        result = subprocess.check_output(
            [
                "nix-build",
                "--no-out-link",
                "--expr",
                f'(import {harness} {{}}).requireTaskArchive {{ sha256 = "{archive_hash}"; }}',
            ],
            text=True,
        ).strip()
        self.assertEqual(result, added)
        self.assertEqual(task_source.file_hash(Path(result)), archive_hash)

    def test_canonical_rules_select_header_and_excluded_paths(self):
        (self.source / "bookkeeping.json").write_text("{}")
        (self.source / "notes-a.md").write_text("a")
        (self.source / "notes").mkdir()
        (self.source / "notes/reference").write_text("reference")
        entries = []
        for path in self.source.rglob("*"):
            if path.is_dir():
                continue
            name = path.relative_to(self.source).as_posix()
            content = (
                os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
            )
            kind = "symlink" if path.is_symlink() else "file"
            mode = (
                "777"
                if path.is_symlink()
                else ("755" if path.stat().st_mode & 0o100 else "644")
            )
            entries.append(
                (
                    name,
                    f"{kind} {mode} {len(content)} {hashlib.sha256(content).hexdigest()} {name}",
                )
            )
        entries.sort()

        def digest(header, entries):
            return (
                "sha256:"
                + hashlib.sha256(
                    (
                        header + "\n" + "\n".join(row for _, row in entries) + "\n"
                    ).encode()
                ).hexdigest()
            )

        everything = {"header": "manifest-a", "exclude": []}
        self.assertEqual(
            task_source.canonical_digests(self.source, [everything]),
            (digest("manifest-a", entries),),
        )
        exclude = ["bookkeeping.json", "notes/*", "notes-*.md"]
        kept = [e for e in entries if e[0] in {"task.toml", "script", "link"}]
        self.assertEqual(
            task_source.canonical_digests(
                self.source,
                [
                    {"header": "manifest-b", "exclude": exclude},
                    {"header": "manifest-a", "exclude": exclude},
                    {"header": "manifest-b", "exclude": exclude},
                ],
            ),
            (digest("manifest-b", kept), digest("manifest-a", kept)),
        )
        descriptor = self.descriptor | {
            "registered_digest": digest("manifest-a", kept),
            "canonical": [everything, {"header": "manifest-a", "exclude": exclude}],
        }
        self.assertTrue(task_source.verify(self.source, descriptor)["verified"])
        with self.assertRaisesRegex(ValueError, "does not match"):
            task_source.verify(self.source, descriptor | {"canonical": [everything]})
