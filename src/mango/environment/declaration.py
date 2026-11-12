"""Read and check an environment's ``mkEnvironment`` declaration.

The declaration is the single source of truth: image/service/verifier mappings
and every Compose document come from evaluating ``default.nix``'s
``declaration`` attribute, which forces no image or application derivation.
"""

import json
import subprocess
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from mango.runtime.manifest import RuntimeDeclaration
from mango.runtime.materialize import verifier_environments
from mango.runtime.task import relative_path

Name = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")]
# Compose service keys may include dots; image output names remain simple Nix
# attribute path components because they are addressed as images.<name>.
ServiceName = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")]
Attribute = Annotated[str, Field(min_length=1)]
DECLARATION_VERSION = 1


class EnvironmentManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    _compose_documents: dict[str, dict] = PrivateAttr(default_factory=dict)
    _declaration: dict = PrivateAttr(default_factory=dict)

    name: str
    platform: Literal["linux/amd64"] = "linux/amd64"
    task: str = "task"
    images: dict[Name, Attribute] = Field(min_length=1)
    builds: dict[Name, Attribute] = Field(default_factory=dict)
    services: dict[ServiceName, Name] = Field(min_length=1)
    #: Harbor verifier key (context/variant) to service-to-image mapping.
    verifiers: dict[str, dict[ServiceName, Name]] = Field(default_factory=dict)
    environment_files: dict[str, str] = Field(default_factory=dict)
    executable_files: list[str] = Field(default_factory=list)

    @field_validator("task")
    @classmethod
    def validate_task(cls, value):
        return relative_path(value)

    @field_validator("environment_files")
    @classmethod
    def validate_environment_files(cls, value):
        for target, source in value.items():
            relative_path(target)
            relative_path(source)
        return value

    @field_validator("executable_files")
    @classmethod
    def validate_executable_files(cls, value):
        return [relative_path(path) for path in value]

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        overlap = self.images.keys() & self.builds.keys()
        if overlap:
            raise ValueError(f"Image and build output names overlap: {sorted(overlap)}")
        if "egress" not in self.images:
            raise ValueError("Images must include the egress image")
        if "main" not in self.services:
            raise ValueError("Services must include main")
        referenced = set(self.services.values())
        for key, services in self.verifiers.items():
            if not services:
                raise ValueError(f"Verifier {key} requires at least one service")
            if "main" not in services:
                raise ValueError(f"Verifier {key} must include main")
            referenced.update(services.values())
        missing = referenced - self.images.keys()
        if missing:
            raise ValueError(f"Unknown image references: {sorted(missing)}")
        return self

    @property
    def attributes(self):
        return self.images | self.builds

    def runtime(self) -> RuntimeDeclaration:
        """The Nix-free description used to generate the Harbor task."""
        return RuntimeDeclaration(
            task=self.task,
            compose=self._compose_documents,
            environment_files=self.environment_files,
            executable_files=self.executable_files,
        )

    def validate_task_mappings(self, root: Path) -> dict[str, dict[str, str]]:
        """Check the task's verifier contexts against the declaration."""
        contexts = verifier_environments(root / self.task)
        if contexts.keys() != self.verifiers.keys():
            raise ValueError(
                "Declared verifiers must cover task contexts exactly; "
                f"expected {sorted(contexts)}, got {sorted(self.verifiers)}"
            )
        return self.verifiers


def evaluate_declaration(root: Path, harness: Path) -> dict:
    result = subprocess.run(
        [
            "nix-instantiate",
            str(root / "default.nix"),
            "--eval",
            "--strict",
            "--json",
            "--arg",
            "harness",
            "builtins.toPath " + json.dumps(str(harness)),
            "-A",
            "declaration",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError("Cannot evaluate declaration: " + result.stderr.strip())
    declaration = json.loads(result.stdout)
    if declaration.get("version") != DECLARATION_VERSION:
        raise ValueError("Unsupported environment declaration version")
    return declaration


def _local_source(root: Path, value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        return relative_path(value)
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise ValueError(
            f"Declared task and asset paths must be local to the environment: {value}"
        ) from error


def load_manifest(root: Path, harness: Path) -> EnvironmentManifest:
    root = root.resolve()
    if not (root / "default.nix").is_file():
        raise ValueError(f"No default.nix at {root}")
    declaration = evaluate_declaration(root, harness)
    document = dict(declaration["manifest"])
    document["name"] = declaration["name"]
    document["task"] = _local_source(root, document["task"])
    document["environment_files"] = {
        name: _local_source(root, source)
        for name, source in document.get("environment_files", {}).items()
    }
    task = root / document["task"]
    if not task.resolve().is_relative_to(root):
        raise ValueError("Task path escapes environment through a symlink")
    contexts = verifier_environments(task)
    declared = document.get("verifiers", {})
    expected = {
        context.relative_to(task).as_posix() for context, _ in contexts.values()
    }
    if set(declared) != expected or set(declaration["verifierCompose"]) != expected:
        raise ValueError(
            f"Declared verifiers must cover task contexts exactly: {sorted(expected)}"
        )
    document["verifiers"] = {
        key: declared[context.relative_to(task).as_posix()]
        for key, (context, _) in contexts.items()
    }
    manifest = EnvironmentManifest.model_validate(document)
    manifest._declaration = declaration
    manifest._compose_documents = {
        "environment": declaration["compose"],
        **declaration["verifierCompose"],
    }
    for context in manifest._compose_documents:
        relative_path(context)
    for source in manifest.environment_files.values():
        if not (root / source).resolve().is_relative_to(root):
            raise ValueError(
                "Environment file source escapes its root through a symlink"
            )
    return manifest
