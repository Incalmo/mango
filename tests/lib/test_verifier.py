"""pythonVerifier: the task's tests on a minimal Python userland."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2]


class PythonVerifierTests(unittest.TestCase):
    def test_tests_and_runtime_config(self):
        with tempfile.TemporaryDirectory() as directory:
            tests = Path(directory) / "tests"
            tests.mkdir()
            (tests / "test.sh").write_text("#!/bin/sh\npython3 /tests/grade.py\n")
            (tests / "grade.py").write_text("print('graded')\n")
            expression = (
                f'(import {HARNESS} {{}}).pythonVerifier {{ name = "grader"; tests = {tests}; }}'
            )
            result = subprocess.run(
                ["nix-build", "--no-out-link", "--expr", expression],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            image = json.loads(Path(result.stdout.strip().splitlines()[-1]).read_text())
            config = image["image-config"]
            self.assertEqual(config["WorkingDir"], "/tests")
            self.assertEqual(config["Cmd"], ["sleep", "infinity"])
            self.assertIn("HOME=/tmp", config["Env"])


if __name__ == "__main__":
    unittest.main()
