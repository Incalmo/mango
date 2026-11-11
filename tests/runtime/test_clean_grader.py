"""Image-only graders pass Harbor's conventional definition check."""

import tempfile
import unittest
from pathlib import Path

import yaml
from harbor.environments.definition import require_agent_environment_definition

REPO = Path(__file__).resolve().parents[2]
from mango.runtime.manifest import BuildManifest, RuntimeDeclaration
from mango.runtime.materialize import materialize_runtime, verifier_environments


class CleanGraderTests(unittest.TestCase):
    def test_missing_conventional_definition_gets_fallback_without_collapsing_variants(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory)
            (task / "environment").mkdir()
            (task / "environment/docker-compose.yaml").write_text(
                "services:\n  main:\n    image: unused\n"
            )
            (task / "tests").mkdir()
            (task / "task.toml").write_text(
                '[environment]\n[[steps]]\nname="a"\n'
                '[steps.verifier]\nenvironment_mode="separate"\n'
                '[steps.verifier.environment]\ndocker_image="a"\n'
                '[[steps]]\nname="b"\n'
                '[steps.verifier]\nenvironment_mode="separate"\n'
                '[steps.verifier.environment]\ndocker_image="b"\n'
            )
            variants = verifier_environments(task)
            refs = ["test.invalid/grader@sha256:" + str(n) * 64 for n in [1, 2]]
            manifest = BuildManifest(
                build_id="test",
                source_digest="sha256:" + "0" * 64,
                services={"main": refs[0]},
                egress_image=refs[0],
                verifiers={key: {"main": ref} for key, ref in zip(variants, refs)},
                runtime=RuntimeDeclaration(
                    task="task", compose={"environment": {"services": {"main": {}}}}
                ),
            )
            materialize_runtime(task, manifest, manifest.source_digest)
            require_agent_environment_definition(task / "tests", docker_image=None)
            self.assertFalse((task / "tests/Dockerfile").exists())
            rendered = [
                yaml.safe_load(path.read_text())
                for path in (task / "tests").glob(".incalmo-compose-*.yaml")
            ]
            self.assertEqual(
                {doc["services"]["main"]["image"] for doc in rendered}, set(refs)
            )
            self.assertTrue(
                all("build" not in doc["services"]["main"] for doc in rendered)
            )


if __name__ == "__main__":
    unittest.main()
