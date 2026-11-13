import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("nix-instantiate"), "Nix evaluation unavailable")
class NpmCacheArchiveTests(unittest.TestCase):
    def evaluate(self, document):
        adapter = (
            Path(__file__).resolve().parents[2] / "lib/adapters/npm-cache-archive.nix"
        )
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "receipt.json"
            receipt.write_text(json.dumps(document))
            expression = """let
              pkgs = {
                lib = { assertMsg = condition: message: condition; escapeShellArg = value: value; };
                python3 = "/python"; coreutils = "/coreutils";
                runCommand = name: attrs: script: { inherit name script; };
              };
              adapter = import ADAPTER { inherit pkgs; };
            in (adapter { archive = "/archive"; receipt = RECEIPT; }).name"""
            expression = expression.replace("ADAPTER", str(adapter)).replace(
                "RECEIPT", str(receipt)
            )
            return subprocess.run(
                ["nix-instantiate", "--eval", "--strict", "--expr", expression],
                capture_output=True,
                text=True,
            )

    def receipt(self):
        return {
            "status": "completed",
            "verified_by_replay": True,
            "offline": {
                "verified_by_replay": True,
                "replay_error": None,
                "replay": {"ran": True},
            },
            "cache_archive": {
                "lifecycle_replay_network_disabled": True,
                "installed_package_selection_equal": True,
                "sha256": "a" * 64,
                "path": "npm/cache/" + "a" * 64 + ".tar.gz",
            },
        }

    def test_verified_receipt_evaluates_without_building(self):
        result = self.evaluate(self.receipt())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("npm-cache.tar.gz", result.stdout)

    def test_unverified_lifecycle_and_wrong_archive_identity_are_rejected(self):
        for field, value in [
            ("lifecycle_replay_network_disabled", False),
            ("path", "other.tar.gz"),
        ]:
            document = self.receipt()
            document["cache_archive"][field] = value
            with self.subTest(field=field):
                self.assertNotEqual(self.evaluate(document).returncode, 0)
