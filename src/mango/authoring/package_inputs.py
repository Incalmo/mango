"""Verified authoring-only mounts for package files and repository keys."""

import hashlib
import os
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

from mango.authoring import packages as p

MOUNT_ROOT = "/mango/author-debs"
KEY_MOUNT_ROOT = "/mango/author-keys"


def _verify(stream, expected, destination=None):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
        if destination is not None:
            destination.write(chunk)
    if digest.hexdigest() != expected:
        raise p.LockError("Input does not match its declared SHA256")


def materialize(item: p.LocalDeb) -> Path:
    return _materialize(item, p.cache_dir() / "local-debs" / (item.sha256 + ".deb"))


def materialize_key(item: p.RepositoryKey) -> Path:
    return _materialize(item, p.cache_dir() / "repository-keys" / item.sha256)


def _materialize(item: p.PinnedFile, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            with target.open("rb") as stream:
                _verify(stream, item.sha256)
        except p.LockError:
            target.unlink(missing_ok=True)
            raise
        return target
    if item.author_source:
        source = Path(item.author_source).open("rb")
    else:
        # URLs are strict stable public references validated by LocalDeb.
        try:
            source = urlopen(Request(item.urls[0], headers={"User-Agent": "mango-lock-packages/1.0"}),
                             timeout=120)
        except Exception as exc:
            # CDN redirect errors can embed temporary credentials in their URL.
            raise p.LockError("Pinned input download failed: " + type(exc).__name__
                              + " status=" + str(getattr(exc, "code", None))) from None
    temporary = None
    try:
        with source, tempfile.NamedTemporaryFile("wb", dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            _verify(source, item.sha256, output)
        os.replace(temporary, target)
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def mount_args(request: p.LockRequest) -> list[str]:
    result = []
    seen = set()
    for stage in request.stages:
        for item in stage.local_debs:
            if item.sha256 not in seen:
                seen.add(item.sha256)
                path = materialize(item).resolve()
                result += ["-v", f"{path}:{MOUNT_ROOT}/{item.sha256}.deb:ro"]
        for item in getattr(stage, "repository_keys", ()):
            identity = ("key", item.sha256)
            if identity not in seen:
                seen.add(identity)
                path = materialize_key(item).resolve()
                result += ["-v", f"{path}:{KEY_MOUNT_ROOT}/{item.sha256}:ro"]
    return result
