"""Portable VM builds must not be rejected before QEMU can try its fallback."""

import json
import os
import subprocess
import tarfile
import unittest
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2]
PREFIX = f"let h = import {HARNESS} {{}}; in "


class VmAccelerationTests(unittest.TestCase):
    def evaluate(self, expression):
        result = subprocess.run(
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
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_only_explicit_kvm_requires_a_kvm_builder(self):
        for mode, features in [("auto", []), ("tcg", []), ("kvm", ["kvm"])]:
            with self.subTest(mode=mode):
                self.assertEqual(
                    self.evaluate(
                        '(h.mkLinuxImage {name="accel-test"; base=h.pkgs.emptyDirectory; '
                        f'buildScript="true"; acceleration="{mode}";}}).layer.requiredSystemFeatures'
                    ),
                    features,
                )

    def test_default_is_automatic_and_forced_software_has_distinct_derivation(self):
        self.assertTrue(
            self.evaluate("""let image = args: h.mkLinuxImage ({
          name="accel-test"; base=h.pkgs.emptyDirectory; buildScript="true";
        } // args); in (image {}).layer.drvPath == (image {acceleration="auto";}).layer.drvPath
          && (image {}).layer.drvPath != (image {acceleration="tcg";}).layer.drvPath""")
        )

    @unittest.skipUnless(
        os.environ.get("RUN_LINUX_VM_TESTS") == "1", "opt-in QEMU test"
    )
    def test_auto_falls_back_when_kvm_cannot_initialize(self):
        qemu = (
            subprocess.check_output(
                ["nix-build", "--no-out-link", "--expr", PREFIX + "h.pkgs.qemu_kvm"],
                text=True,
            )
            .strip()
            .splitlines()[-1]
            + "/bin/qemu-system-x86_64"
        )
        command = [
            qemu,
            "-machine",
            "accel=kvm:tcg",
            "-cpu",
            "max",
            "-display",
            "none",
            "-nodefaults",
            "-S",
            "-qmp",
            "stdio",
        ]
        if Path("/dev/kvm").exists():
            # Mask KVM only in a private, unprivileged mount namespace. The
            # host device and mounts are untouched. QEMU must handle the failed
            # KVM ioctl by starting TCG, not by merely noticing a missing file.
            available = subprocess.run(
                ["unshare", "--user", "--map-root-user", "--mount", "--", "true"],
                capture_output=True,
            )
            if available.returncode:
                self.skipTest("unprivileged namespaces unavailable for masking KVM")
            command = [
                "unshare",
                "--user",
                "--map-root-user",
                "--mount",
                "--",
                "sh",
                "-c",
                'mount --make-rprivate / && mount --bind /dev/null /dev/kvm && exec "$@"',
                "mango-fallback-test",
                *command,
            ]
        result = subprocess.run(
            command,
            input='{"execute":"qmp_capabilities"}\n{"execute":"query-kvm"}\n{"execute":"quit"}\n',
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        status = next(
            reply["return"] for reply in replies if "enabled" in reply.get("return", {})
        )
        self.assertFalse(status["enabled"])
        self.assertIn("falling back to tcg", result.stderr)

    @unittest.skipUnless(
        os.environ.get("RUN_LINUX_VM_TESTS") == "1", "opt-in real VM build"
    )
    def test_auto_and_software_build_identical_layers(self):
        layers = []
        for mode in ("auto", "tcg"):
            expression = (
                PREFIX
                + """h.mkLinuxImage {
              name="vm-acceleration-test"; acceleration="MODE";
              diskSize=1024; memSize=512;
              base=h.fetchLinuxBase {
                image="docker.io/library/alpine";
                digest="sha256:bc21487101ae47172509130ee1092cc273a831968d64b617776d1f69c81e42ab";
                hash="sha256-VJrEWNzP7YSUs7FXJ6UIbPd7YlPrIGXCxJ2UAbL9iCc=";
              };
              buildScript = "mkdir -p /opt; echo portable > /opt/acceleration; chown 123:456 /opt/acceleration";
            }""".replace("MODE", mode)
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
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            image = Path(result.stdout.strip().splitlines()[-1])

            def blob(descriptor):
                return image / "blobs/sha256" / descriptor["digest"].split(":")[1]

            index = json.loads((image / "index.json").read_text())
            manifest = json.loads(blob(index["manifests"][0]).read_text())
            layer = blob(manifest["layers"][-1])
            layers.append(layer.read_bytes())
            with tarfile.open(layer) as archive:
                entry = archive.getmember("opt/acceleration")
                self.assertEqual((entry.uid, entry.gid), (123, 456))
                self.assertEqual(archive.extractfile(entry).read(), b"portable\n")
        self.assertEqual(*layers)
