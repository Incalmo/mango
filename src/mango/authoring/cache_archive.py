"""Validate and pin a package manager's original cache as a source artifact.

This records bytes and filesystem metadata; callers must separately prove that
their package manager can replay the cache with the network disabled.
"""

import hashlib
import os
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def pin_cache_archive(
    source: Path,
    destination: Path,
    *,
    prefix: str,
    max_archive_bytes: int = 512 * 1024 * 1024,
    max_expanded_bytes: int = 1024 * 1024 * 1024,
) -> dict:
    """Publish a regular-file/directory-only cache tar atomically by SHA256."""
    source, destination = Path(source), Path(destination)
    parts = prefix.split("/")
    if any(part in ("", ".", "..") for part in parts) or "\\" in prefix:
        raise ValueError("Cache input prefix must be a safe relative path")
    if source.is_symlink() or not source.is_file():
        raise ValueError("Cache archive must be a regular file")
    if source.stat().st_size > max_archive_bytes:
        raise ValueError("Cache archive exceeds its size limit")
    members, total, seen = [], 0, set()
    with tarfile.open(source, "r:gz") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not (member.isfile() or member.isdir())
                or str(path) in seen
            ):
                raise ValueError("Unsupported or duplicate cache archive member")
            seen.add(str(path))
            total += member.size
            if total > max_expanded_bytes:
                raise ValueError("Expanded cache exceeds its size limit")
            item = {
                "path": member.name,
                "mode": member.mode,
                "uid": member.uid,
                "gid": member.gid,
                "size": member.size,
                "type": "file" if member.isfile() else "directory",
            }
            if member.isfile():
                with archive.extractfile(member) as stream:
                    item["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
            members.append(item)
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / (digest + ".tar.gz")
    # A Nix shell's temporary directory can be on another filesystem. Stage
    # the copy beside its destination before publishing it atomically.
    with tempfile.NamedTemporaryFile(dir=destination, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            with source.open("rb") as original:
                shutil.copyfileobj(original, stream)
            stream.flush()
            os.fsync(stream.fileno())
            with temporary.open("rb") as copied:
                if hashlib.file_digest(copied, "sha256").hexdigest() != digest:
                    raise ValueError("Cache archive changed while being published")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        "path": prefix + "/" + target.name,
        "sha256": digest,
        "size": target.stat().st_size,
        "author_source": str(target),
        "members": members,
    }
