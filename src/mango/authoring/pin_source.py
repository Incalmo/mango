"""Pin a Git source for ``pkgs.fetchgit { url; rev; hash; }``.

Authoring-time only: this contacts the remote. It resolves a branch, tag or
``HEAD`` to a full commit, then fetches that commit with the harness's own
``pkgs.fetchgit`` to learn the Nix content hash. The hash is therefore the one
a build checks, with ``fetchgit``'s defaults (submodules included). Commit the
printed ``{ repository; commit; hash; }``; builds never resolve refs.

    mango-pin-source https://github.com/owner/app.git
    mango-pin-source https://github.com/owner/app.git --rev v1.2.3 --nix
"""

import argparse
import json
import re
import subprocess

from mango import HARNESS
from mango.authoring.pin_base import GOT, nix

COMMIT = re.compile(r"[0-9a-f]{40}")


def resolve_commit(repository: str, rev: str) -> str:
    """The commit ``rev`` names on the remote; a full commit is kept as is."""
    if COMMIT.fullmatch(rev):
        return rev
    output = subprocess.run(
        ["git", "ls-remote", "--", repository, rev, f"{rev}^{{}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    refs: dict[str, str] = {}
    for line in output.splitlines():
        commit, _, name = line.partition("\t")
        refs[name] = commit
    # An annotated tag's own object is not a commit; its peeled entry is. A
    # branch wins over a tag of the same name, as it does for `git checkout`.
    for name in (rev, f"refs/heads/{rev}", f"refs/tags/{rev}^{{}}", f"refs/tags/{rev}"):
        if name in refs:
            return refs[name]
    raise ValueError(f"{repository} has no branch or tag named {rev!r}")


def content_hash(repository: str, commit: str) -> str:
    """Fetch with a fake hash and read the real one from Nix's mismatch report."""
    expression = (
        f"let h = import {json.dumps(str(HARNESS))} {{}}; in h.pkgs.fetchgit {{ "
        f"url = {json.dumps(repository)}; rev = {json.dumps(commit)}; "
        f"hash = h.pkgs.lib.fakeHash; "
        # A unique name keeps parallel probes from sharing one store lock.
        f'name = "probe-{commit[:12]}"; }}'
    )
    result = subprocess.run(
        ["nix-build", "--no-out-link", "--expr", expression],
        capture_output=True,
        text=True,
        check=False,
    )
    match = GOT.search(result.stderr)
    if not match:
        raise RuntimeError("Cannot determine source content hash:\n" + result.stderr)
    return match.group(1)


def pin(repository: str, rev: str = "HEAD") -> dict:
    commit = resolve_commit(repository, rev)
    return {
        "repository": repository,
        "commit": commit,
        "hash": content_hash(repository, commit),
    }


def main():
    parser = argparse.ArgumentParser(
        prog="mango-pin-source", description=__doc__.split("\n")[0]
    )
    parser.add_argument(
        "repository", help="Git URL, e.g. https://github.com/owner/app.git"
    )
    parser.add_argument(
        "--rev", default="HEAD", help="branch, tag, HEAD or full commit (default: HEAD)"
    )
    parser.add_argument("--nix", action="store_true", help="print a Nix attribute set")
    args = parser.parse_args()
    value = pin(args.repository, args.rev)
    print(nix(value) if args.nix else json.dumps(value))


if __name__ == "__main__":
    main()
