"""Real image assembly with overlapping package/config directories."""

import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2]


class ImageRootTests(unittest.TestCase):
    def test_merged_roots_preserve_file_and_directory_modes(self):
        expression = r"""
        let h = import HARNESS {}; p = h.pkgs;
        package = p.runCommand "root-test-package" {} ''
          mkdir -p $out/etc $out/bin
          echo original > $out/etc/config
          echo data > $out/etc/package-data
          printf '#!/bin/sh\nexit 0\n' > $out/bin/program
          chmod +x $out/bin/program
        '';
        first = p.runCommand "root-test-first" {} ''
          mkdir -p $out/etc
          echo first > $out/etc/config
        '';
        second = p.runCommand "root-test-second" {} ''
          mkdir -p $out/etc
          echo second > $out/etc/config
        '';
        in h.mkImage {
          name = "root-merge-test"; packages = [package]; root = [first second];
          dirs = [{path="/etc";} {path="/tmp";mode="1777";}]; maxLayers=1;
        }
        """.replace("HARNESS", str(HARNESS))
        image = subprocess.check_output(
            ["nix-build", "--no-out-link", "--expr", expression], text=True
        ).strip()
        with tempfile.TemporaryDirectory() as temporary:
            layout = Path(temporary) / "oci"
            subprocess.run(
                [
                    "skopeo",
                    "--insecure-policy",
                    "copy",
                    f"nix:{image}",
                    f"oci:{layout}",
                ],
                check=True,
                capture_output=True,
            )
            blobs = layout / "blobs/sha256"
            index = json.loads((layout / "index.json").read_text())
            manifest = json.loads(
                (blobs / index["manifests"][0]["digest"].split(":")[1]).read_text()
            )
            entries = {}
            config = None
            for layer in manifest["layers"]:
                with tarfile.open(blobs / layer["digest"].split(":")[1]) as archive:
                    for member in archive:
                        name = member.name.removeprefix("./").strip("/")
                        entries[name] = member
                        if name == "etc/config":
                            config = archive.extractfile(member).read()
            self.assertEqual(config, b"second\n")
            self.assertEqual(entries["etc/config"].mode, 0o644)
            self.assertEqual(entries["etc"].mode, 0o755)
            self.assertEqual(entries["tmp"].mode, 0o1777)
            self.assertTrue(entries["bin/program"].issym())
