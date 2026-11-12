"""Copy build context and declared input farms without flattening payload links."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


def in_store(path: Path, store: Path) -> bool:
    return path.is_absolute() and path.is_relative_to(store) and ".." not in path.parts


def payload(source: Path, destination: Path) -> None:
    """Copy one declared payload, preserving every symlink inside it."""
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)


def store_reference(source: Path, store: Path) -> Path:
    """Resolve store-to-store output aliases, never a runtime filesystem link."""
    seen = set()
    while True:
        parent = source.parent.resolve(strict=True)
        if not in_store(parent, store):
            raise ValueError(
                f"Declared input traverses outside the Nix store: {source}"
            )
        source = parent / source.name
        if source in seen:
            raise ValueError(f"Cyclic Nix input reference: {source}")
        seen.add(source)
        if not source.is_symlink():
            return source
        target = Path(os.readlink(source))
        if not in_store(target, store):
            return source  # The payload itself is a link; copy its text unchanged.
        source = target


def inputs(
    source: Path, destination: Path, *, store: Path = Path("/nix/store")
) -> None:
    """Traverse farm structure; a store reference marks a payload boundary."""
    if source.is_symlink():
        target = Path(os.readlink(source))
        if in_store(target, store):
            payload(store_reference(target, store), destination)
        else:
            destination.symlink_to(os.readlink(source))
    elif source.is_dir():
        destination.mkdir()
        for child in source.iterdir():
            inputs(child, destination / child.name, store=store)
        shutil.copystat(source, destination, follow_symlinks=False)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["context", "inputs"])
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.mode == "context":
        payload(args.source, args.destination)
    else:
        inputs(args.source, args.destination)


if __name__ == "__main__":
    main()
