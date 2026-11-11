"""The authored Nix declaration supplies mappings and Compose without duplication."""

import contextlib
import io
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import yaml

REPO = Path(__file__).resolve().parents[2]
from mango.cli import execute
from mango.environment.declaration import evaluate_declaration, load_manifest


class DeclarativeEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "customer"
        self.work = Path(self.temp.name) / "work"
        (self.root / "task/environment").mkdir(parents=True)
        (self.root / "task/tests").mkdir()
        (self.root / "task/task.toml").write_text(
            '[environment]\n[verifier]\nenvironment_mode="separate"\n'
            '[verifier.environment]\ndocker_image="grader"\n'
        )
        (self.root / "task/environment/asset.txt").write_text("keep")
        self.write_recipe()

    def write_recipe(
        self,
        services='main = { container = "main"; volumes = [ "./asset.txt:/asset:ro" ]; };',
        verifiers='tests = { main.container = "grader"; };',
    ):
        (self.root / "default.nix").write_text(
            """{ harness }:
let h = import harness {}; mkEnvironment = import (harness + "/lib/environment.nix") { inherit (h) pkgs; };
in mkEnvironment {
 name = "synthetic"; task = ./task;
 containers = { main = throw "image forced during metadata evaluation";
                grader = throw "grader forced during metadata evaluation";
                egress = throw "egress forced during metadata evaluation"; };
 builds.application = throw "application forced during metadata evaluation";
 services = { """
            + services
            + """ };
 networks.internal.internal = true;
 verifiers = { """
            + verifiers
            + """ };
}
"""
        )

    def test_metadata_evaluation_is_lazy_and_needs_no_manifest_or_compose(self):
        declaration = evaluate_declaration(self.root, REPO)
        self.assertEqual(declaration["manifest"]["images"]["main"], "images.main")
        self.assertEqual(
            declaration["manifest"]["builds"], {"application": "builds.application"}
        )
        self.assertFalse((self.root / "manifest.json").exists())
        self.assertFalse((self.root / "task/environment/docker-compose.yaml").exists())
        app = load_manifest(self.root, REPO)
        self.assertEqual(app.task, "task")
        mapping = app.validate_task_mappings(self.root)
        self.assertEqual(next(iter(mapping.values())), {"main": "grader"})

    def test_load_materializes_complete_runtime_and_preserves_source(self):
        app = load_manifest(self.root, REPO)
        mapping = app.validate_task_mappings(self.root)
        self.work.mkdir()
        drvs = ["/nix/store/test.drv"] * len(app.attributes)
        (self.work / "inputs.json").write_text(json.dumps({"derivations": drvs}))
        refs = {
            name: "test.invalid/" + name + "@sha256:" + str(i) * 64
            for i, name in enumerate(app.images, 1)
        }
        with (
            patch("mango.cli.HARNESS", REPO),
            patch("mango.cli.instantiate", return_value=drvs),
            patch("mango.cli.load", return_value=refs),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            execute(Namespace(action="load"), self.root, app, self.work, mapping)
        runtime = self.work / "runtime"
        doc = yaml.safe_load((runtime / "environment/docker-compose.yaml").read_text())
        self.assertEqual(doc["services"]["main"]["image"], refs["main"])
        self.assertTrue(doc["networks"]["internal"]["internal"])
        self.assertEqual((runtime / "environment/asset.txt").read_text(), "keep")
        grader = yaml.safe_load(
            next((runtime / "tests").glob(".incalmo-compose-*.yaml")).read_text()
        )
        self.assertEqual(grader["services"]["main"]["image"], refs["grader"])
        self.assertFalse((self.root / "task/environment/docker-compose.yaml").exists())

    def test_declaration_generates_usable_local_compose(self):
        self.test_load_materializes_complete_runtime_and_preserves_source()
        app = load_manifest(self.root, REPO)
        with (
            patch("mango.cli.HARNESS", REPO),
            patch(
                "mango.cli.instantiate",
                return_value=["/nix/store/test.drv"] * len(app.attributes),
            ),
            patch("mango.harbor.compose.verify_local_images"),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            execute(
                Namespace(action="compose"),
                self.root,
                app,
                self.work,
                app.validate_task_mappings(self.root),
            )
        report = json.loads(output.getvalue())
        self.assertEqual(len(report["graders"]), 1)
        document = json.loads(Path(report["runtime"]["path"]).read_text())
        self.assertEqual(document["services"]["main"]["pull_policy"], "never")
        self.assertNotIn("build", document["services"]["main"])

    def test_unknown_container_build_and_missing_grader_rejected(self):
        for services in [
            'main.container = "unknown";',
            'main = {container="main"; build=".";};',
        ]:
            self.write_recipe(services=services)
            with self.assertRaises(Exception):
                evaluate_declaration(self.root, REPO)
        self.write_recipe(verifiers="")
        with self.assertRaisesRegex(ValueError, "cover task contexts exactly"):
            load_manifest(self.root, REPO)


if __name__ == "__main__":
    unittest.main()
