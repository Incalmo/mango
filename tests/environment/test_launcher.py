"""The frontend pins execution, not just the Nix expression it evaluates."""

import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mango import launcher


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "space and 'quote"
        self.root.mkdir()
        (self.root / "default.nix").write_text("{ harness }: {}")

    def test_existing_lock_survives_default_changes(self):
        pin = launcher.ensure_lock(self.root)
        before = (self.root / "mango.lock").read_bytes()
        with patch.object(launcher, "ROOT", Path("/missing/default")):
            self.assertEqual(launcher.ensure_lock(self.root), pin)
        self.assertEqual((self.root / "mango.lock").read_bytes(), before)

    def test_bad_lock_is_not_replaced(self):
        path = self.root / "mango.lock"
        for content in ("[]", '{"version": 2}', '{"version": 1, "mango": {}}', "oops"):
            path.write_text(content)
            with self.assertRaises(ValueError):
                launcher.ensure_lock(self.root)
            self.assertEqual(path.read_text(), content)

    def test_load_builds_then_loads_with_same_workspace_and_pinned_backend(self):
        with (
            patch.object(launcher, "resolve", return_value=Path("/locked/harness")),
            patch.object(
                launcher.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as run,
        ):
            with self.assertRaises(SystemExit) as result:
                launcher.main(["load", str(self.root), "--builder", "host"])
        self.assertEqual(result.exception.code, 0)
        command = run.call_args.args[0]
        self.assertEqual(
            command[:3], ["nix-shell", "/locked/harness/shell.nix", "--run"]
        )
        fetch, load = [shlex.split(part) for part in command[3].split(" && ")]
        self.assertEqual(fetch[-1], "fetch")
        self.assertEqual(load[-1], "load")
        self.assertEqual(fetch[:-1], load[:-1])
        self.assertEqual(fetch[3], str(self.root))
        self.assertEqual(fetch[5], str(self.root.parent / "mango-build" / self.root.name))
        self.assertEqual(fetch[2], "from mango.cli import main; main()")

    def test_legacy_form_keeps_flags_and_selects_lock(self):
        launcher.ensure_lock(self.root)
        args = [
            str(self.root),
            "--workspace",
            "/work dir",
            "run",
            "--agent",
            "oracle",
            "--",
            "--model",
            "example",
        ]
        with (
            patch.object(launcher, "resolve", return_value=Path("/locked")),
            patch.object(
                launcher.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 7),
            ) as run,
        ):
            with self.assertRaises(SystemExit) as result:
                launcher.main(args)
        self.assertEqual(result.exception.code, 7)
        self.assertEqual(shlex.split(run.call_args.args[0][-1])[3:], args)

    def test_resolution_passes_hash_to_nix_and_propagates_mismatch(self):
        pin = launcher.ensure_lock(self.root)
        with patch.object(
            launcher.subprocess,
            "run",
            side_effect=subprocess.CalledProcessError(1, "nix"),
        ) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                launcher.resolve(pin)
        expression = run.call_args.args[0][-1]
        source = json.loads(
            json.loads(
                expression.removeprefix(
                    "(builtins.fetchTree (builtins.fromJSON "
                ).removesuffix(")).outPath")
            )
        )
        self.assertEqual(source["narHash"], pin["narHash"])
        self.assertEqual(source["rev"], pin["rev"])

    def test_run_forwards_harbor_arguments_only_to_run(self):
        with (
            patch.object(launcher, "resolve", return_value=Path("/locked")),
            patch.object(
                launcher.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as run,
        ):
            with self.assertRaises(SystemExit):
                launcher.main(
                    ["run", str(self.root), "--builder", "host", "--agent", "oracle", "--", "--model", "x"]
                )
        commands = [
            shlex.split(part) for part in run.call_args.args[0][-1].split(" && ")
        ]
        self.assertEqual(len(commands), 3)
        self.assertNotIn("--agent", commands[0])
        self.assertNotIn("--agent", commands[1])
        self.assertEqual(
            commands[2][-6:], ["run", "--agent", "oracle", "--", "--model", "x"]
        )

    def test_failed_fetch_does_not_attempt_load(self):
        # Execute the actual shell pipeline against a tiny backend, including a
        # quoted environment path; this catches quoting and failure propagation.
        package = self.root / "mango"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "cli.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            "def main():\n"
            "    with (Path(sys.argv[1]) / 'actions').open('a') as stream:\n"
            "        stream.write(sys.argv[-1] + '\\n')\n"
            "    sys.exit(13 if sys.argv[-1] == 'fetch' else 0)\n"
        )
        command = launcher.invocation(
            Path("/unused"), [str(self.root), "/work", "load", []], ["fetch", "load"]
        )
        result = subprocess.run(
            ["bash", "-c", command[-1]],
            env={**os.environ, "PYTHONPATH": str(self.root)},
        )
        self.assertEqual(result.returncode, 13)
        self.assertEqual((self.root / "actions").read_text(), "fetch\n")


if __name__ == "__main__":
    unittest.main()
