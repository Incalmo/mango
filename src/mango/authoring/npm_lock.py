#!/usr/bin/env python3
"""Resolve an npm project's registry tarballs to a hash-verified inventory, REMOTE HOST ONLY.

Authoring-time resolver for ``npm ci`` projects. Its output feeds Mango's runtime
*offline* build; it does not translate or reproduce the original image. Run it only
on the remote authoring host with ``MANGO_REMOTE_AUTHOR=1`` and Docker there.
Nothing here may be run on a developer machine.

Supported subset (everything else is rejected, never approximated)
 - manager ``npm`` only, ``command: "ci"`` only, version already in the base and
   equal to the request's exact version. ``pnpm`` and ``yarn`` are refused with
   reason ``unsupported-manager`` (see "Limits").
 - ``package-lock.json`` / ``npm-shrinkwrap.json`` content, lockfileVersion 2 or 3.
   Registry tarballs only (https, no credentials, no query); no workspaces, no Git,
   file/link, remote-URL or alias-to-non-registry sources.
 - Flags (``=`` form only): --omit=dev|optional|peer, --include=dev|optional|peer,
   --legacy-peer-deps, --strict-peer-deps, --no-audit, --no-fund, --no-progress,
   --ignore-scripts. Environment: NODE_ENV only. Anything else (--force,
   --workspace*, --os/--cpu/--libc, --registry, --install-strategy, ...) is rejected.

Request file::

    {"requests": [{
        "id": "app",
        "base": "node@sha256:<64 hex>",              # pinned linux/amd64 image
        "manager": {"name": "npm", "version": "10.8.2"},
        "command": "ci",
        "flags": ["--omit=dev", "--no-audit"],       # the original relevant flags
        "env": {"NODE_ENV": "production"},           # optional
        "files": {"package_json": "...", "package_lock": "..."},
        # or: "source_path": "/abs/remote/dir", "project_dir": "frontend"
        "limits": {...}, "timeout_seconds": 1800, "replay": true}]}

Phases, in one disposable root container (``--cpus 1.5 --memory 4g``) on the base:
 1. Only package.json and the lock are copied in. ``npm ci --ignore-scripts
    --cache <fresh>`` (plus the original flags) populates the manager's own cache.
    The lock is never regenerated; sha256 of both files is checked before/after
    inside the container and again on the host source.
 2. ``node_modules/.package-lock.json`` (the hidden lockfile, written by npm itself)
    gives the set npm actually installed for this platform and flags (platform-
    restricted optional dependencies and omitted types are therefore absent).
    Every installed entry must equal the project lock's entry for version,
    resolved URL and integrity (``lock-drift`` otherwise).
 3. Each distinct tarball is downloaded on the host from its ORIGINAL public URL; its
    bytes must match every SHA-256/384/512 value of the lock integrity string.
    sha1-only integrity is rejected. The file SHA-256, SHA-512 and size are recorded.
 4. Replay (default on): the container network is disconnected, a second empty cache
    is filled with ``npm cache add <tarball file>`` from the host-verified bytes and
    ``npm ci --offline`` must reproduce the identical installed tree. Only then is
    ``offline.verified_by_replay`` true.

Lockless ``npm install`` (the corpus pattern ``cd /opt/viteapp && npm install``)
 - ``command: "install"`` is accepted ONLY with no lock in the input (``files`` holds
   just ``package_json``; ``source_path`` has no package-lock.json/npm-shrinkwrap.json)
   and no package arguments, no ``-g``, no flags beyond the allowlist above. With an
   existing lock it stays ``unsupported-command`` (``npm ci`` semantics are unchanged).
 - Fetch phase: the pinned base's own npm runs the ORIGINAL ``npm install`` plus only
   ``--ignore-scripts --no-audit --no-fund``. npm's own solver picks the versions; nothing
   is pinned or upgraded here. package.json must stay byte-identical. The package-lock.json
   npm GENERATED is captured byte-for-byte, validated (lockfileVersion 2/3, registry
   https tarballs, strong integrity, root entry equals the manifest dependency maps) with
   the same logic as for ``npm ci``, and compared to the tree npm installed.
 - The result carries it as ``generated_lock`` (sha256, size, base64 bytes). The absent
   lock in the input is an intentional generated artifact, not a source change.
   The translated build stage writes the ORIGINAL package.json plus ``generated_lock`` and
   runs the original ``npm install --offline`` (scripts enabled) against the verified
   cache; npm then reads the lock, so the selection equals the captured network install.
 - Replay (network none) removes node_modules and re-runs the same ``npm install --offline
   --ignore-scripts`` from the generated lock: the installed tree AND the lock bytes must be
   identical, otherwise ``offline.verified_by_replay`` is false. Original build commands
   (``npm run build``...) are untouched, must run offline in Mango, and are not qualified here.
 - ``status: "completed"`` does NOT mean verified. Certification must call
   ``require_verified(result)`` (or run the CLI with ``--require-verified``).

The fetch phase forces ``--ignore-scripts``; the recorded offline install command
does NOT, so the translated build runs the original lifecycle scripts offline.
Lifecycle scripts (and anything they download) are listed under ``unresolved``.

With ``capture_cache: true`` and ``--cache-directory DIR``, a further disconnected
install runs the original lifecycle scripts against the original manager cache.
Package selection, generated lock and source manifest must remain unchanged.
The manager-created cache (including registry metadata) is then retained as a
hash-checked source archive; this is dependency state, not an environment image.

Results are checkpointed after every request, in the style of remote_go_deps.py.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Self

RESOLVER_VERSION = "node-deps-1"
REQUEST_KEYS = {
    "id",
    "base",
    "platform",
    "manager",
    "command",
    "flags",
    "env",
    "files",
    "source_path",
    "project_dir",
    "limits",
    "timeout_seconds",
    "replay",
    "prepared_image",
    "capture_cache",
}
BASE_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
DIR_RE = re.compile(
    r"^(\.|[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)*)$"
)
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")
SRI_RE = re.compile(r"^(sha1|sha256|sha384|sha512)-([A-Za-z0-9+/]+={0,2})(\?\S*)?$")
DIGEST_SIZE = {"sha1": 20, "sha256": 32, "sha384": 48, "sha512": 64}
STRONG = ("sha512", "sha384", "sha256")
OMIT_TYPES = {"dev", "optional", "peer"}
BOOL_FLAGS = {
    "--legacy-peer-deps",
    "--strict-peer-deps",
    "--no-audit",
    "--no-fund",
    "--no-progress",
    "--ignore-scripts",
}
REGISTRY = "https://registry.npmjs.org/"
GIT_HOSTS = {"github.com", "codeload.github.com", "gitlab.com", "bitbucket.org"}
ROOT_LIFECYCLE = (
    "preinstall",
    "install",
    "postinstall",
    "prepublish",
    "preprepare",
    "prepare",
    "postprepare",
)
WORK = "/tmp/mango-node-deps"
PROJECT = f"{WORK}/project"
CACHE = f"{WORK}/cache"
REPLAY_CACHE = f"{WORK}/cache-replay"
TARBALLS = f"{WORK}/tarballs"
CONTAINER_LIMITS = {"cpus": "1.5", "memory": "4g", "pids": "1024"}
DEFAULT_LIMITS = {
    "max_lock_bytes": 32 << 20,
    "max_hidden_lock_bytes": 64 << 20,
    "max_tarball_bytes": 256 << 20,
    "max_total_bytes": 4 << 30,
    "max_packages": 6000,
}
HARD_LIMITS = {
    "max_lock_bytes": 64 << 20,
    "max_hidden_lock_bytes": 128 << 20,
    "max_tarball_bytes": 1 << 30,
    "max_total_bytes": 16 << 30,
    "max_packages": 20_000,
}
DEFAULT_TIMEOUT = 1800
MAX_TIMEOUT = 3600
def _default_progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


PROGRESS: Callable[[str], None] = _default_progress


class ResolverError(ValueError):
    """A request or resolution result this resolver refuses to accept."""

    def __init__(self, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# Gate, redaction, hashing
# --------------------------------------------------------------------------
def require_remote_author() -> None:
    if os.environ.get("MANGO_REMOTE_AUTHOR") != "1":
        raise ResolverError(
            "Set MANGO_REMOTE_AUTHOR=1 only on the remote authoring host",
            "not-remote-author",
        )


_URL_AUTH_RE = re.compile(r"(://)[^/@\s]+@")
_URL_QUERY_RE = re.compile(r"(https?://[^\s?#]+)[?#][^\s]*")


def redact(text: str) -> str:
    """Strip URL userinfo, query strings and fragments from diagnostic text."""
    return _URL_QUERY_RE.sub(r"\1?<redacted>", _URL_AUTH_RE.sub(r"\1<redacted>@", text))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# Request validation (pure)
# --------------------------------------------------------------------------
def parse_flags(flags: object) -> dict:
    """Validate original npm flags against the allowlist; return a normal form."""
    if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
        raise ResolverError("flags must be a list of strings", "invalid-request")
    parsed = {"omit": [], "include": [], "bool": [], "argv": []}
    for flag in flags:
        name, eq, value = flag.partition("=")
        if flag in BOOL_FLAGS:
            parsed["bool"].append(flag)
        elif name in ("--omit", "--include") and eq:
            if value not in OMIT_TYPES:
                raise ResolverError(
                    f"{name} value must be one of {sorted(OMIT_TYPES)}",
                    "unsupported-flag",
                )
            parsed[name[2:]].append(value)
        elif flag in ("--workspace", "--workspaces", "-w", "-ws"):
            raise ResolverError("workspaces are not supported", "workspaces")
        else:
            raise ResolverError(
                f"unsupported npm flag {name!r} (allowed: {sorted(BOOL_FLAGS)} and --omit=/--include= with =)",
                "unsupported-flag",
            )
        parsed["argv"].append(flag)
    return parsed


def _check_relative_dir(value: object) -> str:
    if (
        not isinstance(value, str)
        or not DIR_RE.match(value)
        or ".." in value.split("/")
    ):
        raise ResolverError(f"unsafe project_dir {value!r}", "invalid-request")
    return value


def validate_request(request: object) -> dict:
    """Validate against the explicit schema and return a normalised copy."""
    if not isinstance(request, dict):
        raise ResolverError("request must be an object", "invalid-request")
    unknown = set(request) - REQUEST_KEYS
    if unknown:
        raise ResolverError(
            f"unsupported request keys: {sorted(unknown)}", "invalid-request"
        )
    for key in ("id", "base", "manager"):
        if key not in request:
            raise ResolverError(f"missing request key: {key}", "invalid-request")
    if not isinstance(request["id"], str) or not ID_RE.match(request["id"]):
        raise ResolverError("id must match " + ID_RE.pattern, "invalid-request")
    if not isinstance(request["base"], str) or not BASE_RE.match(request["base"]):
        raise ResolverError(
            "base must be pinned as <image>@sha256:<64 hex>", "invalid-base"
        )
    prepared = request.get("prepared_image")
    if prepared is not None and (
        not isinstance(prepared, dict)
        or set(prepared) != {"id", "base", "apt_request_sha256"}
        or not isinstance(prepared["id"], str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", prepared["id"])
        or prepared["base"] != request["base"]
        or not isinstance(prepared["apt_request_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", prepared["apt_request_sha256"])
    ):
        raise ResolverError(
            "prepared_image requires immutable image ID, exact original base and APT request SHA256",
            "invalid-prepared-image",
        )
    if request.get("platform", "linux/amd64") != "linux/amd64":
        raise ResolverError("only linux/amd64 bases are supported", "base-platform")
    manager = request["manager"]
    if not isinstance(manager, dict) or set(manager) != {"name", "version"}:
        raise ResolverError("manager must be {name, version}", "invalid-request")
    if manager["name"] != "npm":
        raise ResolverError(
            f"manager {manager['name']!r} is not supported: only npm. pnpm's store index and "
            "yarn berry's zip checksums are not tarball-byte inventories, and no official "
            "documented cache schema is consumed here",
            "unsupported-manager",
        )
    if not isinstance(manager["version"], str) or not SEMVER_RE.match(
        manager["version"]
    ):
        raise ResolverError(
            "manager.version must be an exact version", "invalid-request"
        )
    command = request.get("command", "ci")
    if command not in ("ci", "install"):
        raise ResolverError(
            "only `npm ci` (with a lock) and lockless `npm install` are supported",
            "unsupported-command",
        )
    normal = dict(request)
    normal["platform"] = "linux/amd64"
    normal["command"] = command
    normal["flags"] = request.get("flags", [])
    normal["_flags"] = parse_flags(normal["flags"])
    env = request.get("env", {})
    if (
        not isinstance(env, dict)
        or set(env) - {"NODE_ENV"}
        or not all(
            isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", v)
            for v in env.values()
        )
    ):
        raise ResolverError(
            "env may only set NODE_ENV to a simple value", "invalid-request"
        )
    normal["env"] = env
    if not isinstance(request.get("replay", True), bool):
        raise ResolverError("replay must be a boolean", "invalid-request")
    normal["replay"] = request.get("replay", True)
    if not isinstance(request.get("capture_cache", False), bool):
        raise ResolverError("capture_cache must be a boolean", "invalid-request")
    normal["capture_cache"] = request.get("capture_cache", False)
    if normal["capture_cache"] and not normal["replay"]:
        raise ResolverError("cache capture requires offline replay", "invalid-request")
    timeout = request.get("timeout_seconds", DEFAULT_TIMEOUT)
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 0 < timeout <= MAX_TIMEOUT
    ):
        raise ResolverError(
            f"timeout_seconds must be in 1..{MAX_TIMEOUT}", "invalid-request"
        )
    normal["timeout_seconds"] = timeout
    limits = dict(DEFAULT_LIMITS)
    given = request.get("limits", {})
    if not isinstance(given, dict) or set(given) - set(DEFAULT_LIMITS):
        raise ResolverError(
            "limits must be an object of known limit names", "invalid-request"
        )
    for key, value in given.items():
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 < value <= HARD_LIMITS[key]
        ):
            raise ResolverError(
                f"limit {key} must be in 1..{HARD_LIMITS[key]}", "invalid-request"
            )
        limits[key] = value
    normal["limits"] = limits
    has_inline, has_path = "files" in request, "source_path" in request
    if has_inline == has_path:
        raise ResolverError(
            "give exactly one of files (inline) or source_path (remote read-only dir)",
            "invalid-request",
        )
    if has_inline:
        files = request["files"]
        if "project_dir" in request:
            raise ResolverError(
                "project_dir only goes with source_path", "invalid-request"
            )
        if command == "install":
            if isinstance(files, dict) and "package_lock" in files:
                raise ResolverError(
                    "`npm install` with an existing lock is not supported (it may rewrite "
                    "the lock); only a lockless project is accepted",
                    "unsupported-command",
                )
            if (
                not isinstance(files, dict)
                or set(files) != {"package_json"}
                or not isinstance(files["package_json"], str)
            ):
                raise ResolverError(
                    "files must be {package_json} text for lockless npm install",
                    "invalid-request",
                )
        elif (
            not isinstance(files, dict)
            or set(files) - {"package_json", "package_lock"}
            or not all(
                isinstance(files.get(k), str) for k in ("package_json", "package_lock")
            )
        ):
            raise ResolverError(
                "files must be {package_json, package_lock} text", "invalid-request"
            )
    else:
        source = request["source_path"]
        if (
            not isinstance(source, str)
            or not source.startswith("/")
            or ".." in Path(source).parts
            or source == "/"
        ):
            raise ResolverError(
                "source_path must be an explicit absolute directory", "invalid-request"
            )
        normal["project_dir"] = _check_relative_dir(request.get("project_dir", "."))
    return normal


def load_sources(request: dict) -> dict:
    """Exact package.json/lock bytes. ``source_path`` is read, never written."""
    limits = request["limits"]
    if "files" in request:
        found = {
            "package_json": request["files"]["package_json"].encode(),
            "package_lock": request["files"]["package_lock"].encode()
            if "package_lock" in request["files"]
            else None,
            "lock_name": "package-lock.json",
            "origin": "inline",
        }
    else:
        root = Path(request["source_path"])
        if not root.is_dir() or root.is_symlink():
            raise ResolverError(
                "source_path is not a real directory on this host", "source-missing"
            )
        base = root / request["project_dir"]
        for name in (".npmrc", ".yarnrc", ".yarnrc.yml"):
            if os.path.lexists(base / name):
                raise ResolverError(
                    f"{name} present: project registry/auth configuration is not reproduced",
                    "project-config",
                )
        if os.path.lexists(base / "pnpm-workspace.yaml"):
            raise ResolverError(
                "pnpm-workspace.yaml present: workspaces unsupported", "workspaces"
            )
        lock_name = None
        for candidate in (
            "npm-shrinkwrap.json",
            "package-lock.json",
        ):  # npm prefers shrinkwrap
            if os.path.lexists(base / candidate):
                lock_name = candidate
                break
        install = request["command"] == "install"
        if install and lock_name is not None:
            raise ResolverError(
                f"{lock_name} present: `npm install` with an existing lock is not supported",
                "unsupported-command",
            )
        if lock_name is None and not install:
            raise ResolverError(
                "no package-lock.json or npm-shrinkwrap.json", "source-missing"
            )
        found = {
            "lock_name": lock_name or "package-lock.json",
            "origin": "source_path",
            "package_lock": None,
        }
        for key, name in (
            ("package_json", "package.json"),
            *(() if install else (("package_lock", lock_name),)),
        ):
            file = base / name
            if file.is_symlink() or not file.is_file():
                raise ResolverError(f"{name} is not a regular file", "source-missing")
            if file.stat().st_size > limits["max_lock_bytes"]:
                raise ResolverError(
                    f"{name} exceeds {limits['max_lock_bytes']} bytes", "limit-exceeded"
                )
            found[key] = file.read_bytes()
    for key in ("package_json", "package_lock"):
        if found[key] is not None and len(found[key]) > limits["max_lock_bytes"]:
            raise ResolverError(f"{key} too large", "limit-exceeded")
    return found


def source_digests(sources: dict) -> dict:
    digests = {"package.json": sha256_hex(sources["package_json"])}
    if sources["package_lock"] is not None:  # lockless install: nothing to hash
        digests[sources["lock_name"]] = sha256_hex(sources["package_lock"])
    return digests


def request_key(request: dict) -> str:
    public = {k: v for k, v in request.items() if not k.startswith("_")}
    if public.get("capture_cache") is False:
        public.pop("capture_cache")
    return sha256_hex(json.dumps(public, sort_keys=True).encode())[:16]


# --------------------------------------------------------------------------
# Integrity and URLs (pure)
# --------------------------------------------------------------------------
def parse_integrity(value: object) -> dict[str, set[str]]:
    """Parse an SRI string into {algorithm: {base64 digests}}; reject weak-only."""
    if not isinstance(value, str) or not value.strip():
        raise ResolverError("integrity missing", "bad-integrity")
    algos: dict[str, set[str]] = {}
    for token in value.split():
        match = SRI_RE.match(token)
        if not match:
            raise ResolverError("unsupported integrity token", "bad-integrity")
        try:
            raw = base64.b64decode(match[2], validate=True)
        except ValueError:
            raise ResolverError(
                "integrity is not valid base64", "bad-integrity"
            ) from None
        if len(raw) != DIGEST_SIZE[match[1]]:
            raise ResolverError(
                "integrity digest has the wrong length", "bad-integrity"
            )
        algos.setdefault(match[1], set()).add(match[2])
    if not any(a in algos for a in STRONG):
        raise ResolverError(
            "sha1-only integrity is rejected (need sha256/384/512)", "weak-integrity"
        )
    return algos


def digest_file(path: Path) -> dict:
    """Hash a file's bytes: {sha1,sha256,sha384,sha512: raw digest, size}."""
    hashes = {name: hashlib.new(name) for name in DIGEST_SIZE}
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            size += len(chunk)
            for h in hashes.values():
                h.update(chunk)
    return {**{n: h.digest() for n, h in hashes.items()}, "size": size}


def verify_digests(integrity: str, digests: dict) -> None:
    """Every algorithm named by ``integrity`` must match the file's bytes."""
    for algo, expected in parse_integrity(integrity).items():
        actual = base64.b64encode(digests[algo]).decode()
        if actual not in expected:
            raise ResolverError(f"{algo} integrity mismatch", "integrity-mismatch")


def check_tarball_url(url: object) -> str:
    if not isinstance(url, str):
        raise ResolverError("resolved URL missing", "unsupported-source")
    if url.startswith(("git+", "git:", "file:", "link:", "workspace:", "npm:")):
        raise ResolverError(
            "Git/file/link/alias sources are not supported", "unsupported-source"
        )
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ResolverError("resolved URL must be https", "insecure-url")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ResolverError("resolved URL contains credentials", "credentialed-url")
    if parts.query or parts.fragment:
        raise ResolverError(
            "resolved URL has a query/fragment (signed or mutable)", "credentialed-url"
        )
    if parts.hostname in GIT_HOSTS:
        raise ResolverError(
            "Git-host archives are not registry tarballs", "unsupported-source"
        )
    if not parts.path.endswith((".tgz", ".tar.gz")):
        raise ResolverError(
            "resolved URL is not a registry tarball", "unsupported-source"
        )
    return url


# --------------------------------------------------------------------------
# Lock analysis (pure)
# --------------------------------------------------------------------------
def package_name(key: str, entry: dict) -> str:
    return entry.get("name") or key.rsplit("node_modules/", 1)[-1]


def _json_object(data: bytes, what: str) -> dict:
    try:
        value = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ResolverError(f"{what} is not valid JSON", "invalid-lock") from None
    if not isinstance(value, dict):
        raise ResolverError(f"{what} must be a JSON object", "invalid-lock")
    return value


def check_manifest(sources: dict) -> dict:
    manifest = _json_object(sources["package_json"], "package.json")
    if manifest.get("workspaces"):
        raise ResolverError(
            "package.json declares workspaces: unsupported", "workspaces"
        )
    return manifest


DEP_FIELDS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)


def check_lock_root(manifest: dict, lock: dict) -> None:
    """A generated lock's root entry must record exactly the manifest's dependency maps."""
    root = lock["packages"][""]
    for field in DEP_FIELDS:
        if (root.get(field) or {}) != (manifest.get(field) or {}):
            raise ResolverError(
                f"generated lock root {field} differs from package.json", "lock-root"
            )


def analyze_lock(sources: dict, limits: dict) -> dict:
    """Reject unsupported lock content; return entries, bundled keys and unresolved notes."""
    manifest = check_manifest(sources)
    lock = _json_object(sources["package_lock"], sources["lock_name"])
    if lock.get("lockfileVersion") not in (2, 3):
        raise ResolverError("lockfileVersion must be 2 or 3 (npm >= 7)", "lock-version")
    packages = lock.get("packages")
    if not isinstance(packages, dict) or not isinstance(packages.get(""), dict):
        raise ResolverError("lock has no root packages entry", "invalid-lock")
    if packages[""].get("workspaces"):
        raise ResolverError("lock root declares workspaces: unsupported", "workspaces")
    if sources.get("generated"):
        check_lock_root(manifest, lock)
    entries: dict[str, dict] = {}
    bundled: list[str] = []
    lifecycle: list[dict] = []
    for key, entry in packages.items():
        if key == "":
            continue
        if not isinstance(entry, dict) or "node_modules/" not in key:
            raise ResolverError(
                "lock entry outside node_modules (workspace or local package): unsupported",
                "workspaces",
            )
        if entry.get("link"):
            raise ResolverError(
                f"{key}: link/workspace entries are unsupported", "workspaces"
            )
        if not isinstance(entry.get("version"), str):
            raise ResolverError(f"{key}: lock entry has no version", "invalid-lock")
        if entry.get("inBundle"):
            bundled.append(key)
            continue
        check_tarball_url(entry.get("resolved"))
        parse_integrity(entry.get("integrity"))
        entries[key] = entry
        if entry.get("hasInstallScript"):
            lifecycle.append(
                {
                    "key": key,
                    "name": package_name(key, entry),
                    "version": entry["version"],
                }
            )
    if len(entries) > limits["max_packages"]:
        raise ResolverError(
            f"more than {limits['max_packages']} packages", "limit-exceeded"
        )
    scripts = (
        manifest.get("scripts") if isinstance(manifest.get("scripts"), dict) else {}
    )
    return {
        "entries": entries,
        "bundled": bundled,
        "lifecycle_packages": lifecycle,
        "root_lifecycle": sorted(s for s in ROOT_LIFECYCLE if s in scripts),
        "lock_version": lock["lockfileVersion"],
    }


def compare_installed(
    entries: dict[str, dict], all_keys: set[str], hidden: object, omit_dev: bool
) -> dict:
    """Check npm's hidden lockfile against the project lock; return the installed set."""
    if not isinstance(hidden, dict) or not isinstance(hidden.get("packages"), dict):
        raise ResolverError(
            "node_modules/.package-lock.json missing or malformed", "hidden-lock"
        )
    installed: dict[str, dict] = {}
    for key, entry in hidden["packages"].items():
        if key == "":
            continue
        if not isinstance(entry, dict) or entry.get("link"):
            raise ResolverError(
                f"{key}: installed link entries are unsupported", "workspaces"
            )
        want = entries.get(key)
        if want is None:
            if key in all_keys:  # bundled, shipped inside its parent tarball
                continue
            raise ResolverError(
                f"npm installed {key}, absent from the project lock", "lock-drift"
            )
        for field in ("version", "resolved", "integrity"):
            if entry.get(field) != want.get(field):
                raise ResolverError(
                    f"{key}: installed {field} differs from the lock", "lock-drift"
                )
        if omit_dev and want.get("dev"):
            raise ResolverError(
                f"{key}: dev package installed despite --omit=dev", "lock-drift"
            )
        installed[key] = want
    if not installed and entries:
        raise ResolverError(
            "npm installed nothing from a non-empty lock", "hidden-lock"
        )
    return installed


def skipped_report(entries: dict[str, dict], installed: dict[str, dict]) -> dict:
    skipped = {k: e for k, e in entries.items() if k not in installed}
    return {
        "count": len(skipped),
        "platform_restricted": sorted(
            f"{package_name(k, e)}@{e['version']}"
            for k, e in skipped.items()
            if e.get("os") or e.get("cpu")
        ),
        "dev_only": sum(1 for e in skipped.values() if e.get("dev")),
        "optional": sum(1 for e in skipped.values() if e.get("optional")),
    }


def tarball_path(prefix: str, name: str, version: str, sha256: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._+-]", "_", f"{name}-{version}")
    return f"{prefix}/{safe}-{sha256[:12]}.tgz"


# --------------------------------------------------------------------------
# npm failure classification (pure)
# --------------------------------------------------------------------------
_CLASSES = [
    (
        "lock-out-of-sync",
        ("can only install packages when your package.json and package-lock", "EUSAGE"),
    ),
    ("integrity-mismatch", ("EINTEGRITY",)),
    ("peer-conflict", ("ERESOLVE",)),
    ("platform", ("EBADPLATFORM",)),
    ("package-unavailable", ("E404", "ETARGET", "E403", "E401")),
    ("offline-cache-miss", ("ENOTCACHED",)),
    (
        "network",
        (
            "ENOTFOUND",
            "ETIMEDOUT",
            "ECONNRESET",
            "ECONNREFUSED",
            "EAI_AGAIN",
            "FETCH_ERROR",
        ),
    ),
]
_REASONS = {
    "lock-out-of-sync": "package.json and the lock disagree (npm ci refuses to update the lock)",
    "integrity-mismatch": "npm reported an integrity failure",
    "peer-conflict": "peer dependency conflict under the given flags",
    "platform": "package not installable on linux/amd64",
    "package-unavailable": "package not served by the registry (private, removed or unauthorised)",
    "offline-cache-miss": "offline install needed data absent from the cache",
    "network": "network error reaching the registry",
}


def classify_npm_failure(text: str) -> tuple[str, str]:
    for code, needles in _CLASSES:
        if any(n in text for n in needles):
            return code, _REASONS[code]
    return "npm-failed", "npm failed"


# --------------------------------------------------------------------------
# Remote execution (Docker and the network, remote host only)
# --------------------------------------------------------------------------
def run(
    command: list[str], check: bool = True, timeout: int | None = None, **kwargs
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        raise ResolverError(
            f"{' '.join(command[:3])} timed out after {timeout}s", "timeout"
        ) from None
    if check and result.returncode != 0:
        tail = redact(((result.stderr or "") + (result.stdout or ""))[-2000:])
        code = classify_npm_failure(tail)[0] if "npm" in command else "docker"
        raise ResolverError(
            f"{' '.join(command[:3])} failed ({result.returncode}): {tail}", code
        )
    return result


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise ResolverError(
                "download redirected away from https", "insecure-redirect"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpsOnly)


def download(url: str, destination: Path, max_bytes: int) -> None:
    """Fetch an https ``url`` to ``destination``; abort past ``max_bytes``. Hashing is separate."""
    size = 0
    request = urllib.request.Request(
        url, headers={"User-Agent": "mango-remote-node-deps"}
    )
    try:
        with (
            _OPENER.open(request, timeout=120) as response,
            destination.open("wb") as out,
        ):
            if urllib.parse.urlsplit(response.geturl()).scheme != "https":
                raise ResolverError(
                    "download redirected away from https", "insecure-redirect"
                )
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ResolverError(
                    f"download exceeds {max_bytes} bytes", "limit-exceeded"
                )
            for chunk in iter(lambda: response.read(1 << 20), b""):
                size += len(chunk)
                if size > max_bytes:
                    raise ResolverError(
                        f"download exceeds {max_bytes} bytes", "limit-exceeded"
                    )
                out.write(chunk)
    except urllib.error.HTTPError as exc:
        destination.unlink(missing_ok=True)
        raise ResolverError(
            f"HTTP {exc.code} fetching {redact(url)}", "download-failed"
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        destination.unlink(missing_ok=True)
        raise ResolverError(
            f"fetching {redact(url)} failed: {redact(str(exc))}", "download-failed"
        ) from None
    except ResolverError:
        destination.unlink(missing_ok=True)
        raise


class Container:
    """A disposable root container on the pinned linux/amd64 base (1.5 CPU, 4 GiB)."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.name = f"mango-nodedeps-{uuid.uuid4().hex[:12]}"

    def __enter__(self) -> Self:
        require_remote_author()
        run(
            [
                "docker", "run", "-d", "--rm",
                "--platform", "linux/amd64",
                "--cpus", CONTAINER_LIMITS["cpus"],
                "--memory", CONTAINER_LIMITS["memory"],
                "--memory-swap", CONTAINER_LIMITS["memory"],
                "--pids-limit", CONTAINER_LIMITS["pids"],
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges",
                "--name", self.name,
                "--user", "0:0",
                "--label", "mango.remote-node-deps=1",
                "--entrypoint", "sleep",
                self.base, "infinity",
            ]
        )  # fmt: skip
        return self

    def __exit__(self, *exc) -> None:
        subprocess.run(
            ["docker", "rm", "-f", self.name],
            capture_output=True,
            text=True,
            check=False,
        )

    def exec(
        self,
        argv: list[str],
        env: dict[str, str] | None = None,
        workdir: str | None = None,
        check: bool = True,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        command = ["docker", "exec"]
        for key, value in (env or {}).items():
            command += ["-e", f"{key}={value}"]
        if workdir:
            command += ["-w", workdir]
        return run([*command, self.name, *argv], check=check, timeout=timeout)

    def copy_in(self, source: Path, destination: str) -> None:
        run(["docker", "cp", str(source), f"{self.name}:{destination}"])

    def copy_out(self, source: str, destination: Path) -> None:
        run(["docker", "cp", f"{self.name}:{source}", str(destination)])

    def image_id(self) -> str:
        return run(
            ["docker", "inspect", "--format", "{{.Image}}", self.name]
        ).stdout.strip()

    def image_platform(self) -> str:
        out = run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Os}}/{{.Architecture}}",
                self.base,
            ]
        )
        return out.stdout.strip()

    def disconnect_networks(self) -> list[str]:
        names = run(
            [
                "docker",
                "inspect",
                "--format",
                "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}",
                self.name,
            ]
        ).stdout.split()
        for network in names:
            run(["docker", "network", "disconnect", network, self.name])
        return names


NODE_SHA = (
    "const c=require('crypto'),f=require('fs');"
    "for(const p of process.argv.slice(1))"
    "console.log(c.createHash('sha256').update(f.readFileSync(p)).digest('hex')+'  '+p)"
)
NODE_CAT = "process.stdout.write(require('fs').readFileSync(process.argv[1]))"


def npm_env(request: dict) -> dict[str, str]:
    """Hermetic npm config: no ambient npmrc, public registry, no update checks."""
    env = {
        "npm_config_userconfig": "/nonexistent/mango-npmrc",
        "npm_config_globalconfig": "/nonexistent/mango-global-npmrc",
        "npm_config_registry": REGISTRY,
        "npm_config_update_notifier": "false",
    }
    env.update(request.get("env", {}))
    return env


def install_argv(
    request: dict, cache: str, offline: bool, force_ignore_scripts: bool
) -> list[str]:
    flags = list(request["_flags"]["argv"])
    command = ["npm", request["command"], "--cache", cache]
    if force_ignore_scripts and "--ignore-scripts" not in flags:
        command.append("--ignore-scripts")
    if offline:
        command.append("--offline")
    if "--no-audit" not in flags:
        command.append(
            "--no-audit"
        )  # audit only talks to the registry; no effect on the tree
    if "--no-fund" not in flags:
        command.append("--no-fund")
    return command + flags


def container_hashes(container, paths: list[str]) -> dict[str, str]:
    out = container.exec(["node", "-e", NODE_SHA, *paths]).stdout
    found = {}
    for line in out.splitlines():
        digest, _, path = line.partition("  ")
        found[path] = digest
    if set(found) != set(paths):
        raise ResolverError("could not hash project files in the container", "docker")
    return found


def read_hidden_lock(container, limit: int) -> dict:
    out = container.exec(
        ["node", "-e", NODE_CAT, f"{PROJECT}/node_modules/.package-lock.json"],
        check=False,
    )
    if out.returncode != 0:
        raise ResolverError(
            "node_modules/.package-lock.json not written by npm", "hidden-lock"
        )
    if len(out.stdout) > limit:
        raise ResolverError("hidden lockfile too large", "limit-exceeded")
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        raise ResolverError("hidden lockfile is not JSON", "hidden-lock") from None


NODE_B64 = "process.stdout.write(require('fs').readFileSync(process.argv[1]).toString('base64'))"


def read_container_bytes(container, path: str, limit: int, code: str) -> bytes:
    """Exact bytes of a container file (base64 on the wire, so no newline/encoding change)."""
    out = container.exec(["node", "-e", NODE_B64, path], check=False)
    if out.returncode != 0:
        raise ResolverError(f"{path.rsplit('/', 1)[-1]} is missing", code)
    try:
        data = base64.b64decode(out.stdout.strip(), validate=True)
    except ValueError:
        raise ResolverError(f"{path} could not be read back", code) from None
    if len(data) > limit:
        raise ResolverError(f"{path.rsplit('/', 1)[-1]} too large", "limit-exceeded")
    return data


def check_project_unchanged(
    container, lock_name: str, expected: dict[str, str], when: str
) -> None:
    now = container_hashes(container, list(expected))
    for path, digest in now.items():
        if digest != expected[path]:
            raise ResolverError(
                f"{path.rsplit('/', 1)[-1]} was modified {when}", "lock-mutated"
            )


def download_inventory(installed, prefix, work, fetch, limits) -> list[dict]:
    """Download each distinct tarball and verify its bytes against the lock integrity."""
    by_url: dict[str, dict] = {}
    total = 0
    for key in sorted(installed):
        entry = installed[key]
        url, integrity = entry["resolved"], entry["integrity"]
        known = by_url.get(url)
        if known is not None:
            if known["integrity"] != integrity:
                raise ResolverError(
                    "one tarball URL has conflicting integrity values", "lock-drift"
                )
            known["lock_keys"].append(key)
            continue
        destination = work / f"{len(by_url)}.tgz"
        fetch(url, destination, limits["max_tarball_bytes"])
        digests = digest_file(destination)
        if digests["size"] > limits["max_tarball_bytes"]:
            raise ResolverError("tarball exceeds the size limit", "limit-exceeded")
        total += digests["size"]
        if total > limits["max_total_bytes"]:
            raise ResolverError("total download size limit exceeded", "limit-exceeded")
        verify_digests(integrity, digests)
        sha = digests["sha256"].hex()
        name = package_name(key, entry)
        by_url[url] = {
            "name": name,
            "version": entry["version"],
            "url": url,
            "integrity": integrity,
            "sha256": sha,
            "sha512": digests["sha512"].hex(),
            "size": digests["size"],
            "path": tarball_path(prefix, name, entry["version"], sha),
            "lock_keys": [key],
            "local_file": destination,
        }
    return list(by_url.values())


def replay_offline(
    container,
    request,
    inventory,
    work,
    installed_before: dict,
    generated_lock: bytes | None = None,
) -> dict:
    """Disconnect the network, rebuild a fresh cache from verified bytes, install offline."""
    networks = container.disconnect_networks()
    container.exec(["mkdir", "-p", REPLAY_CACHE, TARBALLS])
    for index, item in enumerate(inventory):
        local = work / "replay" / f"{index}.tgz"
        local.parent.mkdir(exist_ok=True)
        local.write_bytes(item["local_file"].read_bytes())
        container.copy_in(local, f"{TARBALLS}/{index}.tgz")
    env = npm_env(request)
    timeout = request["timeout_seconds"]
    files = [f"{TARBALLS}/{i}.tgz" for i in range(len(inventory))]
    for start in range(0, len(files), 100):
        container.exec(
            [
                "npm",
                "cache",
                "add",
                "--cache",
                REPLAY_CACHE,
                *files[start : start + 100],
            ],
            env=env,
            workdir=PROJECT,
            timeout=timeout,
        )
    if generated_lock is not None:  # `npm install` would no-op over a populated tree
        container.exec(["rm", "-rf", f"{PROJECT}/node_modules"])
    container.exec(
        install_argv(request, REPLAY_CACHE, offline=True, force_ignore_scripts=True),
        env=env,
        workdir=PROJECT,
        timeout=timeout,
    )
    if generated_lock is not None:
        try:
            replayed = read_container_bytes(
                container,
                f"{PROJECT}/package-lock.json",
                request["limits"]["max_lock_bytes"],
                "replay-mismatch",
            )
        except ResolverError as exc:
            raise ResolverError(
                f"generated lock lost during offline replay: {exc}", "replay-mismatch"
            ) from None
        if replayed != generated_lock:
            raise ResolverError(
                "offline replay changed the generated lock bytes", "replay-mismatch"
            )
    again = read_hidden_lock(container, request["limits"]["max_hidden_lock_bytes"])
    if again.get("packages") != installed_before:
        raise ResolverError(
            "offline replay installed a different tree", "replay-mismatch"
        )
    return {"networks_disconnected": networks}


def resolve(
    request: dict,
    fetch: Callable[[str, Path, int], None] = download,
    container_factory: Callable[[str], object] = Container,
    cache_directory: Path | None = None,
) -> dict:
    require_remote_author()
    request = validate_request(request)
    if request["capture_cache"] and cache_directory is None:
        raise ResolverError(
            "cache capture needs an output directory", "invalid-request"
        )
    sources = load_sources(request)
    install_mode = request["command"] == "install"
    if install_mode:
        check_manifest(sources)  # the lock does not exist yet; analysed once generated
        analysis = None
    else:
        analysis = analyze_lock(sources, request["limits"])
    lock_name = sources["lock_name"]
    digests_before = source_digests(sources)
    env = npm_env(request)
    timeout = request["timeout_seconds"]
    prefix = f"npm-tarballs/{request_key(request)}"
    cache_archive = None
    with (
        container_factory(
            (request.get("prepared_image") or {}).get("id", request["base"])
        ) as container,
        tempfile.TemporaryDirectory() as tmp,
    ):
        work = Path(tmp)
        prepared = request.get("prepared_image")
        if prepared is not None and container.image_id() != prepared["id"]:
            raise ResolverError(
                "Container does not run declared immutable prepared image",
                "prepared-image-mismatch",
            )
        platform = container.image_platform()
        if platform != "linux/amd64":
            raise ResolverError(
                f"base image is {platform}, not linux/amd64", "base-platform"
            )
        node_platform = container.exec(
            ["node", "-p", "process.platform+'/'+process.arch"]
        ).stdout.strip()
        if node_platform != "linux/x64":
            raise ResolverError(f"node reports {node_platform}", "base-platform")
        npm_version = container.exec(["npm", "--version"], env=env).stdout.strip()
        if npm_version != request["manager"]["version"]:
            raise ResolverError(
                f"base npm is {npm_version}, request pins {request['manager']['version']}: "
                "the base's own manager is never upgraded",
                "manager-version-mismatch",
            )
        node_version = container.exec(["node", "--version"]).stdout.strip()
        registry = container.exec(
            ["npm", "config", "get", "registry"], env=env
        ).stdout.strip()
        if registry != REGISTRY:
            raise ResolverError(
                "npm registry config is not the public default", "registry-config"
            )
        container.exec(["mkdir", "-p", PROJECT, CACHE])
        for key, name in (
            ("package_json", "package.json"),
            *(() if install_mode else (("package_lock", lock_name),)),
        ):
            local = work / name
            local.write_bytes(sources[key])
            local.chmod(0o600)
            container.copy_in(local, f"{PROJECT}/{name}")
        expected = {
            f"{PROJECT}/{name}": digest for name, digest in digests_before.items()
        }
        check_project_unchanged(container, lock_name, expected, "when copied in")
        if install_mode:
            PROGRESS(f"{request['id']}: npm install (generating the lock)")
        else:
            PROGRESS(
                f"{request['id']}: npm ci ({len(analysis['entries'])} lock packages)"
            )
        container.exec(
            install_argv(request, CACHE, offline=False, force_ignore_scripts=True),
            env=env,
            workdir=PROJECT,
            timeout=timeout,
        )
        check_project_unchanged(
            container, lock_name, expected, f"by npm {request['command']}"
        )
        generated_lock = None
        if install_mode:
            generated_lock = read_container_bytes(
                container,
                f"{PROJECT}/package-lock.json",
                request["limits"]["max_lock_bytes"],
                "lock-not-generated",
            )
            analysis = analyze_lock(
                {
                    "package_json": sources["package_json"],
                    "package_lock": generated_lock,
                    "lock_name": "package-lock.json",
                    "generated": True,
                },
                request["limits"],
            )
            PROGRESS(
                f"{request['id']}: generated lock has {len(analysis['entries'])} packages"
            )
        hidden = read_hidden_lock(container, request["limits"]["max_hidden_lock_bytes"])
        omit_dev = (
            "dev" in request["_flags"]["omit"]
            or request.get("env", {}).get("NODE_ENV") == "production"
        )
        if "dev" in request["_flags"]["include"]:
            omit_dev = False
        all_keys = set(analysis["entries"]) | set(analysis["bundled"])
        installed = compare_installed(analysis["entries"], all_keys, hidden, omit_dev)
        (work / "dl").mkdir()
        inventory = download_inventory(
            installed, prefix, work / "dl", fetch, request["limits"]
        )
        replay: dict = {"ran": False}
        replay_error = None
        if request["replay"]:
            PROGRESS(f"{request['id']}: offline replay ({len(inventory)} tarballs)")
            try:
                replay = {
                    "ran": True,
                    **replay_offline(
                        container,
                        request,
                        inventory,
                        work,
                        hidden["packages"],
                        generated_lock,
                    ),
                }
            except ResolverError as exc:
                replay = {"ran": True}
                replay_error = {"reason_code": exc.code, "error": redact(str(exc))}
            check_project_unchanged(
                container, lock_name, expected, "during offline replay"
            )
        if request["capture_cache"]:
            if replay_error:
                raise ResolverError("Cannot capture unverified cache", "not-verified")
            from .npm_cache import capture

            cache_archive = capture(
                container,
                request,
                work,
                cache_directory,
                hidden["packages"],
                generated_lock,
            )
            check_project_unchanged(
                container, lock_name, expected, "during lifecycle replay"
            )
    try:
        source_changed = (
            "source_path" in request
            and source_digests(load_sources(request)) != digests_before
        )
    except ResolverError:  # e.g. a lock appeared in a lockless source project
        source_changed = True
    if source_changed:
        raise ResolverError(
            "the read-only source project changed during resolution", "lock-mutated"
        )
    result = build_result(
        request,
        sources,
        analysis,
        installed,
        inventory,
        prefix,
        replay,
        replay_error,
        generated_lock,
        {
            "node": node_version,
            "npm": npm_version,
            "image_platform": platform,
            "node_platform": node_platform,
            "registry": registry,
            "runtime_image": (request.get("prepared_image") or {}).get(
                "id", request["base"]
            ),
        },
    )
    if cache_archive is not None:
        result["cache_archive"] = cache_archive
        result["unresolved"] = [
            item
            for item in result["unresolved"]
            if item["kind"] != "lifecycle-scripts-not-run-in-fetch"
        ]
        result["evidence"]["original_lifecycle_replayed_offline"] = True
        result["offline"]["limitations"] = [
            "Manager-created cache archive was replayed with lifecycle scripts offline; "
            "this does not qualify subsequent application build commands."
        ]
    return result


def build_result(
    request,
    sources,
    analysis,
    installed,
    inventory,
    prefix,
    replay,
    replay_error,
    generated_lock,
    tools,
) -> dict:
    verified = bool(replay.get("ran")) and replay_error is None
    flags = list(request["_flags"]["argv"])
    command = request["command"]
    unresolved = [
        {
            "kind": "lifecycle-scripts-not-run-in-fetch",
            "detail": "install-time scripts ran disabled; downloads they perform (binaries, "
            "prebuilt addons, browsers) are NOT captured and must be pinned separately",
            "packages": analysis["lifecycle_packages"],
            "root_scripts": analysis["root_lifecycle"],
        },
        {
            "kind": "non-registry-network",
            "detail": "only registry tarballs reachable through the lock are inventoried; "
            "anything else a build fetches (git, node-gyp headers, corepack, postinstall) is out of scope",
        },
    ]
    if generated_lock is not None:
        unresolved.append(
            {
                "kind": "original-build-not-run",
                "detail": "original build lifecycle commands (e.g. `npm run build`) were not run "
                "and are untouched; they must run offline in Mango. Nothing here qualifies the build",
            }
        )
    lock = {
        "schema_version": 1,
        "artifacts": [
            {"path": i["path"], "urls": [i["url"]], "sha256": i["sha256"]}
            for i in inventory
        ],
    }
    generated = None
    if generated_lock is not None:
        generated = {
            "name": "package-lock.json",
            "origin": "generated by the original `npm install` on the pinned base (intentional artifact; "
            "the input had no lock and package.json is unchanged)",
            "sha256": sha256_hex(generated_lock),
            "size": len(generated_lock),
            "lock_version": analysis["lock_version"],
            "content_base64": base64.b64encode(generated_lock).decode(),
            "consumption": "the translated build stage writes the ORIGINAL package.json plus these exact "
            "bytes as package-lock.json, then runs the original `npm install --offline` (scripts enabled) "
            "with a cache filled from `lock`; npm reads the lock, so the selected tree equals the captured "
            "network install. Reject the build if the lock sha256 or package.json changes.",
        }
    result = {
        "id": request["id"],
        "lock": lock,
        "inventory": [
            {k: v for k, v in item.items() if k != "local_file"} for item in inventory
        ],
        "offline": {
            "tarball_prefix": prefix,
            "cache_population": {
                "command": [
                    "npm",
                    "cache",
                    "add",
                    "--cache",
                    "$NPM_CACHE_DIR",
                    "<tarball file>...",
                ],
                "note": "run on the verified tarball files from `lock`; the cache dir must be fresh, writable, private",
            },
            "install": {
                "command": [
                    "npm",
                    command,
                    "--cache",
                    "$NPM_CACHE_DIR",
                    "--offline",
                    *flags,
                ],
                "env": dict(request.get("env", {})),
                "note": "original flags only: --ignore-scripts is NOT added, so lifecycle scripts run in the offline build",
            },
            "verified_by_replay": verified,
            "replay_error": replay_error,
            "replay": replay,
            "verified_by_offline_build": False,
            "limitations": [
                (
                    "cache population by `npm cache add <file>` is not described by npm's documentation; "
                    "it is proven only by the replay on the remote host"
                ),
                "the manager cache layout (_cacache) is opaque and is never copied or synthesised",
            ],
        },
        "unresolved": unresolved,
        "package_request": {
            "base": request["base"],
            "prepared_image": request.get("prepared_image"),
            "manager": request["manager"],
            "command": command,
            "flags": request["flags"],
            "env": request.get("env", {}),
            "project_dir": request.get("project_dir", "."),
        },
        "evidence": {
            "resolver_version": RESOLVER_VERSION,
            "tools": tools,
            "source_sha256": source_digests(sources),
            "lock_version": analysis["lock_version"],
            **(
                {"lock_origin": "generated", "package_json_unchanged": True}
                if generated_lock is not None
                else {"lock_origin": "source", "lock_unchanged": True}
            ),
            "lock_packages": len(analysis["entries"]),
            "installed_packages": len(installed),
            "bundled_packages": len(analysis["bundled"]),
            "skipped": skipped_report(analysis["entries"], installed),
            "tarballs": len(inventory),
            "total_bytes": sum(i["size"] for i in inventory),
            "container_limits": CONTAINER_LIMITS,
            "note": "authoring evidence only; runtime builds consume the lock offline",
        },
        "verified_by_replay": verified,
    }
    if generated is not None:
        result["generated_lock"] = generated
    return result


def require_verified(result: object) -> dict:
    """Strict gate for consumers: only a completed result whose offline replay passed."""
    if not isinstance(result, dict) or result.get("status") != "completed":
        raise ResolverError("result is not completed", "not-verified")
    offline = result.get("offline")
    if (
        not isinstance(offline, dict)
        or offline.get("verified_by_replay") is not True
        or offline.get("replay_error") is not None
        or result.get("verified_by_replay") is not True
        or not isinstance(offline.get("replay"), dict)
        or offline["replay"].get("ran") is not True
    ):
        raise ResolverError(
            "offline replay did not verify: do not certify this result", "not-verified"
        )
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    os.replace(stream.name, path)


def request_signature(request: dict) -> str:
    """Hash of the request plus the on-disk source bytes it names, and the resolver."""
    payload = {"request": request, "resolver": RESOLVER_VERSION}
    if isinstance(request, dict) and "source_path" in request:
        try:
            payload["sources"] = source_digests(load_sources(validate_request(request)))
        except ResolverError as exc:
            payload["sources"] = f"unreadable:{exc.code}"
    return sha256_hex(json.dumps(payload, sort_keys=True).encode())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("requests", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--retry-failures", action="store_true")
    parser.add_argument("--cache-directory", type=Path)
    parser.add_argument(
        "--require-verified",
        action="store_true",
        help="exit non-zero unless every result passes require_verified()",
    )
    args = parser.parse_args(argv)
    try:
        require_remote_author()
    except ResolverError as exc:
        parser.error(str(exc))
    requests = json.loads(args.requests.read_text())["requests"]
    results = (
        json.loads(args.output.read_text()) if args.output.exists() else {"results": {}}
    )
    for request in requests:
        key = request.get("id") if isinstance(request, dict) else None
        if not isinstance(key, str):
            parser.error("every request needs a string id")
        signature = request_signature(request)
        previous = results["results"].get(key, {})
        cache_valid = True
        if request.get("capture_cache") and previous.get("status") == "completed":
            archive = previous.get("cache_archive", {})
            path = Path(archive.get("author_source", "/nonexistent/mango-cache"))
            cache_valid = path.is_file() and not path.is_symlink()
            if cache_valid:
                with path.open("rb") as stream:
                    cache_valid = hashlib.file_digest(
                        stream, "sha256"
                    ).hexdigest() == archive.get("sha256")
        if (
            cache_valid
            and previous.get("request_sha256") == signature
            and (previous.get("status") == "completed" or not args.retry_failures)
        ):
            continue
        try:
            result = (
                resolve(request, cache_directory=args.cache_directory)
                if args.cache_directory
                else resolve(request)
            )
            result["status"] = "completed"
        except Exception as exc:  # noqa: BLE001 - record each failed remote request and continue
            result = {
                "id": key,
                "status": "failed",
                "reason_code": getattr(exc, "code", "unexpected"),
                "error": redact(str(exc)),
            }
        result["request_sha256"] = signature
        results["results"][key] = result
        atomic_json(args.output, results)
        print(
            json.dumps(
                {
                    "id": key,
                    "status": result["status"],
                    "verified_by_replay": result.get("verified_by_replay", False),
                }
            ),
            flush=True,
        )

    def accepted(item: dict) -> bool:
        if not args.require_verified:
            return item["status"] == "completed"
        try:
            require_verified(item)
        except ResolverError:
            return False
        return True

    return int(not all(accepted(r) for r in results["results"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
