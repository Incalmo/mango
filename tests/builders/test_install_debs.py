"""Offline apt transactions retain package intent rather than mark every dependency manual."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "builders/install-debs.sh"


class InstallDebsTests(unittest.TestCase):
    def test_empty_repair_runs_fix_broken_without_inventing_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input").mkdir()
            (root / "apt-mark").write_text("#!/bin/sh\nexit 0\n")
            (root / "apt-get").write_text(
                '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$APT_ARGS"\n'
            )
            for name in ("apt-mark", "apt-get"):
                (root / name).chmod(0o755)
            result = subprocess.run(
                ["sh", str(SCRIPT), "--repair", "/unused/stager", str(root / "input")],
                env={
                    **os.environ,
                    "PATH": str(root) + ":" + os.environ["PATH"],
                    "APT_ARGS": str(root / "args"),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            args = (root / "args").read_text().splitlines()
            self.assertIn("--fix-broken", args)
            self.assertIn("--no-download", args)
            self.assertNotIn("*.deb", " ".join(args))
            refused = subprocess.run(
                ["sh", str(SCRIPT), "/unused/stager", str(root / "input")],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(refused.returncode, 0)

    def test_explicit_roots_and_preexisting_manual_packages_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input").mkdir()
            (root / "input/pkg.deb").write_bytes(b"fixture")
            (root / "manual").write_text("old-root\n")
            shim = f"""#!{sys.executable}
import json,os,sys
from pathlib import Path
root=Path(os.environ['FIXTURE_ROOT'])
manual=root/'manual'
names=set(manual.read_text().splitlines())
if Path(sys.argv[0]).name=='dpkg-query':
 print('install ok installed' if sys.argv[3]=='${{Status}}' else '1')
 sys.exit(0)
if Path(sys.argv[0]).name=='apt-get':
 (root/'apt-args').write_text(json.dumps(sys.argv[1:]))
 names.update(['old-auto','new-dependency','wanted'])
elif sys.argv[1]=='showmanual':
 print('\\n'.join(sorted(names)))
 sys.exit(0)
elif sys.argv[1]=='manual': names.add(sys.argv[2])
elif sys.argv[1]=='auto': names.discard(sys.argv[2])
manual.write_text('\\n'.join(sorted(names))+'\\n')
"""
            for name in ("apt-get", "apt-mark", "dpkg-query"):
                path = root / name
                path.write_text(shim)
                path.chmod(0o755)
            stager = root / "stage.sh"
            stager.write_text('mkdir -p "$1"\ncp "$2" "$1/fixture_1_all.deb"\n')
            result = subprocess.run(
                ["sh", str(SCRIPT), str(stager), str(root / "input"), "wanted"],
                env={
                    **os.environ,
                    "PATH": str(root) + ":" + os.environ["PATH"],
                    "FIXTURE_ROOT": str(root),
                },
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                set((root / "manual").read_text().splitlines()), {"old-root", "wanted"}
            )
            args = json.loads((root / "apt-args").read_text())
            self.assertIn("--no-download", args)
            self.assertTrue(args[-1].endswith("/fixture_1_all.deb"))
            self.assertFalse(Path(args[-1]).exists())


if __name__ == "__main__":
    unittest.main()
