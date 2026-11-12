"""Export pinned Harbor startup topology as local, image-only Compose files.

Uses the pinned Harbor overlay implementation. No containers are started while
rendering, including Harbor's usual kernel-probe container.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path

from harbor.models.task.config import TaskConfig
from harbor.models.task.verifier_mode import resolve_effective_verifier_env_config
from harbor.models.trial.paths import TrialPaths

from mango.harbor.environment import PreloadedDockerEnvironment
from mango.runtime.materialize import verifier_variant


class ComposeRenderer(PreloadedDockerEnvironment):
    @classmethod
    def _egress_control_kernel_support(cls):
        # Rendering must not pull/run a probe image. Keep required routing in
        # the document; unsupported daemon kernels fail when starting it.
        return True


def verify_local_images(manifest):
    references = {manifest.egress_image, *manifest.services.values()}
    for services in manifest.verifiers.values():
        references.update(services.values())
    for reference in sorted(references):
        result = subprocess.run(
            ["docker", "image", "inspect", reference],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise ValueError(f"Preloaded image missing: {reference}; run load again")
        metadata = json.loads(result.stdout)[0]
        if f"{metadata['Os']}/{metadata['Architecture']}" != manifest.platform:
            raise ValueError(f"Preloaded image platform mismatch: {reference}")


def _literal_dollars(value):
    # `compose config` resolves interpolation. Escape dollars before the output
    # is read as a new Compose input, including command and environment values.
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, list):
        return [_literal_dollars(item) for item in value]
    if isinstance(value, dict):
        return {key: _literal_dollars(item) for key, item in value.items()}
    return value


def _render(context, config, manifest, label, work, phase_policies):
    project = (
        "mango-" + hashlib.sha256(str(work).encode()).hexdigest()[:12] + "-" + label
    )
    environment = ComposeRenderer(
        environment_dir=context,
        environment_name=label,
        session_id=project,
        trial_paths=TrialPaths(work / "compose-logs" / label),
        task_env_config=config,
        network_policy=config.resolve_baseline(),
        phase_network_policies=phase_policies,
    )
    environment._use_prebuilt = True
    environment._env_vars.egress_control_sidecar_image_name = manifest.egress_image
    environment._resources_compose_path = environment._write_resources_compose_file()
    environment._env_compose_path = environment._write_env_compose_file()
    environment._write_egress_control_services_compose_file()
    command = [
        "docker",
        "compose",
        "--project-name",
        project,
        "--project-directory",
        str(context),
    ]
    for path in environment._docker_compose_paths:
        command.extend(["-f", str(path)])
    result = subprocess.run(
        [*command, "config", "--format", "json"],
        env=environment._compose_env_vars(include_os_env=True),
        cwd=context,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError("Cannot render runtime Compose: " + result.stderr.strip())
    document = json.loads(result.stdout)
    expected = {manifest.egress_image, *manifest.services.values()}
    for service in document["services"].values():
        if "build" in service or service.get("image") not in expected:
            raise ValueError("Runtime Compose contains an unmapped image or build")
        service["pull_policy"] = "never"
    target = work / "compose" / f"{label}.json"
    command = ["docker", "compose", "--project-name", project, "-f", str(target)]
    return (
        target,
        _literal_dollars(document),
        {
            "path": str(target),
            "project": project,
            "up": [*command, "up", "--detach", "--no-build", "--pull", "never"],
            "down": [*command, "down"],
            "network_policy": config.resolve_baseline().model_dump(mode="json"),
        },
    )


def generate_compose(work: Path, manifest):
    verify_local_images(manifest)
    task = work / "runtime"
    config = TaskConfig.model_validate_toml((task / "task.toml").read_text())
    phase_policies = [
        policy
        for phase in [
            config.agent,
            config.verifier,
            *[s.agent for s in config.steps or []],
            *[s.verifier for s in config.steps or []],
        ]
        if (policy := phase.explicit_phase_policy()) is not None
    ]
    rendered = [
        _render(
            task / "environment",
            config.environment,
            manifest,
            "environment",
            work,
            phase_policies,
        )
    ]
    graders = []
    for index, step in enumerate(config.steps or [None]):
        verifier_config = resolve_effective_verifier_env_config(config, step)
        if verifier_config is None:
            continue
        context = task / "tests"
        if step and (task / "steps" / step.name / "tests").exists():
            context = task / "steps" / step.name / "tests"
        variant = verifier_variant({"docker_image": verifier_config.docker_image})
        key = f"{context.relative_to(task).as_posix()}/{variant}"
        selected = manifest.model_copy(update={"services": manifest.verifiers[key]})
        rendered.append(
            _render(
                context,
                verifier_config,
                selected,
                f"grader-{index}",
                work,
                phase_policies,
            )
        )
        graders.append(
            {"step": step.name if step else None, "context": key, **rendered[-1][2]}
        )
    # Publish only after every configuration has rendered successfully.
    (work / "compose").mkdir(exist_ok=True)
    for path, document, _ in rendered:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, indent=2) + "\n")
        os.replace(temporary, path)
    return {
        "runtime": rendered[0][2],
        "graders": graders,
        "note": "Compose starts runtime services at their baseline network policy. "
        "It does not run solutions, grading, artifact transfer, or phase policy changes; "
        "use Harbor oracle/nop via `mango ENV run` for those.",
    }
