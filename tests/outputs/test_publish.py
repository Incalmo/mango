"""Hosted publication: push without a daemon and emit a self-describing manifest."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import yaml

REPO = Path(__file__).resolve().parents[2]
from nix2rle import cli as standalone
from nix2rle.authoring import pin_base
from nix2rle.cli import execute
from nix2rle.environment.declaration import load_manifest
from nix2rle.outputs import oci
from nix2rle.runtime.manifest import BuildManifest
from nix2rle.runtime.materialize import materialize_runtime
from nix2rle.runtime.task import generate_task
from tests.support import separate_grader_toml, write_recipe

DIGEST = "sha256:" + "a" * 64


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.work = Path(self.temp.name) / "work"
        (self.root / "task/tests").mkdir(parents=True)
        (self.root / "task/task.toml").write_text(separate_grader_toml())
        (self.root / "task/instruction.md").write_text("Solve it.\n")
        write_recipe(
            self.root,
            containers=("main", "egress", "grader"),
            services='main = { container = "main"; command = [ "sleep" "infinity" ]; };',
            verifiers='tests.main.container = "grader";',
        )
        self.app = load_manifest(self.root, REPO)

    def publish(self, **options):
        drvs = ["/nix/store/test.drv"] * len(self.app.attributes)
        pushed = {}

        def push(images, repository, **kwargs):
            pushed.update(repository=repository, images=sorted(images))
            return {
                name: f"{repository}/{name}@sha256:" + str(i) * 64
                for i, name in enumerate(sorted(images), 1)
            }

        args = Namespace(
            action="publish",
            repository="registry.test/env/build-1",
            build_id="build-1",
            source_digest=DIGEST,
            authfile=None,
            output=self.work / "manifest.json",
        )
        for key, value in options.items():
            setattr(args, key, value)
        self.work.mkdir(exist_ok=True)
        with (
            patch("nix2rle.cli.instantiate", return_value=drvs),
            patch("nix2rle.cli.subprocess.run"),
            patch("nix2rle.cli.realise_derivations"),
            patch("nix2rle.cli.push", side_effect=push),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            execute(args, self.root, self.app, self.work, self.app.verifiers)
        return BuildManifest.model_validate_json(output.getvalue()), pushed

    def test_publish_pushes_every_image_and_binds_source_digest(self):
        manifest, pushed = self.publish()
        self.assertEqual(pushed["repository"], "registry.test/env/build-1")
        self.assertEqual(pushed["images"], ["egress", "grader", "main"])
        self.assertEqual(manifest.source_digest, DIGEST)
        self.assertEqual(manifest.recipe_version, 3)
        self.assertTrue(manifest.services["main"].startswith("registry.test/env/build-1/main@"))
        self.assertTrue(manifest.egress_image.startswith("registry.test/env/build-1/egress@"))
        (grader,) = manifest.verifiers.values()
        self.assertIn("/grader@", grader["main"])
        saved = BuildManifest.model_validate_json((self.work / "manifest.json").read_text())
        self.assertEqual(saved, manifest)

    def test_publish_requires_explicit_identity(self):
        with self.assertRaisesRegex(ValueError, "--source-digest"):
            self.publish(source_digest=None)

    def test_manifest_alone_regenerates_the_task_without_nix(self):
        manifest, _ = self.publish()
        task = Path(self.temp.name) / "generated"
        # No Nix evaluation is available or needed beyond this point.
        with patch("subprocess.run", side_effect=AssertionError("no subprocess")):
            generate_task(self.root, manifest.runtime, task)
            materialize_runtime(task, manifest, DIGEST)
        runtime = yaml.safe_load((task / "environment/docker-compose.yaml").read_text())
        self.assertEqual(runtime["services"]["main"]["image"], manifest.services["main"])
        self.assertEqual(runtime["services"]["main"]["command"], ["sleep", "infinity"])
        self.assertEqual((task / "instruction.md").read_text(), "Solve it.\n")
        self.assertTrue(list((task / "tests").glob(".incalmo-images-*.json")))
        self.assertFalse((self.root / "task/environment").exists())
        with self.assertRaisesRegex(ValueError, "destination exists"):
            generate_task(self.root, manifest.runtime, task)


class FailureClassificationTests(unittest.TestCase):
    def realise(self, log, status):
        script = f"import sys; sys.stderr.write({log!r}); sys.exit({status})"

        real = subprocess.Popen

        def popen(argv, **kwargs):
            return real([sys.executable, "-c", script], **kwargs)

        with (
            patch("nix2rle.cli.subprocess.Popen", side_effect=popen),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            standalone.realise_derivations(["/nix/store/x.drv"], Path("/"))

    def test_download_failure_is_retryable_and_build_failure_is_not(self):
        with self.assertRaises(standalone.Failure) as raised:
            self.realise("error: unable to download 'https://x': HTTP error 503\n", 1)
        self.assertEqual(raised.exception.status, standalone.EXIT_INPUT_DOWNLOAD)
        with self.assertRaises(standalone.Failure) as raised:
            self.realise("error: builder for '/nix/store/x.drv' failed\n", 1)
        self.assertEqual(raised.exception.status, standalone.EXIT_BUILD)
        self.realise("building\n", 0)

    def test_cli_reports_evaluation_failure_as_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "env"
            (root / "task").mkdir(parents=True)
            (root / "task/task.toml").write_text("[environment]\n")
            (root / "default.nix").write_text('{ harness }: throw "broken"')
            result = subprocess.run(
                [sys.executable, "-m", "nix2rle", str(root), "validate"],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, standalone.EXIT_EVALUATION)
        self.assertEqual(
            json.loads(result.stderr.strip().splitlines()[-1])["error"], "evaluation"
        )


class PushTests(unittest.TestCase):
    def test_push_preserves_layout_digest_and_rejects_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = Path(directory) / "layout"
            layout.mkdir()
            (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
            (layout / "index.json").write_text(
                json.dumps({"manifests": [{"digest": DIGEST}]})
            )
            calls = []

            def skopeo(command, **kwargs):
                calls.append(command)
                Path(command[command.index("--digestfile") + 1]).write_text(DIGEST)
                return subprocess.CompletedProcess(command, 0)

            with patch("nix2rle.outputs.oci.subprocess.run", side_effect=skopeo):
                reference = oci.push_one("main", layout, "registry.test/r/")
            self.assertEqual(reference, f"registry.test/r/main@{DIGEST}")
            (command,) = calls
            self.assertIn("--preserve-digests", command)
            self.assertEqual(command[-2], f"oci:{layout.resolve()}")
            self.assertTrue(command[-1].startswith("docker://registry.test/r/main:"))

            def wrong(command, **kwargs):
                Path(command[command.index("--digestfile") + 1]).write_text(
                    "sha256:" + "b" * 64
                )
                return subprocess.CompletedProcess(command, 0)

            with patch("nix2rle.outputs.oci.subprocess.run", side_effect=wrong):
                with self.assertRaisesRegex(RuntimeError, "differs"):
                    oci.push_one("main", layout, "registry.test/r")


class AuthoringToolTests(unittest.TestCase):
    def test_pin_base_reads_hash_from_nix_mismatch(self):
        stderr = (
            "error: hash mismatch in fixed-output derivation:\n"
            "         specified: sha256-AAAA\n"
            "            got:    sha256-VJrEWNzP7YSUs7FXJ6UIbPd7YlPrIGXCxJ2UAbL9iCc=\n"
        )
        with patch(
            "nix2rle.authoring.pin_base.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, "", stderr),
        ):
            value = pin_base.pin("alpine@" + DIGEST)
        self.assertEqual(
            value,
            {
                "image": "docker.io/library/alpine",
                "digest": DIGEST,
                "hash": "sha256-VJrEWNzP7YSUs7FXJ6UIbPd7YlPrIGXCxJ2UAbL9iCc=",
            },
        )
        self.assertIn('digest = "' + DIGEST + '";', pin_base.nix(value))


if __name__ == "__main__":
    unittest.main()
