"""Export explicitly selected native files without following runtime symlinks."""

import os
import stat
import tarfile
from pathlib import PurePosixPath

EXCLUDED = {"dev", "proc", "sys", ".nix-inputs"}


def export_artifacts(root, paths, output, timestamp=1):
    selected = set()

    def collect(name):
        path = root / name
        kind = path.lstat().st_mode
        if not (stat.S_ISREG(kind) or stat.S_ISDIR(kind) or stat.S_ISLNK(kind)):
            raise ValueError(
                f"Artifact is not a regular file, directory or symlink: {name}"
            )
        selected.add(name)
        if stat.S_ISDIR(kind):
            for child in sorted(path.iterdir()):
                collect(f"{name}/{child.name}")

    for value in paths:
        if (
            not isinstance(value, str)
            or not value.startswith("/")
            or any(part in {"", ".", ".."} for part in value[1:].split("/"))
        ):
            raise ValueError(
                f"Artifact path must be an absolute normalized path: {value!r}"
            )
        name = value[1:]
        if name.split("/")[0] in EXCLUDED:
            raise ValueError(f"Cannot export build mount: {value}")
        for parent in PurePosixPath(name).parents:
            if (root / parent).is_symlink():
                raise ValueError(f"Artifact path has a symlink ancestor: {value}")
        collect(name)

    def normalize(member):
        member.mtime = timestamp
        member.uname = member.gname = ""
        member.pax_headers = {}
        return member

    with tarfile.open(
        output, "w", format=tarfile.PAX_FORMAT, dereference=False
    ) as archive:
        for name in sorted(selected):
            archive.add(root / name, arcname=name, recursive=False, filter=normalize)
    os.chmod(output, 0o644)
