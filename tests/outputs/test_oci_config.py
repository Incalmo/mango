"""Runtime config restoration updates OCI descriptors without changing layers."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from nix2rle.outputs.oci import restore_runtime_config


class OciConfigTests(unittest.TestCase):
    def test_restore_exact_runtime_config_and_rehash_descriptors_only(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = Path(directory)
            blobs = layout / "blobs/sha256"
            blobs.mkdir(parents=True)

            def blob(value, media):
                data = json.dumps(value).encode()
                digest = hashlib.sha256(data).hexdigest()
                (blobs / digest).write_bytes(data)
                return {
                    "digest": "sha256:" + digest,
                    "size": len(data),
                    "mediaType": media,
                }

            config = {
                "architecture": "amd64",
                "os": "linux",
                "created": "1970-01-01T00:00:01Z",
                "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "a" * 64]},
                "history": [{"created_by": "nix2container"}],
                "config": {"User": "old"},
            }
            manifest = {
                "schemaVersion": 2,
                "config": blob(config, "application/vnd.oci.image.config.v1+json"),
                "layers": [
                    {
                        "digest": "sha256:" + "b" * 64,
                        "size": 7,
                        "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    }
                ],
            }
            descriptor = blob(manifest, "application/vnd.oci.image.manifest.v1+json")
            descriptor["annotations"] = {"example": "keep"}
            (layout / "index.json").write_text(
                json.dumps({"schemaVersion": 2, "manifests": [descriptor]})
            )
            runtime = {
                "User": "app",
                "Env": ["PIN=1"],
                "Labels": {"kept": "yes"},
                "StopSignal": "SIGUSR1",
                "Healthcheck": {"Test": ["CMD", "/bin/true"]},
                "Entrypoint": [],
                "Cmd": ["/bin/sh"],
                "WorkingDir": "/work",
            }
            result = restore_runtime_config(layout, runtime)

            def verify(descriptor):
                data = (blobs / descriptor["digest"].split(":")[1]).read_bytes()
                self.assertEqual(
                    descriptor["digest"], "sha256:" + hashlib.sha256(data).hexdigest()
                )
                self.assertEqual(descriptor["size"], len(data))
                return json.loads(data)

            changed = verify(result["manifests"][0])
            restored = verify(changed["config"])
            self.assertEqual(changed["layers"], manifest["layers"])
            self.assertEqual(restored, {**config, "config": runtime})
            self.assertEqual(result["manifests"][0]["annotations"], {"example": "keep"})
            self.assertEqual(restore_runtime_config(layout, runtime), result)
            with self.assertRaisesRegex(ValueError, "must be an object"):
                restore_runtime_config(layout, None)


if __name__ == "__main__":
    unittest.main()
