"""Image-only runtime materialization and the Harbor prebuilt-image adapter.

Only a verified, extracted copy is modified. Compose runtime settings remain
intact; build settings never reach the execution daemon.
"""

import copy
import hashlib
import json
import re
import tomllib
from pathlib import Path

import yaml

from mango.runtime.manifest import BUILD_RECIPE_VERSION, BuildManifest

SIDECAR_SERVICE = "harbor-docker-egress-control-sidecar"
# File names shared with the hosted prebuilt-image adapter; keep them stable.
MANIFEST_NAME = ".mango-images.json"
EGRESS_BUILD_SERVICE = "mango-egress"


def verifier_variant(config: dict) -> str:
    return hashlib.sha256(json.dumps(config.get("docker_image")).encode()).hexdigest()[
        :16
    ]


def verifier_environments(task: Path) -> dict[str, tuple[Path, dict]]:
    """Mirror Harbor's verifier mode/config inheritance and step context fallback.

    Kept dependency-free so packaging and CLI materialization need no Harbor import.
    Only docker_image affects which build definition is selected within a context.
    """
    config = tomllib.loads((task / "task.toml").read_text())
    root = config.get("verifier", {})

    def mode(verifier):
        return verifier.get("environment_mode") or (
            "separate" if verifier.get("environment") is not None else None
        )

    result = {}
    for step in config.get("steps") or [None]:
        verifier = step.get("verifier", {}) if step else {}
        if (mode(verifier) or mode(root) or "shared") != "separate":
            continue
        env = verifier.get(
            "environment", root.get("environment", config.get("environment", {}))
        )
        context = task / "tests"
        if step:
            candidate = task / "steps" / step["name"] / "tests"
            if not candidate.resolve().is_relative_to(task.resolve()):
                raise ValueError("Verifier context escapes task")
            if candidate.exists():
                context = candidate
        if not context.resolve().is_relative_to(task.resolve()):
            raise ValueError("Verifier context escapes task")
        key = f"{context.relative_to(task).as_posix()}/{verifier_variant(env)}"
        result[key] = (context, env)
    return result


def verifier_target(key: str, service: str) -> str:
    return (
        "mango-verifier-"
        + hashlib.sha256(key.encode()).hexdigest()[:16]
        + "-"
        + service
    )


def _managed_sidecar(name: str, service: dict) -> bool:
    # Harbor supplies the stock image; factory exports only customize the
    # network/policy mounts. Explicit image/build overrides remain task-owned.
    return name == SIDECAR_SERVICE and not (
        service.get("image") or service.get("build")
    )


def compose_path(task: Path, *, environment: Path | None = None) -> Path:
    environment = environment if environment is not None else task / "environment"
    for name in ("docker-compose.yaml", "docker-compose.yml"):
        path = environment / name
        if path.exists():
            return path
    return environment / "docker-compose.yaml"


def source_compose(
    task: Path, *, environment: Path | None = None, config: dict | None = None
) -> dict:
    path = compose_path(task, environment=environment)
    document = yaml.safe_load(path.read_text()) if path.exists() else {}
    if not isinstance(document, dict):
        raise ValueError("Environment Compose must be a mapping")
    if document.get("include"):
        raise ValueError("Compose include is not supported for prebuilt images")
    document = copy.deepcopy(document)
    services = document.setdefault("services", {})
    if config is None:
        config = tomllib.loads((task / "task.toml").read_text()).get("environment", {})
    if config.get("os", "linux") != "linux":
        raise ValueError("Prebuilt images currently support Linux environments only")
    if "main" not in services:
        services["main"] = {}
    if not (services["main"].get("image") or services["main"].get("build")):
        if config.get("docker_image"):
            services["main"]["image"] = config["docker_image"]
        else:
            services["main"]["build"] = {"context": "."}
    for name, service in services.items():
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
            raise ValueError(f"Invalid service name: {name}")
        if not isinstance(service, dict) or service.get("extends"):
            raise ValueError(f"Service {name}: Compose extends is unsupported")
        if service.get("profiles"):
            raise ValueError(
                f"Service {name}: profiles must be resolved before packaging"
            )
        if (
            not service.get("build")
            and not service.get("image")
            and not _managed_sidecar(name, service)
        ):
            raise ValueError(f"Service {name} has no image or build")
    return document


def materialize_runtime(task: Path, manifest: BuildManifest, digest: str) -> None:
    if manifest.source_digest != digest:
        raise ValueError(
            "Image manifest does not match the verified snapshot or image mappings; run load again"
        )
    if manifest.recipe_version != BUILD_RECIPE_VERSION:
        raise ValueError("Unsupported image manifest recipe; rebuild required")
    verifiers = verifier_environments(task)
    if set(verifiers) != set(manifest.verifiers):
        raise ValueError(
            "Image manifest does not cover exactly the verifier environments; "
            "rebuild required"
        )
    _materialize_environment(task, task / "environment", None, manifest)
    for key, (context, config) in verifiers.items():
        selected = manifest.model_copy(update={"services": manifest.verifiers[key]})
        _materialize_environment(
            task, context, config, selected, verifier_variant(config)
        )


def required_service_names(task: Path, *, environment=None, config=None) -> set[str]:
    """Services requiring explicit images, excluding Harbor's stock sidecar."""
    document = source_compose(task, environment=environment, config=config)
    return {
        name
        for name, service in document["services"].items()
        if not _managed_sidecar(name, service)
    }


def _materialize_environment(task, environment, config, manifest, variant=None):
    document = source_compose(task, environment=environment, config=config)
    managed = {
        name
        for name, service in document["services"].items()
        if _managed_sidecar(name, service)
    }
    if set(document["services"]) - managed != set(manifest.services):
        raise ValueError(
            "Image manifest does not cover exactly the environment services"
        )
    for name, service in document["services"].items():
        service.pop("build", None)
        service["image"] = (
            manifest.egress_image if name in managed else manifest.services[name]
        )
        service["platform"] = manifest.platform
        service["pull_policy"] = "always"
    path = compose_path(task, environment=environment)
    environment.mkdir(parents=True, exist_ok=True)
    target = environment / (
        f".mango-compose-{variant}.yaml" if variant else "docker-compose.yaml"
    )
    target.write_text(yaml.safe_dump(document, sort_keys=False))
    if variant and not path.exists():
        # Harbor validates a conventional definition before consulting the
        # variant-specific adapter path. Clean graders have no Dockerfile.
        # This validation fallback does not select the executed image variant.
        path.write_text(yaml.safe_dump(document, sort_keys=False))
    if not variant and path != target and path.exists():
        path.unlink()
    name = f".mango-images-{variant}.json" if variant else MANIFEST_NAME
    (environment / name).write_text(manifest.model_dump_json())


class ImageOnlyDockerMixin:
    """Enforce prebuilt images even when Harbor asks to rebuild.

    Placed before the Harbor environment class in the MRO. The manifest file
    is written by the trusted executor *after* snapshot verification.
    """

    def _image_variant(self):
        if not hasattr(self, "_mango_image_variant"):
            self._mango_image_variant = (
                None
                if self.environment_dir.name == "environment"
                else verifier_variant(
                    {"docker_image": self.task_env_config.docker_image}
                )
            )
        return self._mango_image_variant

    @property
    def _environment_docker_compose_path(self):
        variant = self._image_variant()
        return self.environment_dir / (
            f".mango-compose-{variant}.yaml" if variant else "docker-compose.yaml"
        )

    def _image_manifest(self) -> BuildManifest:
        variant = self._image_variant()
        name = f".mango-images-{variant}.json" if variant else MANIFEST_NAME
        return BuildManifest.model_validate_json(
            (self.environment_dir / name).read_text()
        )

    @property
    def _docker_compose_paths(self):
        paths = super()._docker_compose_paths
        # Harbor's default main service adds build: even for a custom Compose
        # task. Replace that base with its prebuilt equivalent.
        return [
            self._DOCKER_COMPOSE_PREBUILT_PATH
            if p == self._DOCKER_COMPOSE_BUILD_PATH
            else p
            for p in paths
        ]

    def _compose_infra_env_vars(self):
        values = super()._compose_infra_env_vars()
        values["PREBUILT_IMAGE_NAME"] = self._image_manifest().services["main"]
        return values

    async def start(self, force_build: bool):
        manifest = self._image_manifest()
        # Harbor may reuse the task/step config for another verification pass.
        self.task_env_config = self.task_env_config.model_copy(
            update={"docker_image": manifest.services["main"]}
        )
        self._env_vars.prebuilt_image_name = manifest.services["main"]
        await super().start(force_build=False)

    async def _ensure_egress_control_sidecar_image_built(self):
        self._env_vars.egress_control_sidecar_image_name = (
            self._image_manifest().egress_image
        )

    async def _run_docker_compose_command(self, command, **kwargs):
        if command and command[0] == "build":
            command = ["pull"]
        elif command and command[0] == "up":
            command = ["up", "--no-build", *command[1:]]
        # Reject any remaining build stanza, including injected runtime overlays.
        for path in self._docker_compose_paths:
            document = yaml.safe_load(path.read_text()) or {}
            if any(
                "build" in service for service in document.get("services", {}).values()
            ):
                raise ValueError("Runtime Compose contains build instructions")
        return await super()._run_docker_compose_command(command, **kwargs)
