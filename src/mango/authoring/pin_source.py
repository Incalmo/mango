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
import base64
import hashlib
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

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


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _sri_to_hex(sri: str) -> str:
    return base64.b64decode(sri.partition("-")[2]).hex()


def _scratch_directory_exists(path: str) -> bool:
    candidate = Path(path)
    return candidate.is_dir() and not candidate.is_symlink()


def _cleanup_probe(expression: str, name: str, got: str | None) -> list[str]:
    """Delete this invocation's own probe outputs; return warnings for any left behind.

    Ownership: ``name`` embeds a random per-invocation token, and a store path
    commits to its name, so no other invocation (even for the same
    url/rev/options) and no real ``fetchgit`` source output can have these
    paths. Nix keeps a hash-mismatched fixed-output result at the path for the
    *actual* hash, so the candidates are exactly: that path (computed purely
    from ``got`` and ``name``) and the expected fake-hash output (read from the
    derivation). Deletion uses plain ``nix-store --delete``, which refuses
    live paths; there is no GC, ignore-liveness or scan of the store.
    """
    warnings: list[str] = []
    candidates: list[str] = []
    drv = _run(["nix-instantiate", "--expr", expression])
    if drv.returncode == 0 and drv.stdout.strip():
        outs = _run(["nix-store", "--query", "--outputs", drv.stdout.strip()])
        if outs.returncode == 0:
            candidates += outs.stdout.split()
    if not candidates:
        warnings.append("cannot determine expected probe output path")
    if got is not None:
        fixed = _run(["nix-store", "--print-fixed-path", "--recursive", "sha256",
                      _sri_to_hex(got), name])
        if fixed.returncode == 0 and fixed.stdout.strip():
            candidates.append(fixed.stdout.strip())
        else:
            warnings.append("cannot compute relocated probe output path")
    for path in dict.fromkeys(candidates):
        if not re.fullmatch(r"/nix/store/[0-9a-z]{32}-" + re.escape(name), path):
            warnings.append(f"refusing to delete unexpected path {path}")
            continue
        registered = _run(["nix-store", "--check-validity", path]).returncode == 0
        if not registered and not _scratch_directory_exists(path):
            continue  # absent; a failed fixed-output build can leave an unregistered directory
        deleted = _run(["nix-store", "--delete", path])
        if deleted.returncode != 0:
            warnings.append(f"could not delete {path}: {deleted.stderr.strip()}")
    return warnings


def content_hash(repository: str, commit: str, options: dict | None = None) -> str:
    """Fetch with a fake hash and read the real one from Nix's mismatch report.

    The probe's scratch output is deleted afterwards (see ``_cleanup_probe``); a
    failed cleanup only warns, so it never hides the hash.
    """
    options = options or {}
    allowed = {"leaveDotGit", "deepClone", "fetchSubmodules", "fetchTags"}
    if set(options) - allowed or any(type(value) is not bool for value in options.values()):
        raise ValueError("Unsupported fetchgit options")
    option_fields = " ".join(f"{key} = {json.dumps(value)};" for key, value in options.items())
    suffix = hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()[:8]
    # Unique per invocation: parallel probes share neither a store lock nor
    # an output path, so one cleanup can never delete another's probe.
    name = f"probe-{commit[:12]}-{suffix}-{uuid.uuid4().hex}"
    expression = (
        f"let h = import {json.dumps(str(HARNESS))} {{}}; in h.pkgs.fetchgit {{ "
        f"url = {json.dumps(repository)}; rev = {json.dumps(commit)}; "
        f"hash = h.pkgs.lib.fakeHash; "
        f"{option_fields} "
        f'name = "{name}"; }}'
    )
    result = _run(["nix-build", "--no-out-link", "--expr", expression])
    match = GOT.search(result.stderr)
    got = match.group(1) if match else None
    try:
        warnings = _cleanup_probe(expression, name, got)
    except Exception as error:  # cleanup must never mask the outcome
        warnings = [str(error)]
    for warning in warnings:
        print(f"warning: probe cleanup: {warning}; leaving scratch {name} "
              "for operator review", file=sys.stderr)
    if got is None:
        raise RuntimeError("Cannot determine source content hash:\n" + result.stderr)
    return got


def pin(repository: str, rev: str = "HEAD", *, leave_dot_git: bool = False,
        deep_clone: bool = False, fetch_tags: bool = False,
        fetch_submodules: bool = True) -> dict:
    commit = resolve_commit(repository, rev)
    options = {}
    if leave_dot_git or deep_clone or fetch_tags or not fetch_submodules:
        options = {
            "leaveDotGit": leave_dot_git or deep_clone or fetch_tags,
            "deepClone": deep_clone,
            "fetchTags": fetch_tags,
            "fetchSubmodules": fetch_submodules,
        }
    return {
        "repository": repository,
        "commit": commit,
        "hash": content_hash(repository, commit, options),
        **options,
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
    parser.add_argument("--leave-dot-git", action="store_true", help="retain Git metadata")
    parser.add_argument("--deep-clone", action="store_true", help="fetch full history and retain Git metadata")
    parser.add_argument("--fetch-tags", action="store_true", help="fetch tags and retain Git metadata")
    parser.add_argument("--no-submodules", action="store_true", help="do not fetch submodules")
    args = parser.parse_args()
    value = pin(args.repository, args.rev, leave_dot_git=args.leave_dot_git,
                deep_clone=args.deep_clone, fetch_tags=args.fetch_tags,
                fetch_submodules=not args.no_submodules)
    print(nix(value) if args.nix else json.dumps(value))


if __name__ == "__main__":
    main()
