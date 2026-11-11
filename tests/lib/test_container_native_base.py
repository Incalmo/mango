"""A native OCI stage can be the base of a declarative runtime container."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HARNESS / "builders"))
from mango.outputs.oci import load_one, require_containerd_store
from rootfs import BaseImage


class ContainerBaseDeclarationTests(unittest.TestCase):
    def evaluate(self, expression):
        return subprocess.run(
            [
                "nix-instantiate",
                "--eval",
                "--strict",
                "--json",
                "--expr",
                f"let h = import {HARNESS} {{}}; in " + expression,
            ],
            capture_output=True,
            text=True,
        )

    def test_invalid_or_ambiguous_base_shapes_fail(self):
        for base in [
            "{}",
            '{ociLayout="not-a-store-output";}',
            '{ociLayout=./.;image="alpine";}',
            '{image="alpine";digest="bad";hash="x";}',
        ]:
            result = self.evaluate(
                f'(h.mkContainer {{name="invalid-base";base={base};}}).drvPath'
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Container base must", result.stderr)

    def test_environment_metadata_does_not_force_native_stage(self):
        result = self.evaluate("""(h.mkEnvironment {
          name="lazy-native";task=./.;
          containers.main=h.mkContainer {name="lazy";base.ociLayout=throw "native was forced";};
          containers.egress=throw "egress was forced";
          services.main.container="main";
        }).declaration""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["manifest"]["services"], {"main": "main"}
        )


@unittest.skipUnless(
    os.environ.get("RUN_LINUX_VM_TESTS") == "1", "opt-in VM and Docker test"
)
class NativeContainerRuntimeTests(unittest.TestCase):
    def test_registry_base_load_preserves_inherited_configuration(self):
        from tests.lib.test_containers import build

        image = build(r"""c {
          name="registry-loaded-fixture";
          base={image="docker.io/library/alpine";digest="sha256:bc21487101ae47172509130ee1092cc273a831968d64b617776d1f69c81e42ab";hash="sha256-VJrEWNzP7YSUs7FXJ6UIbPd7YlPrIGXCxJ2UAbL9iCc=";};
          environment.EXTRA="registry";
          files."/etc/declarative".text="preserved";
        }""")
        document = json.loads(image.read_text())
        require_containerd_store()
        with tempfile.TemporaryDirectory() as scratch:
            reference = load_one("registry-fixture-" + uuid.uuid4().hex, image, scratch)
        tag = reference.split("@")[0] + ":" + reference.split("sha256:")[1][:16]
        try:
            metadata = json.loads(
                subprocess.check_output(
                    ["docker", "image", "inspect", reference], text=True
                )
            )[0]
            for field in ["Cmd", "Env", "WorkingDir", "User"]:
                self.assertEqual(
                    metadata["Config"].get(field) or "",
                    document["image-config"].get(field) or "",
                )
            self.assertEqual(
                metadata["RootFS"]["Layers"],
                [layer["diff_ids"] for layer in document["layers"]],
            )
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--pull",
                    "never",
                    reference,
                    "/bin/sh",
                    "-c",
                    "echo $EXTRA; cat /etc/declarative",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "registry\npreserved")
        finally:
            subprocess.run(
                ["docker", "image", "rm", "--force", tag],
                capture_output=True,
                check=False,
            )

    def test_native_base_runtime_files_accounts_and_config(self):
        expression = r"""
        let h = import HARNESS {};
          native = h.mkLinuxImage {
            name="container-native-base-fixture";diskSize=1024;memSize=512;
            base=h.fetchLinuxBase {
              image="docker.io/library/alpine";
              digest="sha256:bc21487101ae47172509130ee1092cc273a831968d64b617776d1f69c81e42ab";
              hash="sha256-VJrEWNzP7YSUs7FXJ6UIbPd7YlPrIGXCxJ2UAbL9iCc=";
            };
            buildScript="set -eu; mkdir -p /srv/native; echo installed > /srv/native/marker; echo base > /srv/native/value; echo app:x:123:456:app:/srv/native:/bin/sh >> /etc/passwd; echo app:x:456: >> /etc/group";
            config={User="app";WorkingDir="/srv/native";Entrypoint=["/bin/sh"];
              Cmd=["-c" "echo original"];Env=["PATH=/bin:/usr/bin" "BASE=kept" "OVERRIDE=base"];
              Labels.native="yes";ExposedPorts."8080/tcp"={};
              Healthcheck.Test=["CMD" "/bin/true"];StopSignal="SIGUSR1";
            };
          };
          image=h.mkContainer {
            name="container-from-native-fixture";base.ociLayout=native;
            environment.OVERRIDE="wrapped";
            files={
              "/srv/native/value"={text="overridden\n";mode="0444";};
              "/etc/passwd".text="root:x:0:0:root:/root:/bin/sh\napp:x:2000:2000:app:/srv/native:/bin/sh\n";
              "/etc/group".text="root:x:0:\napp:x:2000:\n";
            };
            command=["-c" "id -u; echo $BASE; echo $OVERRIDE; pwd; cat value; cat marker; stat -c %a value"];
          };
        in { inherit native image; }
        """.replace("HARNESS", str(HARNESS))
        result = subprocess.run(
            [
                "nix-build",
                "--no-out-link",
                "--expr",
                expression,
                "-A",
                "native",
                "-A",
                "image",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        native, image = map(Path, result.stdout.split())
        base = BaseImage(native)
        document = json.loads(image.read_text())
        self.assertEqual(
            [
                layer["digest"]
                for layer in document["layers"][: len(base.manifest["layers"])]
            ],
            [layer["digest"] for layer in base.manifest["layers"]],
        )
        self.assertEqual(
            [
                layer["diff_ids"]
                for layer in document["layers"][: len(base.manifest["layers"])]
            ],
            base.config["rootfs"]["diff_ids"],
        )
        for field in [
            "User",
            "WorkingDir",
            "Entrypoint",
            "Labels",
            "ExposedPorts",
            "Healthcheck",
            "StopSignal",
        ]:
            self.assertEqual(
                document["image-config"][field], base.config["config"][field]
            )
        require_containerd_store()
        name = "native-base-fixture-" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as scratch:
            reference = load_one(name, image, scratch)
        tag = reference.split("@")[0] + ":" + reference.split("sha256:")[1][:16]
        try:
            metadata = json.loads(
                subprocess.check_output(
                    ["docker", "image", "inspect", reference], text=True
                )
            )[0]
            self.assertEqual(
                metadata["RootFS"]["Layers"][: len(base.manifest["layers"])],
                base.config["rootfs"]["diff_ids"],
            )
            for field in [
                "User",
                "WorkingDir",
                "Entrypoint",
                "Labels",
                "ExposedPorts",
                "StopSignal",
            ]:
                self.assertEqual(
                    metadata["Config"][field], base.config["config"][field]
                )
            self.assertEqual(
                metadata["Config"]["Healthcheck"]["Test"], ["CMD", "/bin/true"]
            )
            runtime = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--pull",
                    "never",
                    reference,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stderr)
            self.assertEqual(
                runtime.stdout.splitlines(),
                [
                    "2000",
                    "kept",
                    "wrapped",
                    "/srv/native",
                    "overridden",
                    "installed",
                    "444",
                ],
            )
            root = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--pull",
                    "never",
                    "--user",
                    "root",
                    reference,
                    "-c",
                    "id -u",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(root.returncode, 0, root.stderr)
            self.assertEqual(root.stdout.strip(), "0")
        finally:
            subprocess.run(
                ["docker", "image", "rm", "--force", tag],
                capture_output=True,
                check=False,
            )


if __name__ == "__main__":
    unittest.main()
