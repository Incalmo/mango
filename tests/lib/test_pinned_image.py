"""Upstream bytes survive packaging and loading without synthetic image layers."""

import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "pinned_image", HARNESS / "builders/pinned_image.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def descriptor(base, data, media_type):
    digest = hashlib.sha256(data).hexdigest()
    (base / digest).write_bytes(data)
    return {"digest": "sha256:" + digest, "size": len(data), "mediaType": media_type}


class PinnedImageTests(unittest.TestCase):
    def test_reuses_strict_digest_and_content_pin_validation(self):
        for digest, content_hash, message in [
            ("latest", "sha256-placeholder", "immutable SHA-256 digest"),
            ("sha256:" + "a" * 64, "", "require a content hash"),
        ]:
            expression = f'(import {HARNESS} {{}}).pinnedImage {{image="docker.io/library/alpine";digest={json.dumps(digest)};hash={json.dumps(content_hash)};}}'
            result = subprocess.run(
                ["nix-instantiate", "--expr", expression],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(message, result.stderr)

    def test_preserves_oci_and_docker_manifest_bytes(self):
        for media_type in MODULE.MANIFEST_TYPES:
            with (
                self.subTest(media_type=media_type),
                tempfile.TemporaryDirectory() as directory,
            ):
                base = Path(directory) / "base"
                base.mkdir()
                config = descriptor(
                    base,
                    b'{"rootfs":{"diff_ids":["sha256:fixture"]},"config":{"User":"42"}}',
                    "application/vnd.oci.image.config.v1+json",
                )
                layer = descriptor(
                    base,
                    b"unchanged compressed layer",
                    "application/vnd.oci.image.layer.v1.tar+gzip",
                )
                raw = json.dumps(
                    {
                        "schemaVersion": 2,
                        "mediaType": media_type,
                        "config": config,
                        "layers": [layer],
                    },
                    indent=3,
                ).encode()
                (base / "manifest.json").write_bytes(raw)
                out = Path(directory) / "layout"
                MODULE.package(base, out)
                index = json.loads((out / "index.json").read_text())
                self.assertEqual(index["manifests"][0]["mediaType"], media_type)
                self.assertEqual(
                    index["manifests"][0]["digest"],
                    "sha256:" + hashlib.sha256(raw).hexdigest(),
                )
                for d, expected in [
                    (index["manifests"][0], raw),
                    (config, (base / config["digest"][7:]).read_bytes()),
                    (layer, (base / layer["digest"][7:]).read_bytes()),
                ]:
                    blob = out / "blobs/sha256" / d["digest"][7:]
                    self.assertTrue(blob.is_symlink())
                    self.assertEqual(blob.read_bytes(), expected)

    def test_rejects_corrupt_or_unsafe_descriptors_before_output(self):
        for invalid in [
            "sha256:../escape",
            "sha512:" + "a" * 128,
            "sha256:" + "a" * 64,
        ]:
            with (
                self.subTest(invalid=invalid),
                tempfile.TemporaryDirectory() as directory,
            ):
                base = Path(directory) / "base"
                base.mkdir()
                if invalid == "sha256:" + "a" * 64:
                    (base / ("a" * 64)).write_bytes(b"wrong bytes")
                (base / "manifest.json").write_text(
                    json.dumps(
                        {
                            "schemaVersion": 2,
                            "config": {"digest": invalid, "size": 11},
                            "layers": [],
                        }
                    )
                )
                out = Path(directory) / "output"
                with self.assertRaises(ValueError):
                    MODULE.package(base, out)
                self.assertFalse(out.exists())


class CachedPinnedImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        expr = f"""(import {HARNESS} {{}}).pinnedImage {{
          image="docker.io/library/alpine";
          digest="sha256:bc21487101ae47172509130ee1092cc273a831968d64b617776d1f69c81e42ab";
          hash="sha256-VJrEWNzP7YSUs7FXJ6UIbPd7YlPrIGXCxJ2UAbL9iCc=";
        }}"""
        result = subprocess.run(
            [
                "nix-build",
                "--no-out-link",
                "--option",
                "builders",
                "",
                "--expr",
                expr,
            ],
            text=True,
            capture_output=True,
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.layout = Path(result.stdout.strip().splitlines()[-1])
        cls.index = json.loads((cls.layout / "index.json").read_text())
        cls.manifest_path = (
            cls.layout / "blobs/sha256" / cls.index["manifests"][0]["digest"][7:]
        )
        cls.base = cls.manifest_path.resolve().parent

    def test_cached_base_has_exact_layers_config_and_diff_ids(self):
        raw = (self.base / "manifest.json").read_bytes()
        self.assertEqual(self.manifest_path.read_bytes(), raw)
        manifest = json.loads(raw)
        self.assertEqual(
            len(list((self.layout / "blobs/sha256").iterdir())),
            len(manifest["layers"]) + 2,
        )
        for entry in [manifest["config"], *manifest["layers"]]:
            self.assertEqual(
                (self.layout / "blobs/sha256" / entry["digest"][7:]).read_bytes(),
                (self.base / entry["digest"][7:]).read_bytes(),
            )
        config = json.loads((self.base / manifest["config"]["digest"][7:]).read_text())
        self.assertEqual(len(config["rootfs"]["diff_ids"]), len(manifest["layers"]))
        self.assertEqual(
            self.index["manifests"][0]["digest"],
            "sha256:" + hashlib.sha256(raw).hexdigest(),
        )

    @unittest.skipUnless(
        os.environ.get("RUN_DOCKER_TESTS") == "1", "opt-in Docker runtime test"
    )
    def test_load_and_runtime_preserve_image(self):
        spec = importlib.util.spec_from_file_location("oci", HARNESS / "src/mango/outputs/oci.py")
        oci = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(oci)
        oci.require_containerd_store()
        with tempfile.TemporaryDirectory() as scratch:
            reference = oci.load_one("pinned-image-fixture", self.layout, scratch)
        self.assertEqual(reference.split("@")[1], self.index["manifests"][0]["digest"])
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
                "sh",
                "-c",
                "printf exact-upstream-runtime",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "exact-upstream-runtime")
