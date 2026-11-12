"""Evaluation-only tests for lib/adapters/composer.nix: nothing is built or fetched.

Run with: nix-shell --run 'python -m unittest tests.lib.test_composer_cache'
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 64
PREFIX = f"""let pkgs = import (import {ROOT}/lib/pins/nixpkgs.nix) {{ system = "x86_64-linux"; }};
 d = import {ROOT}/lib/adapters {{ inherit pkgs; }}; in """


def artifact(path="files/vendor/pkg/1.0.0.0-abc.zip", **extra):
    return {"path": path, "urls": ["https://example.com/pkg.zip"], "sha256": SHA, **extra}


class ComposerCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def evaluate(self, document, suffix=".drvPath"):
        lock = Path(self.tmp.name) / "composer.lock.json"
        lock.write_text(json.dumps(document))
        return subprocess.run(
            ["nix-instantiate", "--eval", "--strict", "--expr",
             PREFIX + f"(d.fetchComposerCache {{ lock = {lock}; }}){suffix}"],
            capture_output=True, text=True, check=False)

    def lock(self, artifacts=None, **extra):
        return {"schema_version": 1, "kind": "composer-cache",
                "artifacts": [artifact()] if artifacts is None else artifacts, **extra}

    def accepted(self, document):
        result = self.evaluate(document)
        self.assertEqual(result.returncode, 0, result.stderr)

    def rejected(self, document, text):
        result = self.evaluate(document)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(text, result.stderr)

    def test_accepts_valid_lock(self):
        self.accepted(self.lock([artifact(), artifact("files/vendor/b/2.0.zip")]))
        self.accepted(self.lock(unmapped_files=[]))

    def test_rejects_wrong_kind_or_schema(self):
        self.rejected({**self.lock(), "kind": "maven"}, "kind")
        self.rejected({**self.lock(), "schema_version": 2}, "schema_version")

    def test_rejects_empty_or_missing_artifacts(self):
        self.rejected(self.lock([]), "non-empty")
        self.rejected({"schema_version": 1, "kind": "composer-cache"}, "non-empty")

    def test_rejects_unsafe_paths(self):
        for path in ["files/../x.zip", "files/a/../x.zip", "/files/x.zip", "x.zip", "other/x.zip",
                     "files/", "files//x.zip", "files/./x.zip", "files/a b.zip", "files/$x.zip",
                     "files/x;y.zip", "files/`x`.zip", "files/x'y.zip", "files/x\ny.zip"]:
            with self.subTest(path=path):
                self.rejected(self.lock([artifact(path)]), "safe files/")

    def test_rejects_duplicates_and_overlaps(self):
        self.rejected(self.lock([artifact(), artifact()]), "duplicate paths")
        self.rejected(self.lock([artifact("files/a"), artifact("files/a/b.zip")]), "overlapping")

    def test_rejects_bad_hashes_and_extra_keys(self):
        for sha in ["", "A" * 64, "a" * 63, "sha256-abc"]:
            with self.subTest(sha=sha):
                self.rejected(self.lock([{**artifact(), "sha256": sha}]), "64-hex")
        self.rejected(self.lock([artifact(sha512="b" * 128)]), "exactly")
        self.rejected(self.lock([artifact(headers={})]), "exactly")

    def test_rejects_unsafe_urls(self):
        for url in ["file:///etc/passwd", "ftp://example.com/x", "https://user:pw@example.com/x",
                    "https://example.com/x?token=abc"]:
            with self.subTest(url=url):
                self.rejected(self.lock([{**artifact(), "urls": [url]}]), "URLs")
        self.rejected(self.lock([{**artifact(), "urls": []}]), "URLs")

    def test_unmapped_files_fail_closed(self):
        self.rejected(self.lock(unmapped_files=[{"path": "repo/x.json", "sha256": SHA}]), "unmapped_files")


if __name__ == "__main__":
    unittest.main()
