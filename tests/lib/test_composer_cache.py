"""Evaluation-only tests for lib/adapters/composer.nix: nothing is built or fetched.

Run with: nix-shell --run 'python -m unittest tests.lib.test_composer_cache'
"""

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 64
HTACCESS = {"path": ".htaccess", "content": "Deny from all",
            "sha256": hashlib.sha256(b"Deny from all").hexdigest()}
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

    def test_accepts_the_known_generated_htaccess_and_it_changes_the_derivation(self):
        plain = self.evaluate(self.lock())
        with_file = self.evaluate(self.lock(generated_files=[HTACCESS]))
        self.assertEqual(with_file.returncode, 0, with_file.stderr)
        self.assertNotEqual(plain.stdout, with_file.stdout)
        self.accepted(self.lock(generated_files=[]))

    def test_generated_files_are_a_closed_exact_set(self):
        for name, entry in {
            "other path": {**HTACCESS, "path": "files/x"},
            "nested path": {**HTACCESS, "path": "sub/.htaccess"},
            "traversal": {**HTACCESS, "path": "../.htaccess"},
            "other content": {**HTACCESS, "content": "Allow from all",
                              "sha256": hashlib.sha256(b"Allow from all").hexdigest()},
            "newline": {**HTACCESS, "content": "Deny from all\n",
                        "sha256": hashlib.sha256(b"Deny from all\n").hexdigest()},
            "wrong hash": {**HTACCESS, "sha256": "0" * 64},
            "extra key": {**HTACCESS, "urls": ["https://example.com/.htaccess"]},
            "missing content": {"path": ".htaccess", "sha256": HTACCESS["sha256"]},
        }.items():
            with self.subTest(name):
                self.rejected(self.lock(generated_files=[entry]), "generated_files")
        self.rejected(self.lock(generated_files=[HTACCESS, HTACCESS]), "duplicate")
        self.rejected(self.lock(generated_files="x"), "generated_files")
        self.rejected(self.lock(generated_files=[HTACCESS], unmapped_files=[{"path": "x", "sha256": SHA}]), "unmapped_files")

    def test_an_archive_cannot_occupy_the_generated_path(self):
        self.rejected(self.lock([artifact(".htaccess")], generated_files=[HTACCESS]), "safe files/")


if __name__ == "__main__":
    unittest.main()
