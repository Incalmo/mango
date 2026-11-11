"""Chained image assembly retains base layers and the entire configuration."""

import hashlib
import json
import sys
import tarfile
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "builders"))
from rootfs import BaseImage, assemble


class ChainedImageTests(unittest.TestCase):
    def test_dir_then_oci_chain_preserves_layers_config_and_repeated_blobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base"
            base.mkdir()
            layer = root / "layer"
            layer.mkdir()
            with tarfile.open(layer / "layer.tar", "w"):
                pass
            raw = (layer / "layer.tar").read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            (base / digest).write_bytes(raw)
            original = {
                "architecture": "amd64",
                "os": "linux",
                "author": "preserved",
                "config": {
                    "User": "123:456",
                    "WorkingDir": "/opt",
                    "Env": ["PIN=1"],
                    "Healthcheck": {"Test": ["CMD", "/bin/true"]},
                },
                "rootfs": {"type": "layers", "diff_ids": ["sha256:" + digest]},
                "history": [{"created_by": "original"}],
            }
            config_bytes = json.dumps(original).encode()
            config_hash = hashlib.sha256(config_bytes).hexdigest()
            (base / config_hash).write_bytes(config_bytes)
            manifest = {
                "schemaVersion": 2,
                "config": {"digest": "sha256:" + config_hash},
                "layers": [
                    {
                        "digest": "sha256:" + digest,
                        "size": len(raw),
                        "mediaType": "application/vnd.oci.image.layer.v1.tar",
                        "annotations": {"preserved": "yes"},
                    }
                ],
            }
            (base / "manifest.json").write_text(json.dumps(manifest))
            first, second = root / "first", root / "second"
            assemble(Namespace(base=base, layer=[layer], config=None, out=first))
            override = root / "config.json"
            override.write_text(json.dumps({"Cmd": ["/bin/true"]}))
            assemble(Namespace(base=first, layer=[layer], config=override, out=second))
            result = BaseImage(second)
            self.assertEqual(result.manifest["layers"][0], manifest["layers"][0])
            self.assertEqual(len(result.manifest["layers"]), 3)
            self.assertEqual(
                result.config["rootfs"]["diff_ids"], ["sha256:" + digest] * 3
            )
            self.assertEqual(result.config["author"], "preserved")
            self.assertEqual(
                result.config["config"], {**original["config"], "Cmd": ["/bin/true"]}
            )
            self.assertEqual(result.config["history"][0], original["history"][0])
            self.assertEqual(len(result.config["history"]), 3)
            self.assertEqual(
                result.blob(result.manifest["layers"][0]).read_bytes(), raw
            )
            self.assertEqual((base / config_hash).read_bytes(), config_bytes)

    def test_ambiguous_oci_indexes_fail_without_selecting_an_arbitrary_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
            for descriptors in [
                [],
                [{"mediaType": "application/vnd.oci.image.index.v1+json"}],
                [{}, {}],
            ]:
                (root / "index.json").write_text(json.dumps({"manifests": descriptors}))
                with self.assertRaisesRegex(ValueError, "exactly one image manifest"):
                    BaseImage(root)


if __name__ == "__main__":
    unittest.main()
