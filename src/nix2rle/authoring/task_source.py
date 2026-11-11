"""Materialize an explicitly pinned task archive; never discover build inputs."""

import argparse
import ctypes
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

import zstandard


def _rule(value):
    """One canonical-manifest rule: a header line and excluded path globs."""
    if not isinstance(value, dict) or set(value) != {"header", "exclude"}:
        raise ValueError("Canonical rules need exactly header and exclude")
    header, exclude = value["header"], value["exclude"]
    if not isinstance(header, str) or not header or "\n" in header:
        raise ValueError("Canonical header must be one nonempty line")
    if not isinstance(exclude, list) or not all(
        isinstance(p, str) and p and "\n" not in p for p in exclude
    ):
        raise ValueError("Canonical exclude must be a list of path globs")
    return value


def read_descriptor(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate descriptor field: {key}")
            result[key] = value
        return result

    value = json.loads(path.read_text(), object_pairs_hook=unique)
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "archive_sha256",
        "registered_digest",
        "canonical",
    }:
        raise ValueError("Task source descriptor has missing or unknown fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 2:
        raise ValueError("Unsupported task source descriptor version")
    for key, pattern in (
        ("archive_sha256", r"[0-9a-f]{64}"),
        ("registered_digest", r"sha256:[0-9a-f]{64}"),
    ):
        if not isinstance(value[key], str) or not re.fullmatch(pattern, value[key]):
            raise ValueError(f"Invalid {key}")
    if not isinstance(value["canonical"], list) or not value["canonical"]:
        raise ValueError("Descriptor needs at least one canonical rule")
    for rule in value["canonical"]:
        _rule(rule)
    return value


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def manifest_entries(root):
    """(path, line) for every file and symlink, sorted by path.

    Each line is ``<kind> <mode> <size> <sha256> <path>``: regular files keep
    only the owner-executable bit (755 or 644); symlinks hash their target.
    """
    entries = []

    def visit(directory):
        for path in sorted(directory.iterdir()):
            mode = path.lstat().st_mode
            name = path.relative_to(root).as_posix()
            if stat.S_ISDIR(mode):
                visit(path)
            elif stat.S_ISLNK(mode):
                target = os.readlink(path).encode()
                entries.append(
                    (
                        name,
                        f"symlink 777 {len(target)} {hashlib.sha256(target).hexdigest()} {name}",
                    )
                )
            elif stat.S_ISREG(mode):
                normalized = "755" if mode & stat.S_IXUSR else "644"
                entries.append(
                    (
                        name,
                        f"file {normalized} {path.stat().st_size} {file_hash(path)} {name}",
                    )
                )
            else:
                raise ValueError(f"Unsupported task file: {name}")

    visit(root)
    return sorted(entries)


def canonical_digests(root, rules):
    """The digest of ``root`` under each rule, in rule order, without repeats.

    A rule's digest is the SHA-256 of its header line followed by the manifest
    lines of every path not matching one of its ``exclude`` globs, each line
    newline-terminated. Globs use ``fnmatch`` against the task-relative path,
    so ``notes/*`` excludes a whole directory.
    """
    entries = manifest_entries(root)
    digests = []
    for rule in map(_rule, rules):
        rows = [
            line
            for name, line in entries
            if not any(fnmatch.fnmatchcase(name, pattern) for pattern in rule["exclude"])
        ]
        text = "\n".join([rule["header"], *rows]) + "\n"
        digests.append("sha256:" + hashlib.sha256(text.encode()).hexdigest())
    return tuple(dict.fromkeys(digests))


def verify(task, descriptor):
    if descriptor["registered_digest"] not in canonical_digests(
        task, descriptor["canonical"]
    ):
        raise ValueError("Task canonical digest does not match descriptor")
    return {"verified": True, "registered_digest": descriptor["registered_digest"]}


def safe_name(name):
    if (
        not name
        or name.startswith("/")
        or any(part in {"", ".", ".."} for part in name.split("/"))
    ):
        raise ValueError(f"Unsafe archive path: {name!r}")
    return name


def extract(archive, root):
    seen = set()
    directories = []
    links = []
    with (
        archive.open("rb") as raw,
        zstandard.ZstdDecompressor().stream_reader(raw) as stream,
    ):
        with tarfile.open(fileobj=stream, mode="r|") as tar:
            for member in tar:
                name = safe_name(
                    member.name.rstrip("/") if member.isdir() else member.name
                )
                if name in seen:
                    raise ValueError(f"Duplicate archive path: {name}")
                seen.add(name)
                if member.mode & ~0o777:
                    raise ValueError(f"Unsupported special permission bits: {name}")
                target = root / name
                if any(
                    (root / parent).is_symlink()
                    for parent in PurePosixPath(name).parents
                ):
                    raise ValueError(f"Archive path traverses a symlink: {name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                if member.isdir():
                    target.mkdir(exist_ok=True)
                    directories.append((target, member.mode, member.mtime))
                elif member.isfile():
                    with target.open("xb") as output, tar.extractfile(member) as source:
                        shutil.copyfileobj(source, output)
                    target.chmod(member.mode & 0o777)
                    os.utime(target, (member.mtime, member.mtime))
                elif member.issym():
                    if member.linkname.startswith("/"):
                        raise ValueError(f"Absolute archive symlink: {name}")
                    target.symlink_to(member.linkname)
                    os.utime(
                        target, (member.mtime, member.mtime), follow_symlinks=False
                    )
                    links.append(target)
                else:
                    raise ValueError(
                        f"Unsupported archive entry (including hardlinks): {name}"
                    )
    for link in links:
        if not link.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Archive symlink escapes task: {link.relative_to(root)}")
    for path, mode, mtime in reversed(directories):
        path.chmod(mode & 0o777)
        os.utime(path, (mtime, mtime))


def publish_no_replace(source, destination):
    # renameat2 is atomic even if another writer creates the target after verification.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


def materialize(archive, descriptor, destination):
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Refusing to overwrite task destination: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".task-source-", dir=destination.parent
    ) as temporary:
        pinned_archive = Path(temporary) / "archive.tar.zst"
        shutil.copyfile(archive, pinned_archive)
        if file_hash(pinned_archive) != descriptor["archive_sha256"]:
            raise ValueError("Task archive SHA-256 does not match descriptor")
        root = Path(temporary) / "task"
        root.mkdir()
        extract(pinned_archive, root)
        result = verify(root, descriptor)
        publish_no_replace(root, destination)
    return result


def main():
    parser = argparse.ArgumentParser(prog="python -m nix2rle.authoring.task_source", description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    materializer = sub.add_parser("materialize")
    materializer.add_argument("--archive", type=Path, required=True)
    materializer.add_argument("--out", type=Path, required=True)
    verifier = sub.add_parser("verify")
    verifier.add_argument("--task", type=Path, required=True)
    for command in (materializer, verifier):
        command.add_argument("--descriptor", type=Path, required=True)
    args = parser.parse_args()
    descriptor = read_descriptor(args.descriptor)
    result = (
        materialize(args.archive, descriptor, args.out)
        if args.action == "materialize"
        else verify(args.task, descriptor)
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
