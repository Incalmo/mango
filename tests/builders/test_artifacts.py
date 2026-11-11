"""Explicit native stage exports preserve metadata, never traverse symlinks."""

import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "builders"))
from artifacts import export_artifacts


class ArtifactTests(unittest.TestCase):
    def test_selection_metadata_and_reproducible_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            (root / "opt/output").mkdir(parents=True)
            file = root / "opt/output/binary"
            file.write_bytes(b"native bytes")
            file.chmod(0o751)
            os.link(file, root / "opt/output/hardlink")
            (root / "opt/output/link").symlink_to("/opt/output/binary")
            (root / "compiler").write_text("not exported")
            outputs = []
            for stamp in (200, 300):
                os.utime(file, (stamp, stamp))
                output = Path(directory) / f"{stamp}.tar"
                export_artifacts(root, ["/opt/output", "/opt/output/binary"], output)
                outputs.append(output.read_bytes())
                with tarfile.open(output) as archive:
                    self.assertEqual(
                        archive.getnames(),
                        [
                            "opt/output",
                            "opt/output/binary",
                            "opt/output/hardlink",
                            "opt/output/link",
                        ],
                    )
                    self.assertTrue(archive.getmember("opt/output/hardlink").islnk())
                    self.assertEqual(
                        archive.getmember("opt/output/hardlink").linkname,
                        "opt/output/binary",
                    )
                    entry = archive.getmember("opt/output/binary")
                    self.assertEqual(
                        (entry.mode, entry.uid, entry.gid),
                        (0o751, os.getuid(), os.getgid()),
                    )
                    self.assertEqual(
                        archive.getmember("opt/output/link").linkname,
                        "/opt/output/binary",
                    )
            self.assertEqual(*outputs)

    def test_reject_paths_mounts_and_symlink_ancestors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            (root / "escape").symlink_to(directory)
            for path in [
                "/",
                "relative",
                "/../outside",
                "/opt/../bin",
                "/opt//bin",
                "/.nix-inputs/files",
                "/dev/null",
                "/proc/version",
                "/sys/kernel",
                "/escape/file",
            ]:
                with self.subTest(path=path), self.assertRaises(ValueError):
                    export_artifacts(root, [path], Path(directory) / "out.tar")
            os.mkfifo(root / "fifo")
            with self.assertRaises(ValueError):
                export_artifacts(root, ["/fifo"], Path(directory) / "out.tar")
