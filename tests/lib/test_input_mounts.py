"""Independent input mounts reuse cached bytes and clean up partial failures."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HARNESS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HARNESS / "builders"))
import rootfs


class InputMounts(unittest.TestCase):
    def test_script_edit_reuses_materialized_components(self):
        expression = r"""let h=import HARNESS {}; p=h.pkgs;
          files=p.linkFarm "stable-files" [{name="value";path=p.writeText "value" "pinned";}];
          make=script: (h.mkLinuxImage {
            name="input-reuse-test";base=p.emptyDirectory;inputs=files;
            context=p.emptyDirectory;buildScript=script;
          }).inputMounts;
          in [ (make "echo first") (make "echo second") ]""".replace(
            "HARNESS", str(HARNESS)
        )
        result = subprocess.run(
            [
                "nix-build",
                "--no-out-link",
                "--option",
                "builders",
                "",
                "--expr",
                expression,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        first, second = map(Path, result.stdout.splitlines())
        self.assertNotEqual(first, second)
        self.assertEqual((first / "files").resolve(), (second / "files").resolve())
        self.assertEqual((first / "context").resolve(), (second / "context").resolve())
        self.assertEqual((first / "files/value").read_text(), "pinned")
        self.assertFalse((first / "files/value").is_symlink())
        self.assertNotEqual(
            (first / "build.sh").read_text(), (second / "build.sh").read_text()
        )

    def test_failed_component_mount_cleans_up_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / "files").mkdir()
            (source / "build.sh").write_text("true")
            destination = Path(directory) / "root"
            destination.mkdir()

            def call(args, **kwargs):
                if args[0] == "mount" and args[-1] == str(destination / "files"):
                    raise subprocess.CalledProcessError(1, args)

            with patch("rootfs.subprocess.run", side_effect=call) as run:
                with self.assertRaises(subprocess.CalledProcessError):
                    with rootfs.mounted_inputs(source, destination):
                        self.fail("failed mount must not execute build")
                self.assertEqual(
                    run.call_args.args[0], ["umount", "-R", str(destination)]
                )
                self.assertTrue(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
