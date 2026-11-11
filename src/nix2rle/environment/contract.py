"""Validate an explicitly authored factory output, without discovering dependencies."""

from pathlib import Path

from nix2rle import HARNESS
from nix2rle.cli import instantiate
from nix2rle.environment.declaration import load_manifest
from nix2rle.runtime.task import validate

FORMAT = "nix2rle-environment/1"


def validate_factory_task(root: Path) -> dict:
    """Check layout, declaration and that every output evaluates to a derivation.

    This is the contract a hosted builder applies first. It builds nothing and
    claims neither a build nor a qualification result.
    """
    root = root.resolve(strict=True)
    if not (root / "default.nix").is_file():
        raise ValueError("Factory output requires default.nix returning mkEnvironment")
    for path in root.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(root):
            raise ValueError(
                f"Factory output has an external symlink: {path.relative_to(root)}"
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
