"""Receipt-bound, offline post-APT toolchain preparation for PHP images. REMOTE AUTHORING ONLY.

Run as ``python -m mango.authoring.php_prefix prepare|extract-composer ...`` (``mango-prepare-php``
once wired). AUTHORING ADAPTER, NOT A BUILD OUTPUT. The image committed here is an authoring
toolchain cache that ``mango.authoring.composer_lock`` resolves Composer against. The translated final image still runs the ORIGINAL
Dockerfile commands from pinned inputs; nothing from here is shipped.

Chain bound by one request and one receipt (every link is RECOMPUTED, never taken from the caller):

  original immutable base  ->  Mango apt state (canonical LockRequest hash, recomputed from the
  embedded ``package_request`` and cross-checked against the Dockerfile's ``apt-get install``)  ->
  the verbatim post-APT commands (docker-php-ext-configure/install, a2enmod, rm) of the original
  RUN, run as one ``/bin/sh -c`` string inside a ``--network none`` container with explicit limits
  (1.5 CPU, 4 GiB, 40 GiB disk reserve)  ->  ``docker commit``  ->  actual image ID, ``php -v``-level
  version and ``php -m`` modules observed in a fresh ``--network none`` container whose runtime
  ``.Image`` equals the committed ID.

The apt step itself is Mango's ``prepare_image`` (network allowed there, as designed); only the
post-APT compilation is network-less. Docker access goes through a ``Runner`` so tests can mock it;
a receipt is only ever marked ``proof: "docker"`` by the real runner and ``verify_receipt`` rejects
anything else.

``extract_composer_phar`` is the separate, safe PHAR extraction helper for the digest-pinned composer
image (no vendor tree, no credentials).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Callable

from mango.authoring.composer_lock import ENV_ALLOWED
from mango.authoring.remote_common import (
    IMAGE_ID_RE,
    SHA256_RE,
    ResolverError,
    redact,
    require_remote_author,
    sha256_hex,
)

SCHEMA = 1
KIND = "mango-toolchain-prefix-receipt"
LABEL = "org.mango.toolchain-prefix"
GIB = 1024**3
LIMITS = {"cpus": "1.5", "memory_bytes": 4 * GIB, "memory_swap_bytes": 4 * GIB, "pids": 1024,
          "disk_reserve_bytes": 40 * GIB, "network": "none"}
INPUT_DIR = "/mango-inputs"
MAX_INPUT_BYTES = 256 * 1024 * 1024
BASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*@sha256:[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
NAME_RE = re.compile(r"^[a-z0-9_]+$")
APT_FLAGS = {"-y", "--yes", "--no-install-recommends", "-q", "-qq"}
RM_PATHS = {"/var/lib/apt/lists/*", "/var/cache/apt/*", "/var/cache/apt/archives/*", "/tmp/*"}
POST_APT_COMMANDS = {"docker-php-ext-configure", "docker-php-ext-install", "a2enmod", "rm"}
REQUEST_KEYS = {"id", "base", "platform", "php_version", "required_php_extensions", "apt", "recipe",
                "post_apt", "env", "inputs", "limits"}


class PrefixError(ResolverError):
    def __init__(self, message: str, code: str = "toolchain-prefix") -> None:
        super().__init__(message, code)


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def recipe_sha256(base: str, runs: list[str], env: dict[str, str]) -> str:
    """Same formula composer_family uses for ``toolchain.recipe_sha256`` (base is the pinned ref)."""
    return canonical_sha256({"base": base, "runs": runs, "env": env})


# --------------------------------------------------------------------------
# Recipe parsing: verbatim post-APT commands, fail closed
# --------------------------------------------------------------------------
def split_chain(script: str) -> list[str]:
    """Verbatim text of each ``&&`` segment. Anything else shell-like is refused, not guessed."""
    if "`" in script or "$" in script or "\n" in script or "\r" in script:
        raise PrefixError("RUN contains expansion/newline syntax that is not modelled", "recipe-unsupported")
    segments, current, quote, i = [], [], None, 0
    while i < len(script):
        ch = script[i]
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            current.append(ch)
        elif script.startswith("&&", i):
            segments.append("".join(current).strip())
            current, i = [], i + 2
            continue
        elif ch in ";|&<>(){}\\":
            raise PrefixError(f"unsupported shell operator {ch!r} in RUN", "recipe-unsupported")
        else:
            current.append(ch)
        i += 1
    if quote:
        raise PrefixError("unbalanced quote in RUN", "recipe-unsupported")
    segments.append("".join(current).strip())
    if any(not s for s in segments):
        raise PrefixError("empty command in RUN chain", "recipe-unsupported")
    return segments


def check_post_apt(argv: list[str]) -> None:
    name, args = argv[0], argv[1:]
    if name == "rm":
        flags = [a for a in args if a.startswith("-")]
        paths = [a for a in args if not a.startswith("-")]
        if not paths or set(flags) - {"-r", "-f", "-rf", "-fr"} or set(paths) - RM_PATHS:
            raise PrefixError(f"rm outside the allowed apt/tmp cleanup: {' '.join(argv)}", "recipe-unsupported")
    elif name == "docker-php-ext-configure":
        if len(args) < 1 or not NAME_RE.fullmatch(args[0]) or not all(re.fullmatch(r"--(with|without|enable|disable)-[a-z0-9-]+", a) for a in args[1:]):
            raise PrefixError(f"unmodelled docker-php-ext-configure: {' '.join(argv)}", "recipe-unsupported")
    elif name == "docker-php-ext-install":
        names = [a for a in args if not a.startswith("-")]
        if not names or not all(NAME_RE.fullmatch(n) for n in names) or not all(re.fullmatch(r"-j\d+", a) for a in args if a.startswith("-")):
            raise PrefixError(f"unmodelled docker-php-ext-install: {' '.join(argv)}", "recipe-unsupported")
    elif name == "a2enmod":
        if not args or not all(NAME_RE.fullmatch(a) for a in args):
            raise PrefixError(f"unmodelled a2enmod: {' '.join(argv)}", "recipe-unsupported")


def parse_recipe(runs: list[str]) -> dict:
    """Split original RUN strings into the apt part and the verbatim post-APT script."""
    if not isinstance(runs, list) or not runs or not all(isinstance(r, str) for r in runs):
        raise PrefixError("recipe.runs must be the original RUN strings", "recipe-unsupported")
    packages: list[str] = []
    seen_install = no_recommends = False
    apt_texts, post = [], []
    for run in runs:
        for text in split_chain(run):
            try:
                argv = shlex.split(text)
            except ValueError as exc:
                raise PrefixError(f"unparsable command {text!r}: {exc}", "recipe-unsupported") from None
            if argv[0] == "apt-get":
                if post:
                    raise PrefixError("apt-get after a post-APT command is not modelled", "recipe-unsupported")
                if argv[1:] == ["update"]:
                    apt_texts.append(text)
                elif argv[1:2] == ["install"] and set(a for a in argv[2:] if a.startswith("-")) <= APT_FLAGS:
                    packages += [a for a in argv[2:] if not a.startswith("-")]
                    no_recommends = no_recommends or "--no-install-recommends" in argv
                    seen_install = True
                    apt_texts.append(text)
                else:
                    raise PrefixError(f"unmodelled apt-get command: {text}", "recipe-unsupported")
            elif argv[0] in POST_APT_COMMANDS:
                if not seen_install:
                    raise PrefixError("post-APT commands before the apt install are not modelled", "recipe-unsupported")
                check_post_apt(argv)
                post.append(text)
            else:
                raise PrefixError(f"command {argv[0]!r} is not an apt/post-APT toolchain step", "recipe-unsupported")
    if not seen_install or not packages or not post:
        raise PrefixError("recipe needs an apt-get install and at least one post-APT command", "recipe-unsupported")
    return {"apt_commands": apt_texts, "apt_packages": packages, "no_install_recommends": no_recommends,
            "post_apt": post, "script": " && ".join(post)}


# --------------------------------------------------------------------------
# apt chain: recompute the canonical LockRequest hash with Mango itself
# --------------------------------------------------------------------------
def mango_lock_hash(package_request: dict) -> str:
    """Canonical LockRequest hash used by the other prepared-toolchain adapters."""
    try:
        from mango.authoring.packages import LockRequest
    except ImportError as exc:
        raise PrefixError(f"Mango is not importable, cannot recompute the apt request hash: {exc}", "mango-missing") from None
    try:
        request = LockRequest.model_validate(package_request)
    except Exception as exc:  # pydantic.ValidationError
        raise PrefixError(f"apt package_request is not a valid Mango LockRequest: {str(exc)[:300]}", "invalid-apt") from None
    return sha256_hex(json.dumps(request.cache_document(), sort_keys=True).encode())


def normalise_repo(base: str) -> str:
    name, _, digest = base.partition("@")
    for prefix in ("docker.io/library/", "docker.io/", "index.docker.io/library/", "mirror.gcr.io/library/"):
        name = name.removeprefix(prefix)
    return f"{name}@{digest}"


def check_apt(apt: object, base: str, parsed: dict, lock_hash: Callable[[dict], str]) -> str:
    """Independently verify the apt evidence against the base and the Dockerfile; return its recomputed hash."""
    if not isinstance(apt, dict) or not isinstance(apt.get("package_request"), dict):
        raise PrefixError("apt.package_request (the Mango LockRequest document) is required", "invalid-apt")
    pr = apt["package_request"]
    if pr.get("manager") != "apt" or normalise_repo(str(pr.get("base"))) != normalise_repo(base):
        raise PrefixError("apt package_request is not for the original immutable base", "apt-base-mismatch")
    stages = pr.get("stages")
    if not isinstance(stages, list) or not stages:
        raise PrefixError("apt package_request has no stages", "invalid-apt")
    if any(s.get("operation", "apt_install") != "apt_install" for s in stages):
        raise PrefixError("only apt_install stages are modelled", "invalid-apt")
    listed = [pkg for s in stages for pkg in s.get("packages", [])]
    if sorted(listed) != sorted(parsed["apt_packages"]):
        raise PrefixError("apt stage packages differ from the Dockerfile's apt-get install", "apt-recipe-mismatch")
    recommends = [s.get("install_recommends", False) for s in stages]
    if parsed["no_install_recommends"] != all(r is False for r in recommends) or (
        not parsed["no_install_recommends"] and any(r is False for r in recommends)
    ):
        raise PrefixError("install_recommends differs from the Dockerfile's apt-get flags", "apt-recipe-mismatch")
    computed = lock_hash(pr)
    if not isinstance(computed, str) or not SHA256_RE.fullmatch(computed):
        raise PrefixError("recomputed apt request hash is malformed", "invalid-apt")
    claimed = apt.get("request_sha256")
    if claimed != computed:
        raise PrefixError("apt.request_sha256 does not match the recomputed canonical LockRequest hash", "apt-hash-mismatch")
    return computed


# --------------------------------------------------------------------------
# Request
# --------------------------------------------------------------------------
def check_env(env: object) -> dict[str, str]:
    if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
        raise PrefixError("env must be an object of strings", "invalid-env")
    extra = sorted(set(env) - ENV_ALLOWED)
    if extra:
        raise PrefixError(f"ENV {extra} is not modelled", "invalid-env")
    return dict(env)


def validate_request(request: object, lock_hash: Callable[[dict], str] = mango_lock_hash) -> dict:
    """Validate and RE-DERIVE every bound field from the verbatim recipe; mismatches fail closed."""
    if not isinstance(request, dict):
        raise PrefixError("request must be an object", "invalid-request")
    unknown = set(request) - REQUEST_KEYS
    if unknown:
        raise PrefixError(f"unknown request keys: {sorted(unknown)}", "invalid-request")
    out = dict(request)
    if not isinstance(out.get("id"), str) or not ID_RE.match(out["id"]):
        raise PrefixError("invalid request id", "invalid-request")
    if not isinstance(out.get("base"), str) or not BASE_RE.match(out["base"]):
        raise PrefixError("base must be NAME@sha256:<64 hex> (the original immutable base)", "invalid-base")
    if out.get("platform", "linux/amd64") != "linux/amd64":
        raise PrefixError("only linux/amd64 is supported", "invalid-request")
    out["platform"] = "linux/amd64"
    if not isinstance(out.get("php_version"), str) or not re.fullmatch(r"\d+\.\d+\.\d+", out["php_version"]):
        raise PrefixError("php_version must be the exact original X.Y.Z", "invalid-request")
    ext = out.get("required_php_extensions")
    if not isinstance(ext, list) or not ext or not all(isinstance(e, str) and NAME_RE.fullmatch(e) for e in ext):
        raise PrefixError("required_php_extensions must list the extensions the recipe installs", "invalid-request")
    recipe = out.get("recipe")
    if not isinstance(recipe, dict) or set(recipe) != {"runs", "env"}:
        raise PrefixError("recipe must be {runs: original RUN strings, env: original ENV}", "invalid-request")
    parsed = parse_recipe(recipe["runs"])
    env = check_env(recipe["env"])
    if check_env(out.get("env", env)) != env:
        raise PrefixError("env differs from recipe.env", "invalid-env")
    out["env"] = env
    post = out.get("post_apt")
    if post is not None and post != {"commands": parsed["post_apt"], "script": parsed["script"]}:
        raise PrefixError("post_apt differs from the commands re-derived from recipe.runs", "recipe-mismatch")
    out["post_apt"] = {"commands": parsed["post_apt"], "script": parsed["script"]}
    installed = sorted({a for c in parsed["post_apt"] for a in shlex.split(c)[1:] if shlex.split(c)[0] == "docker-php-ext-install" and not a.startswith("-")})
    if sorted(set(ext)) != installed or len(set(ext)) != len(ext):
        raise PrefixError("required_php_extensions must equal the docker-php-ext-install set", "invalid-request")
    out["apt_request_sha256"] = check_apt(out.get("apt"), out["base"], parsed, lock_hash)
    inputs = out.get("inputs", [])
    if not isinstance(inputs, list):
        raise PrefixError("inputs must be a list", "invalid-input")
    seen = set()
    for item in inputs:
        if (not isinstance(item, dict) or set(item) != {"name", "path", "sha256"}
                or not isinstance(item["name"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", item["name"])
                or item["name"] in seen or not isinstance(item["path"], str) or not os.path.isabs(item["path"])
                or not isinstance(item["sha256"], str) or not SHA256_RE.fullmatch(item["sha256"])):
            raise PrefixError("each input must be {name, absolute path, sha256}", "invalid-input")
        seen.add(item["name"])
    out["inputs"] = inputs
    if out.get("limits", LIMITS) != LIMITS:
        raise PrefixError(f"limits must be exactly {LIMITS}", "invalid-limits")
    out["limits"] = dict(LIMITS)
    return out


def chain_hashes(request: dict, apt_request_sha256: str) -> dict:
    recipe = request["recipe"]
    return {
        "base": request["base"],
        "apt_request_sha256": apt_request_sha256,
        "recipe_sha256": recipe_sha256(request["base"], recipe["runs"], recipe["env"]),
        "script_sha256": sha256_hex(request["post_apt"]["script"].encode()),
        "env_sha256": canonical_sha256(request["env"]),
        "inputs": {i["name"]: i["sha256"] for i in request["inputs"]},
    }


# --------------------------------------------------------------------------
# Runner (real Docker; tests inject a mock)
# --------------------------------------------------------------------------
def _docker(*args: str, check: bool = True, data: bytes | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(["docker", *args], capture_output=True, input=data, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise PrefixError(f"docker {args[0]} timed out", "docker") from None
    if check and result.returncode:
        tail = redact(result.stderr.decode("utf-8", "replace")[-1500:])
        raise PrefixError(f"docker {args[0]} failed ({result.returncode}): {tail}", "docker")
    return result


class DockerRunner:
    PROOF = "docker"

    def free_bytes(self) -> int:
        root = _docker("info", "--format", "{{.DockerRootDir}}").stdout.decode().strip()
        # A Docker-socket author container cannot see the host DockerRootDir.
        # Its operator can point this at a bind mount on that same backing disk.
        observed = os.environ.get("MANGO_REMOTE_DISK_PATH", root)
        if not Path(observed).is_absolute() or not Path(observed).is_dir():
            raise PrefixError("Docker backing disk is not visible; set MANGO_REMOTE_DISK_PATH to its host bind mount", "disk-unobservable")
        return shutil.disk_usage(observed).free

    def image_inspect(self, ref: str) -> dict:
        return json.loads(_docker("image", "inspect", ref).stdout)[0]

    def create(self, image: str, entrypoint: str, cmd: list[str], network: str, limits: bool) -> str:
        name = "mango-toolchain-prefix-" + uuid.uuid4().hex[:12]
        flags = ["--platform", "linux/amd64", "--network", network, "--user", "0:0"]
        if limits:
            flags += ["--cpus", LIMITS["cpus"], "--memory", str(LIMITS["memory_bytes"]),
                      "--memory-swap", str(LIMITS["memory_swap_bytes"]), "--pids-limit", str(LIMITS["pids"])]
        _docker("create", "--name", name, "--label", "mango.toolchain-prefix=1", *flags, "--entrypoint", entrypoint, image, *cmd)
        return name

    def copy_in(self, name: str, tar: bytes, dest: str) -> None:
        _docker("cp", "-", f"{name}:{dest}", data=tar)

    def copy_out(self, name: str, source: str, destination: str) -> None:
        _docker("cp", f"{name}:{source}", destination)

    def start(self, name: str) -> tuple[int, str, str]:
        result = _docker("start", "-a", name, check=False)
        return result.returncode, result.stdout.decode("utf-8", "replace"), result.stderr.decode("utf-8", "replace")

    def inspect(self, name: str) -> dict:
        return json.loads(_docker("inspect", name).stdout)[0]

    def commit(self, name: str, changes: list[str]) -> str:
        args = [x for c in changes for x in ("--change", c)]
        return _docker("commit", *args, name).stdout.decode().strip()

    def remove(self, name: str) -> None:
        _docker("rm", "-f", name, check=False)


def tar_inputs(inputs: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in sorted(inputs.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.uid, info.gid, info.mtime = len(data), 0o444, 0, 0, 0
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def read_regular(path: str, limit: int = MAX_INPUT_BYTES) -> bytes:
    try:
        info = os.lstat(path)
    except OSError:
        raise PrefixError(f"input {path} not found", "input-missing") from None
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise PrefixError(f"input {path} must be a regular file within limits (no symlinks)", "unsafe-input")
    return Path(path).read_bytes()


def run_once(runner, image: str, argv: list[str], expect_id: str) -> dict:
    """Run one command in a throwaway ``--network none`` container; proves .Image/network/exit code."""
    name = runner.create(image, argv[0], argv[1:], "none", limits=True)
    try:
        code, out, err = runner.start(name)
        info = runner.inspect(name)
    finally:
        runner.remove(name)
    if info.get("Image") != expect_id:
        raise PrefixError("runtime container .Image differs from the committed image ID", "image-id-mismatch")
    if info["HostConfig"].get("NetworkMode") != "none":
        raise PrefixError("verification container was not --network none", "network-not-none")
    if code != 0 or info["State"].get("ExitCode") != 0:
        raise PrefixError(f"`{' '.join(argv)}` failed ({code}): {redact(err[-500:])}", "verify-failed")
    return {"stdout": out, "image_id": info["Image"], "network_mode": "none", "exit_code": 0}


def parse_php_modules(text: str) -> list[str]:
    names, active = set(), False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            active = line == "[PHP Modules]"
        elif line and active:
            names.add(line.lower())
    return sorted(names)


def default_apt_prepare(package_request: dict) -> str:
    from mango.authoring.apt_prefix_cache import prepare_image
    from mango.authoring.packages import LockRequest

    return prepare_image(LockRequest.model_validate(package_request))


def oci_command(values: list[str] | None) -> str:
    return json.dumps(values or [])


def prepare(request: dict, runner=None, apt_prepare: Callable[[dict], str] = default_apt_prepare,
            lock_hash: Callable[[dict], str] = mango_lock_hash, remote_check: bool = True) -> dict:
    """Run the whole chain remotely and return a receipt. Fails closed at every step."""
    runner = runner or DockerRunner()
    if remote_check:
        require_remote_author()
    request = validate_request(request, lock_hash)
    chain = chain_hashes(request, request["apt_request_sha256"])
    files = {}
    for item in request["inputs"]:
        data = read_regular(item["path"])
        if sha256_hex(data) != item["sha256"]:
            raise PrefixError(f"input {item['name']} sha256 differs from the request", "input-mismatch")
        files[item["name"]] = data
    if runner.free_bytes() < LIMITS["disk_reserve_bytes"]:
        raise PrefixError("free disk is below the 40 GiB reserve", "disk-reserve")
    # The independently bound manifest may have been fetched through the explicit
    # Docker Hub mirror; inspect that actual local reference, retaining the original
    # base identity in the recipe chain.
    base_config = runner.image_inspect(request["apt"]["package_request"]["base"]).get("Config") or {}
    apt_image = apt_prepare(request["apt"]["package_request"])
    if not isinstance(apt_image, str) or not IMAGE_ID_RE.fullmatch(apt_image):
        raise PrefixError("Mango prepare_image did not return a sha256 image ID", "apt-image")
    if runner.image_inspect(apt_image).get("Id") != apt_image:
        raise PrefixError("apt-prepared image ID does not resolve to itself", "apt-image")
    script = request["post_apt"]["script"]
    name = runner.create(apt_image, "/bin/sh", ["-c", script], "none", limits=True)
    try:
        if files:
            runner.copy_in(name, tar_inputs(files), INPUT_DIR)
        code, _out, err = runner.start(name)
        info = runner.inspect(name)
        host = info["HostConfig"]
        checks = {
            "exit code": code == 0 and info["State"].get("ExitCode") == 0,
            "network none": host.get("NetworkMode") == "none",
            "cpus": host.get("NanoCpus") == 1_500_000_000,
            "memory": host.get("Memory") == LIMITS["memory_bytes"] and host.get("MemorySwap") == LIMITS["memory_swap_bytes"],
            "pids": host.get("PidsLimit") == LIMITS["pids"],
            "runs on apt image": info.get("Image") == apt_image,
            "exact script": info["Config"].get("Entrypoint") == ["/bin/sh"] and info["Config"].get("Cmd") == ["-c", script],
        }
        failed = sorted(k for k, ok in checks.items() if not ok)
        if failed:
            raise PrefixError(f"post-APT run failed verification: {failed}; stderr: {redact(err[-800:])}", "post-apt-failed")
        changes = [f"ENTRYPOINT {oci_command(base_config.get('Entrypoint'))}", f"CMD {oci_command(base_config.get('Cmd'))}",
                   f"LABEL {LABEL}={chain['recipe_sha256']}"]
        changes += [f"ENV {k}={v}" for k, v in sorted(request["env"].items())]
        image_id = runner.commit(name, changes)
    finally:
        runner.remove(name)
    if not IMAGE_ID_RE.fullmatch(image_id) or runner.image_inspect(image_id).get("Id") != image_id:
        raise PrefixError("committed image ID is malformed or unresolvable", "commit")
    version = run_once(runner, image_id, ["php", "-r", "echo PHP_VERSION;"], image_id)
    modules_run = run_once(runner, image_id, ["php", "-m"], image_id)
    modules = parse_php_modules(modules_run["stdout"])
    if version["stdout"].strip() != request["php_version"]:
        raise PrefixError(f"php is {version['stdout'].strip()!r}, expected {request['php_version']}", "toolchain-mismatch")
    absent = sorted(set(request["required_php_extensions"]) - set(modules))
    if absent:
        raise PrefixError(f"committed image lacks PHP extensions {absent}", "platform-requirements")
    receipt = {
        "schema": SCHEMA, "kind": KIND, "proof": runner.PROOF, "request_id": request["id"], "platform": "linux/amd64",
        "php_version": request["php_version"], "required_php_extensions": request["required_php_extensions"],
        "chain": chain,
        "recipe": request["recipe"], "post_apt": request["post_apt"], "apt": {"package_request": request["apt"]["package_request"]},
        "limits": request["limits"], "apt_image_id": apt_image, "image_id": image_id,
        "execution": {"network_mode": "none", "exit_code": 0, "run_image_id": apt_image, "commit_changes": changes},
        "toolchain": {"php_version": version["stdout"].strip(), "php_extensions": modules,
                      "php_extensions_sha256": sha256_hex("\n".join(modules).encode()),
                      "runtime_image_id": modules_run["image_id"], "network_mode": "none"},
        "authoring_only": True,
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    return receipt


# --------------------------------------------------------------------------
# Receipt verification: recompute the whole chain; trust nothing the receipt asserts
# --------------------------------------------------------------------------
def verify_receipt(receipt: object, *, base: str, runs: list[str], env: dict[str, str], php_version: str,
                   required_extensions: list[str], lock_hash: Callable[[dict], str] = mango_lock_hash,
                   inputs: dict[str, str] | None = None, allow_unproven: bool = False) -> dict:
    """Return the verified facts a consumer may use (image ID, apt hash, recipe hash) or raise.

    ``inputs`` is the caller's expected {name: sha256} of copied artifacts (default none). ``allow_unproven`` exists only so tests can exercise the mock path; real consumers never set it.
    """
    if not isinstance(receipt, dict) or receipt.get("kind") != KIND or receipt.get("schema") != SCHEMA:
        raise PrefixError("not a toolchain prefix receipt", "receipt-invalid")
    if receipt.get("proof") != "docker" and not allow_unproven:
        raise PrefixError(f"receipt proof is {receipt.get('proof')!r}, not a real Docker run", "receipt-unproven")
    body = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
    if canonical_sha256(body) != receipt.get("receipt_sha256"):
        raise PrefixError("receipt_sha256 does not match its content (tampered or hand-edited)", "receipt-invalid")
    if receipt.get("authoring_only") is not True:
        raise PrefixError("receipt must be marked authoring_only", "receipt-invalid")
    recipe = receipt.get("recipe") or {}
    if recipe != {"runs": runs, "env": env}:
        raise PrefixError("receipt recipe differs from the original Dockerfile's RUN/ENV", "recipe-mismatch")
    parsed = parse_recipe(runs)
    if receipt.get("post_apt") != {"commands": parsed["post_apt"], "script": parsed["script"]}:
        raise PrefixError("receipt post-APT script differs from the original commands", "recipe-mismatch")
    if receipt.get("php_version") != php_version or sorted(receipt.get("required_php_extensions") or []) != sorted(required_extensions):
        raise PrefixError("receipt PHP version/extension requirements differ from the plan", "toolchain-mismatch")
    apt_hash = check_apt({"package_request": (receipt.get("apt") or {}).get("package_request"),
                          "request_sha256": (receipt.get("chain") or {}).get("apt_request_sha256")}, base, parsed, lock_hash)
    shell_request = {"base": base, "recipe": {"runs": runs, "env": env}, "post_apt": receipt["post_apt"], "env": env,
                     "inputs": [{"name": n, "path": "/x", "sha256": h} for n, h in (inputs or {}).items()]}
    expected = chain_hashes(shell_request, apt_hash)
    if receipt.get("chain") != expected:
        raise PrefixError("receipt chain hashes (base/apt/script/env/inputs/recipe) do not recompute", "chain-mismatch")
    if receipt.get("limits") != LIMITS:
        raise PrefixError("receipt limits differ from the required limits", "invalid-limits")
    image = receipt.get("image_id")
    if not isinstance(image, str) or not IMAGE_ID_RE.fullmatch(image) or not IMAGE_ID_RE.fullmatch(str(receipt.get("apt_image_id"))):
        raise PrefixError("receipt image IDs are malformed", "receipt-invalid")
    execution, tool = receipt.get("execution") or {}, receipt.get("toolchain") or {}
    if (execution.get("network_mode"), execution.get("exit_code"), execution.get("run_image_id")) != ("none", 0, receipt["apt_image_id"]):
        raise PrefixError("receipt execution evidence is not network none / exit 0 on the apt image", "receipt-invalid")
    modules = tool.get("php_extensions")
    if (tool.get("runtime_image_id") != image or tool.get("network_mode") != "none" or tool.get("php_version") != php_version
            or not isinstance(modules, list) or set(required_extensions) - set(modules)
            or tool.get("php_extensions_sha256") != sha256_hex("\n".join(modules).encode())):
        raise PrefixError("receipt toolchain evidence (runtime image/PHP/modules) is inconsistent", "receipt-invalid")
    return {"image_id": image, "apt_request_sha256": apt_hash, "recipe_sha256": expected["recipe_sha256"],
            "script_sha256": expected["script_sha256"], "php_extensions_sha256": tool["php_extensions_sha256"]}


# --------------------------------------------------------------------------
# Safe Composer PHAR extraction (pinned original digest; no credentials; no vendor)
# --------------------------------------------------------------------------
COMPOSER_REF_RE = re.compile(r"^(?:docker\.io/library/)?composer@sha256:[0-9a-f]{64}$")


def extract_composer_phar(pinned: str, destination: Path, runner=None, pull: bool = False,
                          limit: int = 8 * 1024 * 1024, remote_check: bool = True) -> dict:
    """Copy ``/usr/bin/composer`` out of the digest-pinned original composer image; never executes it.

    The container is created with ``--network none`` and never started. ``pull`` (off by default) runs a
    ``docker pull`` of the exact digest with an EMPTY Docker config dir, so no registry credentials are
    used. Returns the sha256/path/image evidence for ``receipts.requests[*].composer``.
    """
    if remote_check:
        require_remote_author()
    if not COMPOSER_REF_RE.fullmatch(pinned):
        raise PrefixError("composer image must be composer@sha256:<64 hex> (the original pinned digest)", "invalid-base")
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise PrefixError(f"{destination} already exists; refusing to overwrite", "unsafe-source")
    runner = runner or DockerRunner()
    if pull:
        with tempfile.TemporaryDirectory() as empty:
            env = {"PATH": os.environ.get("PATH", ""), "DOCKER_CONFIG": empty,
                   **({"DOCKER_HOST": os.environ["DOCKER_HOST"]} if "DOCKER_HOST" in os.environ else {})}
            result = subprocess.run(["docker", "pull", "--platform", "linux/amd64", pinned], capture_output=True, env=env)
            if result.returncode:
                raise PrefixError("docker pull of the pinned composer digest failed: " + redact(result.stderr.decode()[-500:]), "docker")
    info = runner.image_inspect(pinned)
    digest = pinned.split("@", 1)[1]
    if (info.get("Os"), info.get("Architecture")) != ("linux", "amd64") or not any(d.endswith("@" + digest) for d in info.get("RepoDigests") or []):
        raise PrefixError("local composer image is not linux/amd64 with the pinned digest", "base-platform")
    name = runner.create(pinned, "/usr/bin/composer", [], "none", limits=True)
    try:
        with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
            runner.copy_out(name, "/usr/bin/composer", scratch)
            produced = Path(scratch) / "composer"
            data = read_regular(str(produced), limit)
            if not data.startswith(b"#!/usr/bin/env php"):
                raise PrefixError("extracted file is not the Composer PHAR launcher", "toolchain-mismatch")
            os.replace(produced, destination)
    finally:
        runner.remove(name)
    destination.chmod(0o644)
    return {"extracted_from": pinned, "image_id": info.get("Id"), "sha256": sha256_hex(data), "phar_path": str(destination.resolve()),
            "size": len(data), "executed": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mango-prepare-php", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="REMOTE ONLY: run the prefix request and write a receipt")
    p.add_argument("request", type=Path)
    p.add_argument("--output", type=Path, required=True)
    e = sub.add_parser("extract-composer", help="REMOTE ONLY: copy /usr/bin/composer out of the pinned image")
    e.add_argument("image")
    e.add_argument("--output", type=Path, required=True)
    e.add_argument("--pull", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            receipt = prepare(json.loads(args.request.read_text()))
        else:
            receipt = extract_composer_phar(args.image, args.output, pull=args.pull)
    except ResolverError as exc:
        print(f"php_prefix: [{getattr(exc, 'code', 'error')}] {exc}", file=sys.stderr)
        return 2
    if args.command == "prepare":
        with tempfile.NamedTemporaryFile("w", dir=args.output.parent, delete=False) as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(stream.name, args.output)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
