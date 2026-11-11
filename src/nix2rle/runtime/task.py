"""Generate a Harbor task from a nix2rle source tree and its runtime declaration.

Pure Python: no Nix evaluation and no image access. The local runner and the
hosted execution workers share this step, so a declaration evaluated once on a
builder produces the same task wherever it is applied. Hosts that vendor this
module must keep their copy byte-compatible apart from import lines.
"""

import shutil
from pathlib import Path

import yaml

from nix2rle.runtime.manifest import RuntimeDeclaration

COMPOSE_NAMES = ("docker-compose.yaml", "docker-compose.yml")


def relative_path(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or "\0" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError(f"Expected a relative path without traversal: {value!r}")
    return value


def _inside(root: Path, value: str) -> Path:
    path = root / relative_path(value)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes its root through a symlink: {value}")
    return path


def validate(root: Path, runtime: RuntimeDeclaration) -> None:
    """Reject declarations whose paths leave the source tree."""
    task = _inside(root, runtime.task)
    if not (task / "task.toml").is_file():
        raise ValueError(f"Declared task has no task.toml: {runtime.task}")
    if "environment" not in runtime.compose:
        raise ValueError("Runtime declaration requires the environment Compose")
    for context in runtime.compose:
        relative_path(context)
    for target, source in runtime.environment_files.items():
        relative_path(target)
        if not _inside(root, source).is_dir():
            raise ValueError(f"Environment file source is not a directory: {source}")
    for name in runtime.executable_files:
        relative_path(name)
    for path in task.rglob("*"):
        if path.name in COMPOSE_NAMES or path.name == "Dockerfile":
            raise ValueError(
                "Source task must not author build or runtime scaffolding: "
                f"{path.relative_to(root)}"
            )


def generate_task(root: Path, runtime: RuntimeDeclaration, destination: Path) -> Path:
    """Write the generated Harbor task to ``destination`` (which must not exist).

    The source tree is never modified. Compose documents keep their placeholder
    images; ``materialize_runtime`` binds them to the image manifest.
    """
    root = root.resolve()
    validate(root, runtime)
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Generated task destination exists: {destination}")
    shutil.copytree(root / runtime.task, destination, symlinks=True)
    for context, document in runtime.compose.items():
        directory = destination / context
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "docker-compose.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False)
        )
    environment = destination / "environment"
    for target, source in runtime.environment_files.items():
        # Compose bind-mounts task assets relative to the environment directory.
        shutil.copytree(_inside(root, source), environment / target, symlinks=True)
    for name in runtime.executable_files:
        # Compose implements file-backed configs as bind mounts and may ignore mode.
        path = _inside(environment, name)
        if not path.is_file():
            raise ValueError(f"Declared executable file is missing: {name}")
        path.chmod(0o755)
    return destination
