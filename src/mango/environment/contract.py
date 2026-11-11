"""Validate an explicitly authored environment, without discovering dependencies."""

from pathlib import Path

from mango import HARNESS
from mango.cli import instantiate
from mango.environment.declaration import load_manifest
from mango.runtime.task import validate

FORMAT = "mango-environment/1"


def validate_environment_contract(root: Path) -> dict:
    """Check layout, declaration and that every output evaluates to a derivation.

    This checks the environment contract before building. It builds nothing and
    claims neither a build nor a qualification result.
    """
    root = root.resolve(strict=True)
    if not (root / "default.nix").is_file():
        raise ValueError("Environment requires default.nix returning mkEnvironment")
    for path in root.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(root):
            raise ValueError(
                f"Environment has an external symlink: {path.relative_to(root)}"
            )
    app = load_manifest(root, HARNESS)
    verifiers = app.validate_task_mappings(root)
    validate(root, app.runtime())
    attributes = {**app.images, **app.builds}
    drvs = instantiate(root, attributes.values())
    if len(drvs) != len(attributes) or any(not drv.endswith(".drv") for drv in drvs):
        raise ValueError(
            "Every declared image and application build must evaluate to a derivation"
        )
    return {
        "valid": True,
        "format": FORMAT,
        "name": app.name,
        "task": app.task,
        "services": app.services,
        "verifiers": verifiers,
        "derivations": dict(zip(attributes, drvs, strict=True)),
        "built": False,
        "qualified": False,
    }
