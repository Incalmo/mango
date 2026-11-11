"""The declaration's mappings must cover each task and grader exactly."""

import json
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import yaml
from pydantic import ValidationError

HARNESS = Path(__file__).resolve().parents[2]
from mango.cli import execute, prepare_runtime
from mango.environment.declaration import EnvironmentManifest, load_manifest
from mango.environment.workspace import workspace_lock
from mango.runtime.manifest import BuildManifest
from tests.support import write_recipe


class EnvironmentManifestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "environment"
        self.task = self.root / "task"
        (self.task / "environment").mkdir(parents=True)
        (self.task / "task.toml").write_text("[environment]\n")
        write_recipe(self.root)
        self.document = {
            "name": "synthetic",
            "images": {"main": "images.main", "egress": "images.egress"},
            "services": {"main": "main"},
        }

    def manifest(self):
        return load_manifest(self.root, HARNESS)

    def multiple_verifiers(self):
        (self.task / "task.toml").write_text(
            '[environment]\n[verifier]\nenvironment_mode = "separate"\n'
            '[verifier.environment]\ndocker_image = "unused"\n'
            '[[steps]]\nname = "alpha"\n[[steps]]\nname = "beta"\n'
        )
        for step in ("alpha", "beta"):
            (self.task / "steps" / step / "tests").mkdir(parents=True)
        write_recipe(
            self.root,
            containers=("main", "egress", "gradeA", "gradeB", "gradeDb"),
            verifiers=(
                '"steps/alpha/tests" = { main.container = "gradeA";'
                ' database.container = "gradeDb"; };'
                ' "steps/beta/tests".main.container = "gradeB";'
            ),
        )

    def test_per_step_multiservice_verifiers_reach_runtime(self):
        self.multiple_verifiers()
        app = self.manifest()
        mappings = app.validate_task_mappings(self.root)
        keys = sorted(mappings)
        work = Path(self.directory.name) / "work"
        work.mkdir()
        drvs = ["/nix/store/placeholder.drv"] * len(app.attributes)
        (work / "inputs.json").write_text(json.dumps({"derivations": drvs}))
        refs = {
            name: f"test.invalid/{name}@sha256:" + str(index) * 64
            for index, name in enumerate(app.images, start=1)
        }
        with (
            patch("mango.cli.instantiate", return_value=drvs),
            patch("mango.cli.load", return_value=refs),
        ):
            execute(Namespace(action="load"), self.root, app, work, mappings)
        loaded = json.loads((work / "images.json").read_text())
        self.assertEqual(
            loaded["verifiers"][keys[0]],
            {"main": refs["gradeA"], "database": refs["gradeDb"]},
        )
        self.assertEqual(loaded["verifiers"][keys[1]], {"main": refs["gradeB"]})
        self.assertEqual(loaded["recipe_version"], 3)
        self.assertIn("environment", loaded["runtime"]["compose"])
        files = sorted((work / "runtime/steps").glob("*/tests/.incalmo-compose-*.yaml"))
        alpha, beta = [yaml.safe_load(file.read_text())["services"] for file in files]
        self.assertEqual(alpha["database"]["image"], refs["gradeDb"])
        self.assertEqual(beta["main"]["image"], refs["gradeB"])
        self.assertNotIn("build", alpha["main"])

    def test_environment_files_and_executable_modes_reach_generated_task(self):
        (self.root / "assets").mkdir()
        (self.root / "assets/run.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.root / "assets/run.sh").chmod(0o644)
        write_recipe(
            self.root,
            extra='environmentFiles.helpers = ./assets; executableFiles = [ "helpers/run.sh" ];',
        )
        app = self.manifest()
        self.assertEqual(app.environment_files, {"helpers": "assets"})
        work = Path(self.directory.name) / "work"
        manifest = BuildManifest(
            build_id="test",
            source_digest="sha256:" + "3" * 64,
            services={"main": "test.invalid/main@sha256:" + "1" * 64},
            egress_image="test.invalid/egress@sha256:" + "2" * 64,
            runtime=app.runtime(),
        )
        prepare_runtime(self.root, manifest, manifest.source_digest, workspace=work)
        copied = work / "runtime/environment/helpers/run.sh"
        self.assertEqual(copied.stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.root / "assets/run.sh").stat().st_mode & 0o777, 0o644)
        self.assertFalse((self.task / "environment/docker-compose.yaml").exists())

    def test_verifier_coverage_is_exact(self):
        (self.task / "tests").mkdir()
        (self.task / "task.toml").write_text(
            '[environment]\n[verifier]\nenvironment_mode="separate"\n'
            '[verifier.environment]\ndocker_image="unused"\n'
        )
        with self.assertRaisesRegex(ValueError, "cover task contexts exactly"):
            self.manifest()
        write_recipe(
            self.root,
            containers=("main", "egress", "grader"),
            verifiers='tests.main.container = "grader";',
        )
        self.assertEqual(
            list(self.manifest().validate_task_mappings(self.root).values()),
            [{"main": "grader"}],
        )

    def test_unknown_images_output_collisions_and_unknown_fields(self):
        for extra in [
            {"services": {"main": "missing"}},
            {"services": {"other": "main"}},
            {"builds": {"main": "application"}},
            {"imagess": {}},
            {"images": {"../escape": "image", "egress": "egress"}},
            {"verifiers": {"tests/variant": {"main": "missing"}}},
            {"verifiers": {"tests/variant": {"database": "main"}}},
        ]:
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                EnvironmentManifest.model_validate(self.document | extra)

    def test_paths_rejected(self):
        for extra in [
            {"task": "../task"},
            {"task": "/task"},
            {"environment_files": {"../outside": "source"}},
            {"executable_files": ["a/../../outside"]},
        ]:
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                EnvironmentManifest.model_validate(self.document | extra)

    def test_symlink_source_escape_rejected(self):
        (self.root / "outside").symlink_to(
            Path(self.directory.name), target_is_directory=True
        )
        write_recipe(self.root, extra="environmentFiles.helpers = ./outside;")
        with self.assertRaisesRegex(ValueError, "local to the environment|escapes"):
            self.manifest()

    def test_authored_scaffolding_is_rejected(self):
        (self.task / "environment/Dockerfile").write_text("FROM scratch\n")
        app = self.manifest()
        with self.assertRaisesRegex(ValueError, "scaffolding"):
            prepare_runtime(
                self.root,
                BuildManifest(
                    build_id="test",
                    source_digest="sha256:" + "3" * 64,
                    services={"main": "test.invalid/main@sha256:" + "1" * 64},
                    egress_image="test.invalid/egress@sha256:" + "2" * 64,
                    runtime=app.runtime(),
                ),
                "sha256:" + "3" * 64,
                workspace=Path(self.directory.name) / "work",
            )

    def test_declaration_action_prints_declaration_and_runtime(self):
        result = subprocess.run(
            [sys.executable, "-m", "mango", str(self.root), "declaration"],
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["name"], "synthetic")
        self.assertIn("main", document["compose"]["services"])
        self.assertEqual(document["runtime"]["task"], "task")
        self.assertFalse((self.root.parent / "build").exists())

    def test_harbor_arguments_only_with_run(self):
        result = subprocess.run(
            [sys.executable, "-m", "mango", str(self.root), "validate", "--", "--model", "x"],
            text=True, capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only accepted by run", result.stderr)

    def test_validate_needs_no_build_or_workspace(self):
        result = subprocess.run(
            [sys.executable, "-m", "mango", str(self.root), "validate"],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["valid"])
        self.assertFalse((self.root.parent / "build").exists())


class WorkspaceTests(unittest.TestCase):
    def test_same_workspace_is_exclusive_different_workspace_is_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "work"
            other = Path(directory) / "other"
            code = (
                "import sys; from pathlib import Path; "
                "from mango.environment.workspace import workspace_lock\n"
                "with workspace_lock(Path(sys.argv[1])): pass\n"
            )

            def attempt(path):
                return subprocess.run(
                    [sys.executable, "-c", code, str(path)],
                    text=True,
                    capture_output=True,
                )

            with workspace_lock(work):
                busy = attempt(work)
                self.assertNotEqual(busy.returncode, 0)
                self.assertIn("Workspace is already in use", busy.stderr)
                self.assertEqual(attempt(other).returncode, 0)
            self.assertEqual(attempt(work).returncode, 0)


if __name__ == "__main__":
    unittest.main()
