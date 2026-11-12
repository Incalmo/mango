"""Explicit native VM CPU allocation without host-dependent defaults."""

import json
import os
import subprocess
import unittest
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2]
PREFIX = f'let h = import {HARNESS} {{}}; image = options: h.mkLinuxImage ({{name="cpu-test"; base=h.pkgs.emptyDirectory; buildScript="true";}} // options); in '


def evaluate(expression):
    return subprocess.run(
        [
            "nix-instantiate",
            "--eval",
            "--strict",
            "--json",
            "--expr",
            PREFIX + expression,
        ],
        text=True,
        capture_output=True,
    )


class NativeCoresTests(unittest.TestCase):
    def test_default_equals_explicit_one_cpu(self):
        result = evaluate(
            "(image {}).layer.drvPath == (image {cores=1;}).layer.drvPath"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout))

    def test_explicit_cpu_setting_reaches_qemu_options(self):
        result = evaluate(
            "{default=(image {}).layer.env.QEMU_OPTS; explicit=(image {cores=3;}).layer.env.QEMU_OPTS; automatic=(image {}).layer.enableParallelBuilding;}"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        options = json.loads(result.stdout)
        self.assertFalse(options["automatic"])
        self.assertNotIn("-smp", options["default"])
        self.assertIn("-smp cpus=3", options["explicit"])
        self.assertEqual(
            options["explicit"].replace(" -smp cpus=3", ""), options["default"]
        )

    def test_invalid_cpu_settings(self):
        for value in ["0", "-1", "true", "false", "1.5", '"2"', "256"]:
            with self.subTest(value=value):
                result = evaluate(f"(image {{cores={value};}}).drvPath")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("cores must be an integer from 1 to 255", result.stderr)

    @unittest.skipUnless(os.environ.get("RUN_LINUX_VM_TESTS") == "1", "opt-in VM test")
    def test_guest_observes_exact_cpu_count(self):
        self.assert_guest_cpu_count(2, 1)

    @unittest.skipUnless(os.environ.get("RUN_LINUX_VM_TESTS") == "1", "opt-in VM test")
    def test_default_guest_ignores_nix_build_cores(self):
        self.assert_guest_cpu_count(1, 4)

    def assert_guest_cpu_count(self, cores, build_cores):
        setting = "" if cores == 1 else f"cores={cores};"
        expression = f"""let h = import {HARNESS} {{}}; in h.mkLinuxImage {{
          name="native-{cores}-cpus"; {setting} memSize=512; diskSize=1024;
          base=h.fetchLinuxBase {{
            image="docker.io/library/alpine";
            digest="sha256:bc21487101ae47172509130ee1092cc273a831968d64b617776d1f69c81e42ab";
            hash="sha256-VJrEWNzP7YSUs7FXJ6UIbPd7YlPrIGXCxJ2UAbL9iCc=";
          }};
          buildScript = ''
            set -eu
            test "$(grep -c '^processor' /proc/cpuinfo)" = {cores}
            echo guest-observed-exactly-{cores}-cpus
          '';
        }}"""
        result = subprocess.run(
            [
                "nix-build",
                "--no-out-link",
                "--option",
                "builders",
                "",
                "--cores",
                str(build_cores),
                "--max-jobs",
                "1",
                "--expr",
                expression,
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
