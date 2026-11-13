"""Docker-managed Nix builds; the host only needs Python and Docker."""

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from mango import cache
from mango.environment.workspace import workspace_lock

IMAGE = "nixos/nix@sha256:c39fc2f168d84ad70cb07102dd82cc73dfb0915bd609308d1d269030bbfa7a26"
PLATFORM = "linux/amd64"
EXPORT_ACTIONS = {"load", "prepare", "compose", "run"}
SUPPORTED = EXPORT_ACTIONS | {"build", "fetch", "check", "validate", "declaration", "validate-contract", "lock"}


def ensure_builder(root):
    path = root / "mango.lock"
    document = json.loads(path.read_text())
    pin = document.get("builder")
    if pin is None:
        pin = {"image": IMAGE, "platform": PLATFORM}
        document["builder"] = pin
        temporary = path.with_name(f".mango.lock-{uuid.uuid4().hex}")
        try:
            temporary.write_text(json.dumps(document, indent=2) + "\n")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    if (
        not isinstance(pin, dict) or set(pin) != {"image", "platform"}
        or pin["platform"] != PLATFORM
        or not isinstance(pin["image"], str)
        or not re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}", pin["image"])
    ):
        raise ValueError("mango.lock: builder requires a digest-pinned image and linux/amd64 platform")
    return pin


def bootstrap(pin, command):
    source = {"type": "git", "allRefs": True, **pin}
    expression = "(builtins.fetchTree (builtins.fromJSON " + json.dumps(json.dumps(source)) + ")).outPath"
    return (
        "set -euo pipefail\n"
        "harness=$(nix eval --impure --raw --expr " + shlex.quote(expression) + ")\n"
        'exec nix-shell "$harness/shell.nix" --run ' + shlex.quote(shlex.join(command))
    )


def check_docker(*, require_images=True):
    if platform.system() != "Linux":
        raise ValueError("The managed builder currently supports Linux hosts; use --builder host elsewhere")
    result = subprocess.run(
        ["docker", "info", "--format", "{{json .}}"],
        check=True, capture_output=True, text=True,
    )
    info = json.loads(result.stdout)
    if info.get("OSType") != "linux" or info.get("Architecture") not in {"x86_64", "amd64"}:
        raise ValueError("The managed builder requires a linux/amd64 Docker engine")
    if any("rootless" in option for option in info.get("SecurityOptions", [])):
        raise ValueError("The managed builder requires rootful Docker for sandboxed Nix builds")
    if require_images and "io.containerd.snapshotter" not in json.dumps(info.get("DriverStatus", [])):
        raise ValueError("Docker must use its containerd image store to preserve OCI image digests")
    # Bind mounts and the authenticated loopback bridge must refer to this host.
    host = os.environ.get("DOCKER_HOST")
    if not host:
        host = subprocess.run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    if not host.startswith("unix://"):
        raise ValueError("The managed builder requires a local Docker engine")
    return Path(host.removeprefix("unix://"))


def docker_command(root, stage, pin, name, volume, script):
    command = [
        "docker", "run", "--rm", "--name", name, "--platform", pin["platform"],
        "--privileged", "--network", "host",
        "--mount", f"type=volume,source={volume},target=/nix",
        "--mount", f"type=bind,source={root},target={root},readonly",
        "--mount", f"type=bind,source={stage},target=/mango-output",
        "--mount", f"type=bind,source={Path(__file__).with_name('builder_worker.py')},target=/mango-worker.py,readonly",
        "--env", "NIX_REMOTE=local", "--env", "NIX_CONFIG",
        "--workdir", str(root),
        pin["image"], "bash", "-c", script,
    ]
    return command


def import_result(stage, workspace):
    records = json.loads((stage / "archives.json").read_text())
    # Validate all handoff files before changing the current runtime.
    for filename in ("images.json", "controller.json"):
        if not isinstance(json.loads((stage / filename).read_text()), dict):
            raise ValueError(f"Builder returned invalid {filename}")
    if not (stage / "runtime/task.toml").is_file():
        raise ValueError("Builder did not produce a Harbor task")
    for record in records:
        archive = stage / record["file"]
        if not archive.resolve().is_relative_to(stage.resolve()) or archive.is_symlink():
            raise ValueError("Builder returned an unsafe image archive path")
        with archive.open("rb") as stream:
            subprocess.run(["docker", "load", "--quiet"], stdin=stream, check=True)
        subprocess.run(
            ["docker", "image", "inspect", record["reference"]],
            check=True, stdout=subprocess.DEVNULL,
        )
    # Only publish runtime files after every image was loaded successfully.
    runtime = workspace / "runtime"
    previous = workspace / f".runtime-previous-{uuid.uuid4().hex}"
    if runtime.exists():
        runtime.rename(previous)
    try:
        (stage / "runtime").rename(runtime)
    except BaseException:
        if previous.exists():
            previous.rename(runtime)
        raise
    finally:
        if previous.exists() and runtime.exists():
            shutil.rmtree(previous)
    (stage / "images.json").replace(workspace / "images.json")
    (stage / "controller.json").replace(workspace / "controller.json")


def run_runtime(root, workspace, action, extra, pin, volume, socket):
    controller = json.loads((workspace / "controller.json").read_text())
    name = "mango-runtime-" + uuid.uuid4().hex
    command = [
        "docker", "run", "--rm", "--init", "--name", name, "--platform", pin["platform"],
        "--network", "host", "--cap-drop", "ALL",
        "--user", f"{os.getuid()}:{os.getgid()}", "--group-add", str(socket.stat().st_gid),
        "--mount", f"type=volume,source={volume},target=/nix,readonly",
        "--mount", f"type=bind,source={root},target={root},readonly",
        "--mount", f"type=bind,source={workspace},target={workspace}",
        "--mount", f"type=bind,source={socket},target=/var/run/docker.sock",
        "--mount", f"type=bind,source={Path(__file__).with_name('builder_runtime.py')},target=/mango-runtime.py,readonly",
        "--env", "HOME=/tmp", "--env", "DOCKER_HOST=unix:///var/run/docker.sock",
        "--env", "PATH=" + controller["path"],
        "--env", "PYTHONPATH=" + controller["harness"] + "/src",
        "--workdir", str(root),
    ]
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL"):
        if key in os.environ:
            command += ["--env", key]
    command += [pin["image"], controller["python"], "/mango-runtime.py", str(root), str(workspace), action, *extra]
    try:
        subprocess.run(command, check=True)
    finally:
        subprocess.run(["docker", "rm", "--force", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run(root, workspace, action, extra, harness_pin):
    if action not in SUPPORTED:
        raise ValueError(f"{action} is not supported by the managed builder; use --builder host")
    if action in EXPORT_ACTIONS - {"run"} and extra:
        raise ValueError(f"{action} takes no additional arguments with the managed builder")
    for path in (root, workspace):
        if "," in str(path) or path.is_relative_to("/nix"):
            raise ValueError("Managed builder paths cannot contain commas or be inside /nix")
    socket = check_docker(require_images=action in EXPORT_ACTIONS)
    with workspace_lock(workspace):
        pin = ensure_builder(root)
        if action == "lock":
            print(json.dumps({"lock": str(root / "mango.lock"), "builder": pin}))
            return
        # One store per builder version and local user, reusable across tasks.
        volume = "mango-nix-" + str(os.getuid()) + "-" + pin["image"].split(":")[-1][:16]
        identity = hashlib.sha256((str(root) + "\0" + str(workspace)).encode()).hexdigest()[:24]
        work = f"/nix/var/mango/workspaces/{identity}"
        name = "mango-builder-" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix=".builder-", dir=workspace) as temporary:
            stage = Path(temporary)
            worker = [
                "python", "/mango-worker.py", str(root), work, "/mango-output",
                str(os.getuid()), str(os.getgid()), action,
                *([] if action in EXPORT_ACTIONS else extra),
            ]
            script = bootstrap(harness_pin, worker)
            with cache.session(root, check_trust=False, inherit_config=False) as cache_env:
                env = dict(os.environ)
                env["NIX_CONFIG"] = (
                    (cache_env or {}).get("NIX_CONFIG", "")
                    + "\nexperimental-features = nix-command flakes\n"
                    + "sandbox = true\nsandbox-fallback = false\nrequire-sigs = true\n"
                    # Single-user store: do not share numeric nixbld UIDs with
                    # a host daemon whose build cleanup can kill those users.
                    + "build-users-group =\nmax-jobs = 2\ncores = 4\n"
                    + "accept-flake-config = false\n"
                )
                print("Building with Mango's managed Nix store (privileged Docker; KVM when usable, otherwise QEMU TCG)", file=sys.stderr)
                try:
                    subprocess.run(docker_command(root, stage, pin, name, volume, script), env=env, check=True)
                finally:
                    # Also clean up when the CLI is interrupted while Docker is running.
                    subprocess.run(["docker", "rm", "--force", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if action in EXPORT_ACTIONS:
                import_result(stage, workspace)
                print(f"Harbor task: {workspace / 'runtime'}", file=sys.stderr)
        if action in {"compose", "run"}:
            run_runtime(root, workspace, action, extra, pin, volume, socket)
