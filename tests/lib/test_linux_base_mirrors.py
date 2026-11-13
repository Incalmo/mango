"""Exercise registry routing with a fake copier, never image downloads/builds."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DIGEST = "sha256:" + "1" * 64


class LinuxBaseMirrorsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        expression = """
let h = import REPO {};
    helper = import (REPO + "/lib/images/linux-image.nix") {
      pkgs = h.pkgs // { runCommand = name: attrs: script: {
        inherit script; inherit (attrs) outputHash outputHashMode;
      }; };
    };
    base = { image = "docker.io/library/example"; digest = "DIGEST"; hash = "unchanged-nar-hash"; };
in {
  mirrored = helper.fetchLinuxBase (base // { mirrors = [ "first.example/library/example" "second.example/library/example" ]; });
  original = helper.fetchLinuxBase base;
}
""".replace("REPO", str(REPO)).replace("DIGEST", DIGEST)
        result = subprocess.run(
            ["nix-instantiate", "--eval", "--strict", "--json", "--expr", expression],
            check=True,
            capture_output=True,
            text=True,
        )
        cls.declarations = json.loads(result.stdout)

    def run_routes(self, success):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        copier = root / "skopeo"
        copier.write_text("""#!/bin/sh
for arg do
  case "$arg" in docker://*) source=$arg;; dir:*) destination=${arg#dir:};; esac
done
printf '%s\\n' "$source" >> "$ROUTE_LOG"
if [ -e "$destination/partial" ]; then exit 99; fi
mkdir -p "$destination"
if [ "$source" = "$SUCCESS_ROUTE" ]; then
  printf 'success' > "$destination/complete"
  exit 0
fi
touch "$destination/partial"
exit 1
""")
        copier.chmod(0o755)
        env = dict(
            os.environ,
            PATH=str(root) + os.pathsep + os.environ["PATH"],
            out=str(root / "output"),
            ROUTE_LOG=str(root / "routes"),
            SUCCESS_ROUTE=success,
        )
        result = subprocess.run(
            ["bash", "-e", "-c", self.declarations["mirrored"]["script"]],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        return result, root

    def test_second_mirror_after_partial_failure(self):
        target = f"docker://second.example/library/example@{DIGEST}"
        result, root = self.run_routes(target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len((root / "routes").read_text().splitlines()), 2)
        self.assertTrue((root / "output/complete").exists())
        self.assertFalse((root / "output/partial").exists())

    def test_original_is_last_fallback(self):
        target = f"docker://docker.io/library/example@{DIGEST}"
        result, root = self.run_routes(target)
        self.assertEqual(result.returncode, 0, result.stderr)
        routes = (root / "routes").read_text().splitlines()
        self.assertEqual(len(routes), 3)
        self.assertEqual(routes[-1], target)
        self.assertTrue(all(route.endswith("@" + DIGEST) for route in routes))

    def test_all_fail_removes_partial_output(self):
        result, root = self.run_routes("none")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((root / "output").exists())

    def test_default_and_hash_are_unchanged(self):
        default = self.declarations["original"]
        self.assertNotIn("for source", default["script"])
        self.assertIn(f"docker://docker.io/library/example@{DIGEST}", default["script"])
        for declaration in self.declarations.values():
            self.assertEqual(declaration["outputHash"], "unchanged-nar-hash")
            self.assertEqual(declaration["outputHashMode"], "recursive")
