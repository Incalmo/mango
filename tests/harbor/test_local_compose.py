"""Render real Compose configurations using disposable authored task fixtures."""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
from mango.cli import build_digest, execute
from mango.environment.declaration import load_manifest
from mango.harbor.compose import verify_local_images
from mango.runtime.manifest import BuildManifest
from mango.runtime.materialize import SIDECAR_SERVICE
from tests.support import write_recipe


class LocalComposeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.work = Path(self.temp.name) / "work"
        self.work.mkdir()
        task = self.root / "task"
        (task / "environment").mkdir(parents=True)
        (task / "tests").mkdir()
        (task / "task.toml").write_text(
            '[environment]\nnetwork_mode="no-network"\n'
            '[verifier]\nenvironment_mode="separate"\n'
            '[verifier.environment]\ndocker_image="old-grader"\n'
            'network_mode="no-network"\n'
        )
        (task / "environment/asset.txt").write_text("preserved")
        self.write_recipe()
        self.app = load_manifest(self.root, REPO)
        self.verifiers = self.app.validate_task_mappings(self.root)
        self.drvs = ["/nix/store/test.drv"] * 4

        def ref(name, n):
            return "test.invalid/" + name + "@sha256:" + str(n) * 64

        self.manifest = BuildManifest(
            build_id="test",
            source_digest=build_digest(
                self.drvs, self.app.model_dump(), self.verifiers
            ),
            services={"main": ref("main", 1), "db": ref("db", 2)},
            egress_image=ref("egress", 3),
            verifiers={
                key: {"main": ref("grader", 4), "db": ref("db", 2)}
                for key in self.verifiers
            },
            runtime=self.app.runtime(),
        )
        (self.work / "images.json").write_text(self.manifest.model_dump_json())

    def write_recipe(self, volume="./asset.txt:/data/asset:ro"):
        write_recipe(
            self.root,
            containers=("main", "db", "egress", "grader"),
            services=(
                'main = { container = "main";'
                ' command = [ "sh" "-c" "echo $$HOME; sleep infinity" ];'
                f' volumes = [ "{volume}" ]; }};'
                ' db = { container = "db"; networks = [ "data" ]; };'
            ),
            verifiers='tests = { main.container = "grader"; db.container = "db"; };',
            extra="networks.data.internal = true;",
        )

    def compose(self):
        with (
            patch("mango.cli.instantiate", return_value=self.drvs),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            execute(
                Namespace(action="compose"),
                self.root,
                self.app,
                self.work,
                self.verifiers,
            )
        return json.loads(output.getvalue())

    def test_missing_load_and_stale_mapping_fail(self):
        receipt = self.work / "images.json"
        receipt.unlink()
        with self.assertRaisesRegex(ValueError, "run fetch and load first"):
            self.compose()
        receipt.write_text(self.manifest.model_dump_json())
        self.app.services["main"] = "db"
        with self.assertRaisesRegex(ValueError, "image mappings; run load again"):
            self.compose()
        self.assertFalse((self.work / "compose").exists())

    def test_stale_grader_mapping_fails(self):
        key = next(iter(self.app.verifiers))
        self.app.verifiers[key]["main"] = "db"
        with self.assertRaisesRegex(ValueError, "image mappings; run load again"):
            self.compose()
        self.assertFalse((self.work / "compose").exists())

    def test_missing_local_image_does_not_pull(self):
        with patch(
            "mango.harbor.compose.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1),
        ) as run:
            with self.assertRaisesRegex(ValueError, "Preloaded image missing"):
                verify_local_images(self.manifest)
        self.assertEqual(run.call_args.args[0][:3], ["docker", "image", "inspect"])

    def test_invalid_volume_reports_compose_error_without_publishing(self):
        self.write_recipe(volume="missing:/data")
        with patch("mango.harbor.compose.verify_local_images"):
            with self.assertRaisesRegex(ValueError, "undefined volume missing"):
                self.compose()
        self.assertFalse((self.work / "compose").exists())

    def test_real_compose_preserves_topology_assets_and_separate_grader(self):
        with patch("mango.harbor.compose.verify_local_images"):
            report = self.compose()
        self.assertEqual(len(report["graders"]), 1)
        self.assertIn("oracle", report["grading"])
        self.assertIn("does not run", report["note"])
        for entry in [report["runtime"], *report["graders"]]:
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    entry["path"],
                    "config",
                    "--format",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            document = json.loads(result.stdout)
            services = document["services"]
            self.assertIn(SIDECAR_SERVICE, services)
            self.assertEqual(
                services["main"]["network_mode"], "service:" + SIDECAR_SERVICE
            )
            for service in services.values():
                self.assertNotIn("build", service)
                self.assertEqual(service["pull_policy"], "never")
                self.assertIn("@sha256:", service["image"])
            self.assertIn("--no-build", entry["up"])
            self.assertEqual(entry["up"][-2:], ["--pull", "never"])
        main = json.loads(Path(report["runtime"]["path"]).read_text())["services"]
        self.assertIn("data", main["db"]["networks"])
        self.assertNotIn("network_mode", main["db"])
        self.assertEqual(
            Path(main["main"]["volumes"][0]["source"]).read_text(), "preserved"
        )
        self.assertIn("$$HOME", main["main"]["command"][-1])


if __name__ == "__main__":
    unittest.main()
