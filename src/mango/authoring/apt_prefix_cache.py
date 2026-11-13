"""Opt-in, bounded Docker snapshots of exact intermediate apt transactions.

These images are authoring caches, never environment outputs. Each receipt binds
the original immutable base, repository timeline, ordered package transactions,
resolver version and actual image ID. Cache misses retain ordinary apt selection.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from mango.authoring import packages as p

LABEL = "org.mango.apt-prefix"
VERSION = 1


def descriptor(request: p.LockRequest, count: int) -> dict:
    document = request.cache_document()
    document["stages"] = document["stages"][:count]
    return {"format": VERSION, "resolver": p.RESOLVER_VERSIONS["apt"], "request": document}


def key_for(document: dict) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()


def docker(*args, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and result.returncode:
        raise p.LockError("apt prefix cache Docker operation failed: " + result.stderr[-4000:])
    return result


@contextlib.contextmanager
def locked(root: Path, key: str, *, blocking: bool = True):
    with (root / (key + ".lock")).open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def read_receipt(path: Path, document: dict) -> dict | None:
    if not path.exists():
        return None
    receipt = json.loads(path.read_text())
    if receipt.get("descriptor") != document:
        return None
    image = receipt.get("image", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        return None
    result = docker("image", "inspect", image, check=False)
    if result.returncode:
        return None
    inspected = json.loads(result.stdout)[0]
    labels = inspected.get("Config", {}).get("Labels") or {}
    if inspected["Id"] != image or labels.get(LABEL) != path.stem:
        return None
    path.touch()
    return receipt


def save_receipt(path: Path, receipt: dict):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        json.dump(receipt, stream)
    os.replace(stream.name, path)


def prune(root: Path, protected: set[str]):
    """Remove only unused images owned by this cache, oldest first.

    Docker's non-force removal also protects images used by live containers.
    Size totals count shared layers repeatedly, conservatively bounding storage.
    """
    max_count = int(os.environ.get("MANGO_APT_PREFIX_MAX_IMAGES", "32"))
    max_bytes = int(os.environ.get("MANGO_APT_PREFIX_MAX_BYTES", str(64 * 1024**3)))
    receipts = []
    for path in root.glob("*.json"):
        try:
            receipt = json.loads(path.read_text())
            receipts.append((path.stat().st_mtime, path, receipt))
        except (OSError, ValueError):
            continue
    total = sum(r.get("size", 0) for _, _, r in receipts)
    count = len(receipts)
    for _, path, receipt in sorted(receipts):
        if count <= max_count and total <= max_bytes:
            break
        if path.stem in protected:
            continue
        with locked(root, path.stem, blocking=False) as acquired:
            if not acquired:
                continue
            # Revalidate ownership; never delete a user image by stale metadata.
            valid = read_receipt(path, receipt["descriptor"])
            if valid is not None and docker("image", "rm", receipt["image"], check=False).returncode:
                continue
            path.unlink(missing_ok=True)
            count -= 1
            total -= receipt.get("size", 0)


def resolve_output(request: p.LockRequest) -> str:
    return _resolve_output(request, install_final=False)[0]


def prepare_image(request: p.LockRequest) -> str:
    """Return an authoring-only image with every exact apt stage installed.

    This supports resolving later Python/Cargo dependencies against the same
    distro package state. The image is a local Docker content ID, not a build
    input; callers must retain the original immutable base and apt file locks.
    """
    return _resolve_output(request, install_final=True)[1]


def _resolve_output(request: p.LockRequest, *, install_final: bool) -> tuple[str, str]:
    root = p.cache_dir() / "apt-prefixes"
    root.mkdir(parents=True, exist_ok=True)
    outputs = []
    image = request.base
    held = contextlib.ExitStack()
    current = None
    try:
        for index in range(len(request.stages) - (0 if install_final else 1)):
            document = descriptor(request, index + 1)
            key = key_for(document)
            path = root / (key + ".json")
            # Retain prefix locks until the next container is created and run;
            # this also prevents eviction between lookup and Docker startup.
            current = contextlib.ExitStack()
            current.enter_context(locked(root, key))
            receipt = read_receipt(path, document)
            print(f"apt prefix {key[:12]} {'hit' if receipt is not None else 'miss'}",
                  file=sys.stderr, flush=True)
            if receipt is None:
                prefix = request.model_copy(update={"stages": request.stages[:index + 1]})
                script = p.apt_script(prefix, start=index, initialize=index == 0,
                                      install_last=True)
                ca = os.environ.get("SSL_CERT_FILE") or "/etc/ssl/certs/ca-certificates.crt"
                name = "mango-apt-prefix-" + uuid.uuid4().hex
                try:
                    from mango.authoring.package_inputs import mount_args
                    result = docker("run", "--name", name, "--platform", "linux/amd64",
                                    "--label", LABEL + "=" + key,
                                    "-v", f"{Path(ca).resolve()}:{p.CA_BUNDLE}:ro",
                                    *mount_args(prefix),
                                    "--entrypoint", "/bin/sh", image, "-c", script)
                    output = p.filter_local_deb_uris(prefix, result.stdout)
                    lines = [line.strip() for line in output.splitlines()]
                    markers = [line for line in lines if line.startswith(p.STAGE_MARKER)]
                    if markers != [f"{p.STAGE_MARKER}{index}"] or any(
                        line.startswith("'") and not p.APT_URI.fullmatch(line) for line in lines
                    ):
                        raise p.LockError("Incomplete apt prefix transaction output")
                    committed = docker("commit", "--change", f"LABEL {LABEL}={key}", name).stdout.strip()
                    info = json.loads(docker("image", "inspect", committed).stdout)[0]
                    receipt = {"descriptor": document, "image": info["Id"],
                               "size": info["Size"], "output": output}
                    save_receipt(path, receipt)
                finally:
                    docker("rm", "-f", name, check=False)
            outputs.append(receipt["output"])
            image = receipt["image"]
            held.close()
            held = current
        if not install_final:
            final = request.model_copy(update={"base": image})
            outputs.append(p._run_in_base(final, p.apt_script(
                request, start=len(request.stages) - 1, initialize=False)))
    finally:
        if current is not None:
            current.close()
        held.close()
        prune(root, set())
    return "\n".join(outputs), image
