"""Test external environments and relocation using disposable inputs only."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
from mango.cli import build_digest, export_source, instantiate
from mango.environment.declaration import load_manifest
from mango.runtime.materialize import verifier_environments
from tests.support import separate_grader_toml


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / "customer-source"
        self.source.mkdir()
        (self.source / "input.txt").write_text("test input")
        task = self.source / "task"
        (task / "environment").mkdir(parents=True)
        (task / "tests").mkdir()
        (task / "task.toml").write_text(separate_grader_toml("unused"))
        self.write_recipe(self.source)
        self.root = Path(self.directory.name) / "bundle"
        export_source(self.source, self.root)
        self.bundle = self.root / "environment"
        self.state = self.root / "build/environment"
        self.state.mkdir(parents=True)
        app = load_manifest(self.bundle, REPO)
        self.attributes = list(app.attributes.values())
        self.manifest = {
            "build_id": "test",
            "source_digest": build_digest(
                instantiate(self.bundle, self.attributes),
                app.model_dump(),
                app.validate_task_mappings(self.bundle),
            ),
            "services": {"main": "test.invalid/main@sha256:" + "1" * 64},
            "verifiers": {
                key: {"main": "test.invalid/verifier@sha256:" + "2" * 64}
                for key in verifier_environments(self.bundle / "task")
            },
            "egress_image": "test.invalid/egress@sha256:" + "3" * 64,
            "runtime": app.runtime().model_dump(),
        }
        self.write_manifest()

    def write_recipe(self, root, main="main", grader="grader"):
        (root / "default.nix").write_text(
            "{ harness }:\n"
            "let h = import harness {};\n"
            '    image = h.pkgs.writeText "test-image" (builtins.readFile ./input.txt);\n'
            "in h.mkEnvironment {\n"
            '  name = "export-test"; task = ./task;\n'
            "  containers = { main = image; grader = image; egress = image; };\n"
            f'  services.main = {{ container = "{main}"; command = [ "sleep" "infinity" ]; }};\n'
            f'  verifiers.tests.main.container = "{grader}";\n'
            "}\n"
        )

    def write_manifest(self):
        (self.state / "images.json").write_text(json.dumps(self.manifest))

    def command(self, action, *args):
        return subprocess.run(
            [sys.executable, "-m", "mango", str(self.bundle), action, *args],
            # The relocated bundle's own package, never this checkout's.
            env=dict(os.environ, PYTHONPATH=str(self.root / "src")),
            cwd="/",
            capture_output=True,
            text=True,
            check=False,
        )

    def test_relocated_export_preserves_runtime_and_verifier(self):
        self.assertEqual(
            instantiate(self.source, self.attributes),
            instantiate(self.bundle, self.attributes),
        )
        result = self.command("prepare")
        self.assertEqual(result.returncode, 0, result.stderr)
        runtime = yaml.safe_load(
            (self.state / "runtime/environment/docker-compose.yaml").read_text()
        )
        self.assertEqual(runtime["services"]["main"]["command"], ["sleep", "infinity"])
        self.assertIn("@sha256:", runtime["services"]["main"]["image"])
        self.assertEqual(
            len(list((self.state / "runtime/tests").glob(".mango-images-*.json"))), 1
        )
        for excluded in ("examples", ".jj", ".git", "tests", "docs"):
            self.assertFalse((self.root / excluded).exists(), excluded)
        self.assertTrue((self.root / "lib/images/egress/HARBOR-LICENSE").exists())

    def test_comment_only_edit_keeps_build(self):
        before = instantiate(self.bundle, self.attributes)
        with (self.bundle / "default.nix").open("a") as stream:
            stream.write("\n# changed\n")
        self.assertEqual(before, instantiate(self.bundle, self.attributes))
        self.assertEqual(self.command("prepare").returncode, 0)

    def test_stale_manifest_cannot_replace_runtime(self):
        self.assertEqual(self.command("prepare").returncode, 0)
        runtime = self.state / "runtime/environment/docker-compose.yaml"
        original = runtime.read_bytes()
        (self.bundle / "input.txt").write_text("different input")
        self.assertNotEqual(self.command("prepare").returncode, 0)
        self.assertEqual(runtime.read_bytes(), original)

    def test_missing_service_or_verifier_rejected(self):
        for field in ["services", "verifiers"]:
            previous = self.manifest[field].copy()
            self.manifest[field].clear()
            self.write_manifest()
            self.assertNotEqual(self.command("prepare").returncode, 0)
            self.manifest[field] = previous

    def test_recipe_change_requires_fetch_before_load(self):
        (self.state / "inputs.json").write_text(
            json.dumps(
                {
                    "derivations": instantiate(self.bundle, self.attributes),
                }
            )
        )
        (self.bundle / "input.txt").write_text("changed")
        result = self.command("load")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Environment changed since fetch", result.stderr)

    def test_changed_service_or_verifier_mapping_requires_load(self):
        for main, grader in (("grader", "grader"), ("main", "main")):
            with self.subTest(main=main, grader=grader):
                self.write_recipe(self.bundle, main=main, grader=grader)
                for action in ("prepare", "run"):
                    result = self.command(action)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("image mappings; run load again", result.stderr)
                    self.assertFalse((self.state / "runtime").exists())
        self.write_recipe(self.bundle)
        self.assertEqual(self.command("prepare").returncode, 0)

    def test_unsafe_export_and_workspace_rejected(self):
        with self.assertRaises(ValueError):
            export_source(self.source, self.source / "nested")
        with self.assertRaises(ValueError):
            export_source(self.source, self.root)
        result = self.command("prepare", "--workspace", str(self.bundle / "work"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.bundle / "work").exists())


if __name__ == "__main__":
    unittest.main()
