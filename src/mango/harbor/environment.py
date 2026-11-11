"""Local Harbor integration for prebuilt image manifests.

These classes override private Harbor methods; harbor.nix pins the release
they were written against. Re-check them whenever that pin changes.
"""

import os
import tempfile
from pathlib import Path

import yaml
from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.trial.config import ResourceMode

from mango.runtime.materialize import ImageOnlyDockerMixin

#: Memory ceiling for each service of a locally run environment. Hosted workers
#: cap a whole trial at about 12.8 GiB, so no service can use more than this
#: there. Locally nothing is capped: one runaway service drives the machine into
#: a global OOM and the kernel kills whatever scores highest, often an unrelated
#: environment or the desktop.
DEFAULT_SERVICE_MEMORY = "12g"

#: Overrides the ceiling with a compose size ("8g", "4096m"); "none" disables it.
SERVICE_MEMORY_ENV = "INCALMO_LOCAL_SERVICE_MEMORY"

MEMORY_OVERLAY_NAME = "docker-compose-memory.yaml"


def service_memory() -> str | None:
    """The configured per-service ceiling, or ``None`` when disabled."""
    value = os.environ.get(SERVICE_MEMORY_ENV, DEFAULT_SERVICE_MEMORY).strip()
    return None if value.lower() in ("", "0", "none") else value


def _declares_memory_limit(service: object) -> bool:
    if not isinstance(service, dict):
        return False
    if "mem_limit" in service:
        return True
    limits = ((service.get("deploy") or {}).get("resources") or {}).get("limits") or {}
    return isinstance(limits, dict) and "memory" in limits


def uncapped_services(compose_path: Path, *, include_main: bool) -> list[str]:
    """Services the task defines without a memory limit of its own.

    A limit the task declares wins: compose rejects ``mem_limit`` beside a
    different ``deploy.resources.limits.memory``, so those services are left
    alone. A missing or unreadable file yields only ``main``; Harbor reports a
    parse error itself when it loads the same file.
    """
    names = {"main"} if include_main else set()
    try:
        document = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return sorted(names)
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict):
        return sorted(names)
    for name, service in services.items():
        if not isinstance(name, str):
            continue
        if _declares_memory_limit(service):
            names.discard(name)
        else:
            names.add(name)
    return sorted(names)


class LocalMemoryCapMixin:
    """Give every uncapped service of a local environment a memory ceiling.

    Placed before the Harbor environment class in the MRO.
    """

    def _memory_overlay(self) -> Path | None:
        if not hasattr(self, "_incalmo_memory_overlay"):
            self._incalmo_memory_overlay = self._write_memory_overlay()
        return self._incalmo_memory_overlay

    def _write_memory_overlay(self) -> Path | None:
        limit = service_memory()
        if limit is None:
            return None
        # A memory_mb in task.toml already caps main through Harbor's own overlay.
        harbor_caps_main = (
            self._resource_limit_value("memory", auto_mode=ResourceMode.LIMIT)
            is not None
        )
        services = uncapped_services(
            self._environment_docker_compose_path, include_main=not harbor_caps_main
        )
        if not services:
            return None
        self._incalmo_memory_dir = tempfile.TemporaryDirectory(prefix="incalmo-memory-")
        path = Path(self._incalmo_memory_dir.name) / MEMORY_OVERLAY_NAME
        overlay = {"services": {name: {"mem_limit": limit} for name in services}}
        path.write_text(yaml.safe_dump(overlay, sort_keys=True), encoding="utf-8")
        return path

    @property
    def _docker_compose_paths(self):
        paths = super()._docker_compose_paths
        overlay = self._memory_overlay()
        # It only sets mem_limit, and only where nothing else does, so its
        # position cannot change any value another file supplies.
        return [*paths, overlay] if overlay else paths


class PrebuiltDockerEnvironment(
    LocalMemoryCapMixin, ImageOnlyDockerMixin, DockerEnvironment
):
    """Run a frozen task locally without invoking an environment build."""


class PreloadedDockerEnvironment(PrebuiltDockerEnvironment):
    """Explicit local-only disconnected validation of digest-addressed images.

    Select with Harbor's custom environment class option. No environment variable
    can switch production workers into this mode. Missing images fail closed;
    network policy and egress-sidecar configuration remain Harbor-owned.
    """

    async def start(self, force_build: bool):
        import asyncio
        import json

        manifest = self._image_manifest()
        images = {*manifest.services.values(), manifest.egress_image}
        for reference in sorted(images):
            process = await asyncio.create_subprocess_exec(
                "docker",
                "image",
                "inspect",
                reference,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            if process.returncode:
                raise ValueError(f"Preloaded image missing: {reference}")
            metadata = json.loads(stdout)[0]
            platform = f"{metadata['Os']}/{metadata['Architecture']}"
            if platform != manifest.platform:
                raise ValueError(f"Preloaded image platform mismatch: {reference}")
        await super().start(force_build=False)

    async def _run_docker_compose_command(self, command, **kwargs):
        # Still call ImageOnlyDockerMixin to validate every runtime overlay.
        # `config --quiet` performs no network access and returns Harbor's usual
        # command result type for a build/pull that has already been satisfied.
        if command and command[0] in ("build", "pull"):
            command = ["config", "--quiet"]
        elif command and command[0] == "up":
            command = [*command, "--pull", "never"]
        return await super()._run_docker_compose_command(command, **kwargs)
