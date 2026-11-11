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

    def test_network_guard_precedes_base_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = Path(directory) / "plan.json"
            plan.write_text(json.dumps({"requireOffline": True, "steps": []}))
            with patch(
                "rootfs.socket.if_nameindex", return_value=[(1, "lo"), (2, "eth0")]
            ):
                with self.assertRaisesRegex(RuntimeError, "no network device"):
                    rootfs.build(Namespace(plan=str(plan)))


if __name__ == "__main__":
    unittest.main()
