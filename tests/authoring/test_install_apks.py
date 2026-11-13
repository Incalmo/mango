"""Filesystem-only checks of APK replay bookkeeping; no image is built."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class InstallApksTests(unittest.TestCase):
    def replay(self, populated):
        helper = Path(__file__).resolve().parents[2] / "builders/install-apks.sh"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archives = root / "archives"
            archives.mkdir()
            if populated:
                (archives / "dependency.apk").write_text("fixture")
            world = root / "world"
            world.write_text("original-root\n")
            log = root / "calls"
            # Redirect only the fixed world path; all file operations remain real.
            wrapper = r"""
test() {
  if [ "$1" = -f ] && [ "$2" = /etc/apk/world ]; then command test -f "$WORLD";
  else command test "$@"; fi
}
cp() {
  flag=$1; source=$2; target=$3
  [ "$source" != /etc/apk/world ] || source=$WORLD
  [ "$target" != /etc/apk/world ] || target=$WORLD
  command cp "$flag" "$source" "$target"
}
apk() {
  printf '%s\n' "$*" >> "$LOG"
  case "$*" in *.apk) printf 'dependency\n' >> "$WORLD";;
    *) printf 'requested-root\n' >> "$WORLD";; esac
}
. "$HELPER"
"""
            result = subprocess.run(
                ["sh", "-c", wrapper, "sh", str(archives), "requested-root"],
                env={
                    **os.environ,
                    "WORLD": str(world),
                    "LOG": str(log),
                    "HELPER": str(helper),
                },
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(world.read_text(), "original-root\nrequested-root\n")
            calls = log.read_text().splitlines()
            self.assertEqual(len(calls), 2 if populated else 1)
            self.assertTrue(
                all(call.startswith("add --no-cache --no-network ") for call in calls)
            )

    def test_dependencies_do_not_become_world_roots(self):
        self.replay(True)

    def test_empty_transaction_keeps_existing_world_roots(self):
        self.replay(False)
