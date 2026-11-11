import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
from nix2rle.cli import EXIT_EVALUATION, Failure
from nix2rle.environment.contract import validate_factory_task


class FactoryContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "environment"
        (self.root / "task/tests").mkdir(parents=True)
        (self.root / "task/environment").mkdir()
        (self.root / "task/task.toml").write_text(
            '[environment]\n[verifier]\nenvironment_mode="shared"\n'
        )
        self.recipe = """{ harness }:
let h = import harness {}; output = h.pkgs.runCommand "factory-test" {} "mkdir $out";
in h.mkEnvironment {
 name = "factory-test"; task = ./task;
 containers = { main = output; egress = output; };
 services.main.container = "main";
 builds.application = APPLICATION;
}
"""
        self.write("output")

    def write(self, application):
        (self.root / "default.nix").write_text(self.recipe.replace("APPLICATION", application))

    def test_evaluates_every_output_but_does_not_claim_build(self):
        result = validate_factory_task(self.root)
        self.assertEqual(set(result["derivations"]), {"main", "egress", "application"})
        self.assertFalse(result["built"])
        self.assertFalse(result["qualified"])

    def test_lazy_broken_application_is_rejected(self):
        self.write('throw "application is not pinned"')
        with self.assertRaises(Failure) as raised:
            validate_factory_task(self.root)
        self.assertEqual(raised.exception.status, EXIT_EVALUATION)

    def test_old_scaffolding_is_rejected(self):
        (self.root / "task/environment/Dockerfile").write_text("FROM scratch")
        with self.assertRaisesRegex(ValueError, "scaffolding"):
            validate_factory_task(self.root)

    def test_external_symlink_is_rejected(self):
        (self.root / "shared").symlink_to(self.root.parent)
        with self.assertRaisesRegex(ValueError, "external symlink"):
            validate_factory_task(self.root)

    def test_verifier_scaffolding_is_rejected(self):
        (self.root / "task/tests/docker-compose.yaml").write_text("services: {}\n")
        with self.assertRaisesRegex(ValueError, "scaffolding"):
            validate_factory_task(self.root)

    def test_build_result_symlink_is_rejected(self):
        (self.root / "result").symlink_to("/nix/store")
        with self.assertRaisesRegex(ValueError, "external symlink"):
            validate_factory_task(self.root)


if __name__ == "__main__":
    unittest.main()
