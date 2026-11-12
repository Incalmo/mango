"""Replay-helper control flow using mocked filesystem commands, never rootfs runs."""
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "builders/restore-apt-indexes.sh"


class RestoreIndexesTests(unittest.TestCase):
    def run_helper(self, *, mismatch=False, member="Packages", list_fail=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "index.tar.gz"
            archive.write_bytes(b"fixture archive; tar is mocked")
            log = root / "calls"
            for command in ("rm", "mkdir"):
                path = root / command
                path.write_text(f'#!/bin/sh\nprintf \'{command}:%s\\n\' "$*" >> "$TEST_LOG"\n')
                path.chmod(0o755)
            tar = root / "tar"
            tar.write_text('#!/bin/sh\nif [ "$1" = -tzf ]; then\n'
                           + ('exit 1\n' if list_fail else f"printf '%s\\n' '{member}'\n")
                           + 'else printf \'tar:%s\\n\' "$*" >> "$TEST_LOG"; fi\n')
            tar.chmod(0o755)
            digest = "0" * 64 if mismatch else hashlib.sha256(archive.read_bytes()).hexdigest()
            env = {**os.environ, "PATH": str(root) + ":" + os.environ["PATH"], "TEST_LOG": str(log)}
            result = subprocess.run(["sh", str(SCRIPT), str(archive), digest], env=env, capture_output=True, text=True, check=False)
            return result, log.read_text() if log.exists() else ""

    def test_original_update_replaces_lists_with_numeric_ownership(self):
        result, calls = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rm:-rf /var/lib/apt/lists", calls)
        self.assertIn("tar:--numeric-owner -xzpf", calls)

    def test_unverified_unsafe_or_invalid_archive_does_not_touch_state(self):
        for options in ({"mismatch": True}, {"member": "../escape"}, {"list_fail": True}):
            with self.subTest(options=options):
                result, calls = self.run_helper(**options)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, "")
