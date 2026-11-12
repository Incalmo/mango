"""The explicit Linux builder must preserve authored shell semantics."""

import json
import os
import shlex
import sys
import tarfile
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "builders"))
import rootfs


class LinuxImageTests(unittest.TestCase):
    def test_layer_paths_preserve_hidden_files(self):
        self.assertEqual(rootfs.layer_path("./.config/settings"), ".config/settings")
        self.assertEqual(rootfs.layer_path(".dockerenv"), ".dockerenv")
        for name in ("/etc/passwd", "../outside", "./dir/../outside"):
            with self.assertRaises(ValueError):
                rootfs.layer_path(name)

    def test_layer_bytes_ignore_wall_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            file = root / "file"
            file.write_text("identical bytes")
            outputs = []
            for clock in (1000, 2000):
                os.utime(file, (clock, clock))
                output = Path(directory) / f"{clock}.tar"
                rootfs.write_layer(root, {}, rootfs.snapshot(root), output, timestamp=1)
                outputs.append(output.read_bytes())
                with tarfile.open(output) as archive:
                    self.assertEqual(archive.getmember("file").mtime, 1)
            self.assertEqual(*outputs)

    def test_command_is_not_expanded_by_outer_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = (
                'value="original"; printf "%s" "$value"; printf "%s" "$(printf inner)"'
            )
            workdir = "/work with spaces"
            with patch("rootfs.subprocess.run") as run:
                rootfs.run_in_chroot(root, command, {"PATH": "/bin"}, workdir)
            args = run.call_args.args[0]
            script = args[args.index(str(root)) + 1]
            words = shlex.split(script)
            self.assertEqual(
                words, ["cd", workdir, "&&", "exec", "/bin/sh", "-c", command]
            )

    def test_xattr_only_changes_and_removals_enter_layer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'root'
            root.mkdir()
            file = root / 'file'
            file.write_bytes(b'unchanged content')
            before = rootfs.snapshot(root)
            try:
                os.setxattr(file, 'user.mango-test', b'capability-like metadata')
            except OSError as error:
                self.skipTest(f'filesystem does not support user xattrs: {error}')
            added = rootfs.snapshot(root)
            self.assertNotEqual(before, added)
            output = Path(directory) / 'added.tar'
            rootfs.write_layer(root, before, added, output)
            with tarfile.open(output) as archive:
                self.assertEqual(archive.getmember('file').pax_headers[
                    'SCHILY.xattr.user.mango-test'], 'capability-like metadata')
            os.removexattr(file, 'user.mango-test')
            removed = rootfs.snapshot(root)
            output = Path(directory) / 'removed.tar'
            rootfs.write_layer(root, added, removed, output)
            with tarfile.open(output) as archive:
                self.assertNotIn('SCHILY.xattr.user.mango-test',
                                 archive.getmember('file').pax_headers)

    def test_network_guard_precedes_base_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = Path(directory) / "plan.json"
            plan.write_text(json.dumps({"requireOffline": True, "steps": []}))
            with patch(
                "rootfs.socket.if_nameindex", return_value=[(1, "lo"), (2, "eth0")]
            ):
                with self.assertRaisesRegex(RuntimeError, "no network device"):
                    rootfs.build(Namespace(plan=str(plan)))

    def test_readonly_step_mount_restores_existing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source").mkdir()
            (root / "target").mkdir()
            (root / "target/retained").write_text("original")
            spec = {"source": "/source", "target": "/target", "readOnly": True}
            with patch("rootfs.subprocess.run") as run:
                with self.assertRaisesRegex(RuntimeError, "step failed"):
                    with rootfs.step_mount(root, spec):
                        raise RuntimeError("step failed")
            self.assertEqual([c.args[0][:3] for c in run.call_args_list], [
                ["mount", "--bind", str(root / "source")],
                ["mount", "-o", "remount,bind,ro"],
                ["umount", str(root / "target")],
            ])
            self.assertEqual((root / "target/retained").read_text(), "original")

    def test_step_mount_cleans_new_parents_and_rejects_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source").write_text("input")
            spec = {"source": "/source", "target": "/new/nested/file", "readOnly": True}
            with patch("rootfs.subprocess.run"):
                with rootfs.step_mount(root, spec):
                    self.assertTrue((root / "new/nested/file").is_file())
            self.assertFalse((root / "new").exists())
            (root / "escape").symlink_to("/tmp")
            for target in ("/", "/../outside", "/escape/file", "/proc/target", "/.nix-inputs/new"):
                with self.subTest(target=target), self.assertRaises(ValueError):
                    with rootfs.step_mount(root, dict(spec, target=target)):
                        self.fail("unsafe mount was accepted")

    def test_writable_cache_mount_does_not_remount_readonly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cache").mkdir()
            with patch("rootfs.subprocess.run") as run:
                with rootfs.step_mount(root, {
                    "source": "/cache", "target": "/new/cache", "readOnly": False,
                }):
                    (root / "new/authored-sibling").write_text("keep me")
            self.assertEqual(len(run.call_args_list), 2)
            self.assertEqual((root / "new/authored-sibling").read_text(), "keep me")
            self.assertFalse((root / "new/cache").exists())


if __name__ == "__main__":
    unittest.main()
