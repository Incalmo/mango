"""Opt-in real VM test; the fixture is synthesized in a temporary directory."""

import json
import os
import subprocess
import tarfile
import unittest
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get("RUN_LINUX_VM_TESTS") == "1", "opt-in VM test")
class LinuxVmTests(unittest.TestCase):
    def test_build_failure_reports_the_authored_error(self):
        expr = r"""
        let h = import HARNESS {}; in h.mkLinuxImage {
          name = "linux-vm-failure-test";
          base = h.fetchLinuxBase {
            image = "docker.io/library/alpine";
            digest = "sha256:bc21487101ae47172509130ee1092cc273a831968d64b617776d1f69c81e42ab";
            hash = "sha256-VJrEWNzP7YSUs7FXJ6UIbPd7YlPrIGXCxJ2UAbL9iCc=";
          };
          diskSize = 1024; memSize = 512;
          buildScript = "echo explicit-input-fixture-error >&2; exit 23";
        }
        """.replace("HARNESS", str(HARNESS))
        result = subprocess.run(
            ["nix-build", "--no-out-link", "--expr", expr],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("explicit-input-fixture-error", result.stderr)

    def test_chained_native_base_preserves_config_ownership_and_exports(self):
        expr = r"""
        let
          h = import HARNESS {};
          base = h.fetchLinuxBase {
            image = "docker.io/library/alpine";
            digest = "sha256:bc21487101ae47172509130ee1092cc273a831968d64b617776d1f69c81e42ab";
            hash = "sha256-VJrEWNzP7YSUs7FXJ6UIbPd7YlPrIGXCxJ2UAbL9iCc=";
          };
          first = h.mkLinuxImage {
            name = "native-chain-first"; inherit base;
            diskSize = 1024; memSize = 512;
            config = { User = "123:456"; WorkingDir = "/opt/stage";
              Cmd = [ "/bin/cat" "/opt/stage/value" ]; Env = [ "PATH=/bin:/usr/bin" "CHAINED=yes" ];
              ExposedPorts."8080/tcp" = {}; Labels.chain = "first";
            };
            buildScript = ''
              set -eu
              mkdir -p /opt/stage
              echo first > /opt/stage/value
              chmod 751 /opt/stage/value
              chown 123:456 /opt/stage/value
              echo remove > /opt/stage/deleted
            '';
          };
          image = h.mkLinuxImage {
            name = "native-chain-second"; base = first;
            diskSize = 1024; memSize = 512;
            exportPaths = [ "/opt/stage/value" ];
            config.Labels.chain = "second";
            buildScript = ''
              set -eu
              test "$(id -u)" = 0
              test "$PWD" = /opt/stage
              test "$CHAINED" = yes
              test "$(cat value)" = first
              test "$(stat -c %a value)" = 751
              test "$(stat -c %u:%g value)" = 123:456
              test -z "$(ls /sys/class/net | grep -v '^lo$')"
              rm deleted
              echo second > second
            '';
          };
        in { inherit first image; artifacts = image.artifacts; }
        """.replace("HARNESS", str(HARNESS))
        result = subprocess.run(
            [
                "nix-build",
                "--no-out-link",
                "--expr",
                expr,
                "-A",
                "first",
                "-A",
                "image",
                "-A",
                "artifacts",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        first, image, artifacts = map(Path, result.stdout.split())

        def read_image(path):
            blobs = path / "blobs/sha256"
            index = json.loads((path / "index.json").read_text())
            manifest = json.loads(
                (blobs / index["manifests"][0]["digest"].split(":")[1]).read_text()
            )
            config = json.loads(
                (blobs / manifest["config"]["digest"].split(":")[1]).read_text()
            )
            return manifest, config, blobs

        initial, initial_config, _ = read_image(first)
        manifest, config, blobs = read_image(image)
        self.assertEqual(manifest["layers"][:-1], initial["layers"])
        self.assertEqual(
            config["rootfs"]["diff_ids"][:-1], initial_config["rootfs"]["diff_ids"]
        )
        self.assertEqual(config["history"][:-1], initial_config["history"])
        self.assertEqual(
            config["config"],
            {**initial_config["config"], "Labels": {"chain": "second"}},
        )
        with tarfile.open(
            blobs / manifest["layers"][-1]["digest"].split(":")[1]
        ) as layer:
            self.assertIn("opt/stage/.wh.deleted", layer.getnames())
            self.assertEqual(layer.extractfile("opt/stage/second").read(), b"second\n")
            self.assertNotIn("opt/stage/value", layer.getnames())
        with tarfile.open(artifacts) as archive:
            member = archive.getmember("opt/stage/value")
            self.assertEqual((member.mode, member.uid, member.gid), (0o751, 123, 456))
            self.assertEqual(archive.extractfile(member).read(), b"first\n")

    def test_native_artifacts_between_isolated_stages(self):
        expr = r"""
        let
          h = import HARNESS {};
          base = h.fetchLinuxBase {
            image = "docker.io/library/alpine";
            digest = "sha256:bc21487101ae47172509130ee1092cc273a831968d64b617776d1f69c81e42ab";
            hash = "sha256-VJrEWNzP7YSUs7FXJ6UIbPd7YlPrIGXCxJ2UAbL9iCc=";
          };
          native = h.mkLinuxImage {
            name = "native-export-fixture"; inherit base;
            diskSize = 1024; memSize = 512;
            exportPaths = [ "/opt/artifact" ];
            buildScript = ''
              set -eu
              mkdir -p /opt/artifact /build-only
              cp /bin/busybox /opt/artifact/busybox
              chmod 751 /opt/artifact/busybox
              chown 123:456 /opt/artifact/busybox
              ln -s /opt/artifact/busybox /opt/artifact/link
              echo build-tool > /build-only/compiler
            '';
          };
        in h.mkLinuxImage {
          name = "native-import-fixture"; inherit base;
          diskSize = 1024; memSize = 512;
          inputs = h.pkgs.linkFarm "native-stage-input" [{
            name = "native.tar"; path = native.artifacts;
          }];
          buildScript = ''
            set -eu
            tar -xpf /.nix-inputs/files/native.tar -C /
            test ! -e /build-only/compiler
            test "$(stat -c %a /opt/artifact/busybox)" = 751
            test "$(stat -c %u:%g /opt/artifact/busybox)" = 123:456
            test "$(readlink /opt/artifact/link)" = /opt/artifact/busybox
            /opt/artifact/busybox echo native-stage-imported
          '';
        }
        """.replace("HARNESS", str(HARNESS))
        result = subprocess.run(
            ["nix-build", "--no-out-link", "--expr", expr],
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        image = Path(result.stdout.strip().splitlines()[-1])
        blobs = image / "blobs/sha256"
        index = json.loads((image / "index.json").read_text())
        manifest = json.loads(
            (blobs / index["manifests"][0]["digest"].split(":")[1]).read_text()
        )
        with tarfile.open(
            blobs / manifest["layers"][-1]["digest"].split(":")[1]
        ) as layer:
            names = layer.getnames()
            self.assertFalse(
                any(
                    "build-only" in name
                    or "artifacts.tar" in name
                    or ".nix-inputs" in name
                    for name in names
                )
            )
            entry = layer.getmember("opt/artifact/busybox")
            self.assertEqual((entry.mode, entry.uid, entry.gid), (0o751, 123, 456))
            self.assertEqual(
                layer.getmember("opt/artifact/link").linkname, "/opt/artifact/busybox"
            )

    def test_offline_script_and_image_boundaries(self):
        expr = r"""
        let h = import HARNESS {}; in h.mkLinuxImage {
          name = "linux-vm-contract-test";
          base = h.fetchLinuxBase {
            image = "docker.io/library/alpine";
            digest = "sha256:bc21487101ae47172509130ee1092cc273a831968d64b617776d1f69c81e42ab";
            hash = "sha256-VJrEWNzP7YSUs7FXJ6UIbPd7YlPrIGXCxJ2UAbL9iCc=";
          };
          inputs = h.pkgs.linkFarm "test-inputs" [{
            name = "value"; path = h.pkgs.writeText "value" "pinned file";
          }];
          buildEnv.BUILD_ONLY = "temporary";
          config = { Cmd = ["/bin/cat" "/opt/check"]; Env = ["PATH=/bin"]; };
          diskSize = 1024; memSize = 512;
          buildScript = ''
            set -eu
            test "$BUILD_ONLY" = temporary
            test -z "$(ls /sys/class/net | grep -v '^lo$')"
            # Native test suites may use loopback even in an offline build.
            ping -c 1 -W 1 127.0.0.1
            if (echo unexpected > /.nix-inputs/files/value) 2>/dev/null; then
              echo "input mount was writable" >&2; exit 1
            fi
            if (echo unexpected >> /.nix-inputs/build.sh) 2>/dev/null; then
              echo "build script mount was writable" >&2; exit 1
            fi
            if (touch /.nix-inputs/extra) 2>/dev/null; then
              echo "input parent mount was writable" >&2; exit 1
            fi
            if (touch /.nix-inputs/context/extra) 2>/dev/null; then
              echo "context mount was writable" >&2; exit 1
            fi
            su nobody -s /bin/sh -c 'test -r /.nix-inputs/files/value'
            value=inside
            printf '%s' "$value" > /opt/shell-value
            cp /.nix-inputs/files/value /opt/check
          '';
        }
        """.replace("HARNESS", str(HARNESS))
        result = subprocess.run(
            ["nix-build", "--no-out-link", "--expr", expr],
            check=True,
            text=True,
            capture_output=True,
        )
        image = Path(result.stdout.strip().splitlines()[-1])
        blobs = image / "blobs/sha256"

        def blob(descriptor):
            return blobs / descriptor["digest"].split(":")[1]

        index = json.loads((image / "index.json").read_text())
        manifest = json.loads(blob(index["manifests"][0]).read_text())
        config = json.loads(blob(manifest["config"]).read_text())
        self.assertFalse(
            (
                blob(manifest["layers"][-1]).resolve().parent / "disk-image.qcow2"
            ).exists()
        )
        self.assertEqual(config["config"]["Env"], ["PATH=/bin"])
        self.assertEqual(config["config"]["Cmd"], ["/bin/cat", "/opt/check"])
        with tarfile.open(blob(manifest["layers"][-1])) as layer:
            self.assertFalse(any(".nix-inputs" in item.name for item in layer))
            self.assertEqual(layer.extractfile("opt/check").read(), b"pinned file")
            self.assertEqual(layer.extractfile("opt/shell-value").read(), b"inside")
