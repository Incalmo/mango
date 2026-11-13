"""Author an exact Composer download cache by running the ORIGINAL Composer, REMOTE HOST ONLY.

Run as ``python -m mango.authoring.composer_lock REQUESTS --output RESULTS`` (or the
``mango-lock-composer`` command once wired). Authoring-time resolver; see
docs/composer-cache.md. Its output describes a Composer *download cache* (locked dist archives only) that Mango's runtime
offline build can fetch and copy; it neither translates nor reproduces the original image,
and never bundles the final ``vendor/`` tree. Run it only on the remote authoring host with
``MANGO_REMOTE_AUTHOR=1`` and Docker there.

Request file::

    {"requests": [{
        "id": "snipe-it-task-4-composer",
        "base": "php@sha256:<64 hex>",              # immutable ORIGINAL base image
        "prepared_image": {                          # REQUIRED apt-prepared immutable image
            "id": "sha256:<64 hex>",                 #   image ID that actually runs
            "base": "php@sha256:<64 hex>",           #   must equal ``base``
            "apt_request_sha256": "<64 hex>"         #   binds the apt request that produced it
        },
        "php_version": "8.3.30",                     # exact original PHP (PHP_VERSION)
        "composer": {                                # exact original Composer, never substituted
            "version": "2.8.12",
            "source": "image",                       # "image": already in the prepared image
            "sha256": "<64 hex>"                     #   at /usr/local/bin/composer, or
            # "source": "phar", "phar_path": "/srv/inputs/composer-2.8.12.phar"   (host file)
        },
        "source_dir": "/srv/sources/snipe-it",       # READ-ONLY host dir; ONLY composer.json
                                                     # and composer.lock are read and copied
        "composer_argv": ["composer", "install", "--no-dev", "--prefer-dist",
                          "--no-interaction", "--no-progress", "--no-scripts",
                          "--no-autoloader"],        # the original command
        # optional
        "env": {"COMPOSER_ALLOW_SUPERUSER": "1"},   # allow-listed original ENV only
        "pinned_sha256": {"vendor/pkg@1.2.3": "<64 hex>"},   # if given, must cover every archive
        "required_php_extensions": ["gd", "zip"],    # must be loaded (``php -m``) in the prepared image
        "accept_unmapped_cache": [{"path": "...", "sha256": "..."}],   # reviewed leftovers (never .htaccess)
        "limits": {"max_archive_bytes": 536870912, "build_timeout_seconds": 3600}
    }]}

Per request:

1. Validation without network: exact php/composer versions, digest-pinned base, prepared
   image binding, allow-listed ``composer install`` flags (no source installs, no
   ``-d``/``--no-cache``/``update``/``require``), and a host-side lock analysis. Fail closed,
   each with a specific ``reason_code``: ``source-only-package``, ``path-repository``,
   ``source-repository``, ``custom-repository``, ``unlocked``, ``dev-version``,
   ``unsupported-dist-type``, ``private-dist-url``, ``credentials-config``, ...
2. A disposable container runs the PREPARED image ID (``--platform linux/amd64``, 1.5 CPU,
   4 GB, all capabilities dropped, no mounts) with a fresh HOME, COMPOSER_HOME and
   COMPOSER_CACHE_DIR. The running image ID, PHP version, Composer version and Composer
   file hash are verified. The lock must be current (``composer validate``).
3. Download phase: the original ``install`` flags plus the documented ``--download-only``,
   ``--no-plugins`` and ``--no-scripts``. Plugins and scripts therefore never run while the
   network is available. Nothing is upgraded, guessed or substituted.
4. The cache directory is copied out. This resolver does NOT assume Composer's cache file
   naming: ``composer config cache-files-dir`` must equal the documented
   ``<cache-dir>/files`` and every locked archive is bound to a cache file BY BYTES: the host
   downloads each locked dist URL (https, public, bounded), and its SHA-256 must equal exactly
   one cache file; lock ``shasum`` and ``pinned_sha256`` are checked when present. Cache files
   that no locked archive explains are rejected unless listed (path + SHA-256) in
   ``accept_unmapped_cache``. The ONE exception is Composer's own ``.htaccess`` marker:
   Composer 2.8.12 (``Composer/Factory.php``) writes ``file_put_contents($dir.'/.htaccess',
   'Deny from all')`` into its cache, home and data directories. When the author cache holds
   exactly that file (13 ASCII bytes, no newline, sha256-verified) it is carried in the lock as
   ``generated_files: [{path, content, sha256}]``; any other content, any other manager-written
   path, or a collision with an archive path fails closed.
5. Offline replay (required for ``verification: offline-replay-verified``): a fresh container
   from the same image ID with ``--network none`` receives composer.json, composer.lock and a
   writable COPY of only the verified cache, and runs the ORIGINAL argv unchanged (plugins and
   scripts run here, offline). The cache copy holds the verified archives AND the verified
   generated files. The installed package set must equal the locked set. The
   container network mode and image ID are checked. Mock/fake containers can never reach
   ``offline-replay-verified``.

Limitations (documented, not hidden): bytes at each URL are compared once, at authoring time
(GitHub zipball bytes are not guaranteed stable and fail ``unstable-dist-bytes`` if they
differ from what Composer cached); only the packages the original flags install are covered;
unauthenticated GitHub API rate limits may stop large locks (``download-failed``); the
``composer show --format=json`` install-set check fails closed on an unexpected shape.
Results and stage receipts are checkpointed after every step.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import uuid
from pathlib import Path
from typing import Callable

from mango.authoring.remote_common import (
    BASE_RE,
    ID_RE,
    IMAGE_ID_RE,
    SHA256_RE,
    ResolverError,
    atomic_json,
    download,
    file_digests,
    public_host,
    redact,
    require_remote_author,
    run,
    sha256_hex,
)

RESOLVER_VERSION = "3"  # 3: manager-generated cache files (.htaccess) are captured and locked
REQUEST_KEYS = {
    "id", "base", "prepared_image", "platform", "php_version", "composer", "source_dir",
    "composer_argv", "env", "limits", "pinned_sha256", "accept_unmapped_cache",
    "required_php_extensions",
}  # fmt: skip
PREPARED_KEYS = {"id", "base", "apt_request_sha256"}
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
PHP_EXT_RE = re.compile(r"^[a-z0-9_]{1,64}$")
PIN_KEY_RE = re.compile(r"^[a-z0-9_.-]+/[a-z0-9_.-]+@[^\s@]+$")

# Environment the original image may legitimately set; everything else Composer-related
# (COMPOSER_AUTH, COMPOSER_HOME, COMPOSER_CACHE_DIR, COMPOSER_DISABLE_NETWORK, ...) is refused.
ENV_ALLOWED = {"COMPOSER_ALLOW_SUPERUSER", "COMPOSER_MEMORY_LIMIT", "COMPOSER_PROCESS_TIMEOUT"}

WORK = "/work"
PROJECT = f"{WORK}/project"
HOME = f"{WORK}/home"
CACHE = f"{WORK}/cache"
PHAR = f"{WORK}/composer.phar"
IMAGE_COMPOSER = "/usr/local/bin/composer"
FILES_DIR = "files"  # documented default of cache-files-dir under cache-dir (verified at run time)

DEFAULT_LIMITS = {
    "max_archive_bytes": 512 << 20,
    "max_total_bytes": 8 << 30,
    "max_cache_bytes": 8 << 30,
    "max_archives": 5000,
    "max_input_bytes": 64 << 20,
    "build_timeout_seconds": 3600,
}
HARD_LIMITS = {
    "max_archive_bytes": 2 << 30,
    "max_total_bytes": 32 << 30,
    "max_cache_bytes": 32 << 30,
    "max_archives": 20_000,
    "max_input_bytes": 128 << 20,
    "build_timeout_seconds": 6 * 3600,
}
def PROGRESS(message: str) -> None:  # noqa: N802  (module hook, kept for monkeypatching)
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# composer install command screening (pure)
# --------------------------------------------------------------------------
FLAGS_OK = {
    "--no-dev", "--prefer-dist", "--no-interaction", "-n", "--no-progress", "--no-scripts",
    "--no-plugins", "--no-autoloader", "--optimize-autoloader", "-o", "--classmap-authoritative",
    "-a", "--no-audit", "--ignore-platform-reqs", "--quiet", "-q", "-v", "-vv", "-vvv",
    "--no-ansi", "--ansi", "--verbose",
}  # fmt: skip
# Flags that only affect autoloading or script/plugin execution: dropped from the download phase.
AUTOLOAD_ONLY = {
    "--no-autoloader", "--optimize-autoloader", "-o", "--classmap-authoritative", "-a",
    "--no-scripts", "--no-plugins",
}  # fmt: skip
UNSUPPORTED_FLAG_HINT = {
    "--prefer-source": "source installs use Git/VCS, not locked dist archives",
    "--prefer-install=source": "source installs use Git/VCS, not locked dist archives",
    "--prefer-install=auto": "auto may install from source",
    "--no-cache": "the cache is the artifact being authored",
    "-d": "use the project directory, not --working-dir",
    "--working-dir": "use the project directory, not --working-dir",
    "--download-only": "the download phase adds this itself; the original command must not carry it",
    "--dry-run": "a dry run installs nothing",
    "--no-install": "nothing would be installed",
    "--dev": "not an install flag in Composer 2",
}  # fmt: skip
UNSUPPORTED_COMMAND_HINT = {
    "update": "update resolves unlocked versions",
    "require": "require resolves unlocked versions",
    "create-project": "create-project resolves unlocked versions",
    "global": "global state is not supported",
    "remove": "remove resolves unlocked versions",
}  # fmt: skip


def screen_argv(argv: object, where: str = "composer_argv") -> dict:
    """Validate the explicit original command; return {'flags': [...], 'no_dev': bool, ...}."""
    if (
        not isinstance(argv, list)
        or len(argv) < 2
        or len(argv) > 60
        or not all(isinstance(a, str) and a and len(a) <= 512 and "\0" not in a for a in argv)
    ):
        raise ResolverError(f"{where} must be a list of strings: composer install ...", "invalid-argv")
    if argv[0] != "composer":
        raise ResolverError(f"{where}[0] must be the original 'composer' command", "invalid-argv")
    if argv[1] != "install":
        why = UNSUPPORTED_COMMAND_HINT.get(argv[1], "only the original `composer install` is supported")
        raise ResolverError(f"{where}: composer {argv[1]}: {why}", "unsupported-composer-command")
    flags = argv[2:]
    for flag in flags:
        if flag in FLAGS_OK:
            continue
        if flag in UNSUPPORTED_FLAG_HINT or flag.split("=", 1)[0] in UNSUPPORTED_FLAG_HINT:
            why = UNSUPPORTED_FLAG_HINT.get(flag) or UNSUPPORTED_FLAG_HINT[flag.split("=", 1)[0]]
            raise ResolverError(f"{where}: {flag}: {why}", "unsupported-composer-flag")
        if flag == "--prefer-install=dist":
            continue
        if re.fullmatch(r"--ignore-platform-req=[A-Za-z0-9_.*/-]+", flag):
            continue
        if not flag.startswith("-"):
            raise ResolverError(
                f"{where}: positional argument {flag!r}: only a full locked install is supported",
                "unsupported-composer-argument",
            )
        raise ResolverError(f"{where}: {flag} is not on the allow-list", "unsupported-composer-flag")
    return {
        "flags": list(flags),
        "no_dev": "--no-dev" in flags,
        "no_plugins": "--no-plugins" in flags,
        "no_scripts": "--no-scripts" in flags,
        "prefer_set": any(f in ("--prefer-dist", "--prefer-install=dist") for f in flags),
    }


def authoring_argv(info: dict) -> list[str]:
    """Download phase: original install flags, documented --download-only, plugins/scripts off."""
    keep = [f for f in info["flags"] if f not in AUTOLOAD_ONLY]
    out = ["install", *keep]
    if not info["prefer_set"]:
        out.append("--prefer-install=dist")
    out += ["--download-only", "--no-plugins", "--no-scripts"]
    out += [f for f in ("--no-interaction", "--no-progress", "--no-ansi") if f not in out]
    return out


def check_env(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ResolverError("env must be an object", "invalid-env")
    out = {}
    for key, val in value.items():
        if not isinstance(key, str) or not ENV_NAME_RE.match(key) or not isinstance(val, str) or "\0" in val:
            raise ResolverError(f"unsafe env entry {key!r}", "invalid-env")
        if key not in ENV_ALLOWED:
            raise ResolverError(
                f"env {key} is not allow-listed (allowed: {sorted(ENV_ALLOWED)}); "
                "Composer home/cache/auth/network variables are controlled by the resolver",
                "unsupported-env",
            )
        out[key] = val
    return dict(sorted(out.items()))


# --------------------------------------------------------------------------
# Request validation (pure)
# --------------------------------------------------------------------------
def validate_request(request: object) -> dict:
    if not isinstance(request, dict):
        raise ResolverError("request must be an object", "invalid-request")
    unknown = set(request) - REQUEST_KEYS
    if unknown:
        raise ResolverError(f"unknown request keys: {sorted(unknown)}", "invalid-request")
    out = dict(request)
    if not isinstance(out.get("id"), str) or not ID_RE.match(out["id"]):
        raise ResolverError("request id must match [A-Za-z0-9][A-Za-z0-9_.-]*", "invalid-request")
    if not isinstance(out.get("base"), str) or not BASE_RE.match(out["base"]):
        raise ResolverError("base must be NAME@sha256:<64 hex> (immutable, the ORIGINAL base)", "invalid-base")
    if out.get("platform", "linux/amd64") != "linux/amd64":
        raise ResolverError("only linux/amd64 is supported", "invalid-request")
    out["platform"] = "linux/amd64"
    prepared = out.get("prepared_image")
    if (
        not isinstance(prepared, dict)
        or set(prepared) != PREPARED_KEYS
        or not isinstance(prepared["id"], str)
        or not IMAGE_ID_RE.fullmatch(prepared["id"])
        or prepared["base"] != out["base"]
        or not isinstance(prepared["apt_request_sha256"], str)
        or not SHA256_RE.fullmatch(prepared["apt_request_sha256"])
    ):
        raise ResolverError(
            "prepared_image is REQUIRED: {id: sha256:<64 hex> image ID, base: the exact original base, "
            "apt_request_sha256: <64 hex>}; tags are not accepted",
            "invalid-prepared-image",
        )
    if not isinstance(out.get("php_version"), str) or not VERSION_RE.match(out["php_version"]):
        raise ResolverError("php_version must be the exact original X.Y.Z", "invalid-toolchain")
    composer = out.get("composer")
    if (
        not isinstance(composer, dict)
        or set(composer) - {"version", "source", "sha256", "phar_path"}
        or not isinstance(composer.get("version"), str)
        or not VERSION_RE.match(composer["version"])
        or composer.get("source") not in ("image", "phar")
        or not isinstance(composer.get("sha256"), str)
        or not SHA256_RE.fullmatch(composer["sha256"])
    ):
        raise ResolverError(
            "composer must be {version: exact X.Y.Z, source: 'image'|'phar', sha256: <64 hex>[, phar_path]}; "
            "a version or binary is never substituted",
            "invalid-toolchain",
        )
    if composer["source"] == "phar":
        phar = composer.get("phar_path")
        if not isinstance(phar, str) or not os.path.isabs(phar) or "\0" in phar:
            raise ResolverError("composer.phar_path must be an absolute host path", "invalid-toolchain")
    elif "phar_path" in composer:
        raise ResolverError("composer.phar_path is only valid with source 'phar'", "invalid-toolchain")
    out["composer"] = dict(composer)
    source = out.get("source_dir")
    if not isinstance(source, str) or not os.path.isabs(source) or "\0" in source:
        raise ResolverError("source_dir must be an absolute path on this host", "invalid-request")
    out["composer_argv"] = list(out.get("composer_argv") or [])
    out["argv_info"] = screen_argv(out["composer_argv"])
    out["env"] = check_env(out.get("env"))
    pins = out.get("pinned_sha256") or {}
    if not isinstance(pins, dict) or not all(
        isinstance(k, str) and PIN_KEY_RE.match(k) and isinstance(v, str) and SHA256_RE.fullmatch(v)
        for k, v in pins.items()
    ):
        raise ResolverError("pinned_sha256 must map 'vendor/package@version' -> sha256", "invalid-request")
    out["pinned_sha256"] = dict(sorted(pins.items()))
    accepted = out.get("accept_unmapped_cache") or []
    if not isinstance(accepted, list) or not all(
        isinstance(a, dict) and set(a) == {"path", "sha256"} and isinstance(a["path"], str)
        and isinstance(a["sha256"], str) and SHA256_RE.fullmatch(a["sha256"])
        for a in accepted
    ):  # fmt: skip
        raise ResolverError("accept_unmapped_cache must list {path, sha256} objects", "invalid-request")
    out["accept_unmapped_cache"] = sorted(accepted, key=lambda a: a["path"])
    extensions = out.get("required_php_extensions") or []
    if (
        not isinstance(extensions, list)
        or len(extensions) > 64
        or not all(isinstance(e, str) and PHP_EXT_RE.fullmatch(e) for e in extensions)
        or len(set(extensions)) != len(extensions)
    ):
        raise ResolverError(
            "required_php_extensions must be a list of unique lowercase PHP extension names", "invalid-request"
        )
    out["required_php_extensions"] = sorted(extensions)
    limits = dict(DEFAULT_LIMITS)
    given = out.get("limits") or {}
    if not isinstance(given, dict):
        raise ResolverError("limits must be an object", "invalid-request")
    for key, value in given.items():
        if key not in DEFAULT_LIMITS:
            raise ResolverError(f"unknown limit {key!r}", "invalid-request")
        if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= HARD_LIMITS[key]:
            raise ResolverError(f"limit {key} must be an integer in 1..{HARD_LIMITS[key]}", "invalid-request")
        limits[key] = value
    out["limits"] = limits
    return out


# --------------------------------------------------------------------------
# composer.json / composer.lock analysis (pure)
# --------------------------------------------------------------------------
REPO_REASON = {
    "vcs": ("source-repository", "VCS repositories install from Git/VCS sources"),
    "git": ("source-repository", "Git repositories install from Git sources"),
    "github": ("source-repository", "GitHub VCS repositories install from Git sources"),
    "gitlab": ("source-repository", "GitLab VCS repositories install from Git sources"),
    "bitbucket": ("source-repository", "Bitbucket VCS repositories install from Git sources"),
    "git-bitbucket": ("source-repository", "Bitbucket VCS repositories install from Git sources"),
    "svn": ("source-repository", "SVN repositories install from VCS sources"),
    "hg": ("source-repository", "Mercurial repositories install from VCS sources"),
    "fossil": ("source-repository", "Fossil repositories install from VCS sources"),
    "perforce": ("source-repository", "Perforce repositories install from VCS sources"),
    "path": ("path-repository", "path repositories read local directories that are not in the lock"),
    "artifact": ("artifact-repository", "artifact repositories read local archives"),
    "package": ("custom-repository", "inline package repositories carry unreviewed dist/source data"),
    "composer": ("custom-repository", "custom/private Composer repositories are not supported"),
}  # fmt: skip
CREDENTIAL_CONFIG = (
    "http-basic", "bearer", "github-oauth", "gitlab-oauth", "gitlab-token", "bitbucket-oauth",
    "forgejo-token", "custom-headers", "client-certificate",
)  # fmt: skip
UNSUPPORTED_CONFIG = {
    "secure-http": "disabling secure-http allows insecure downloads",
    "disable-tls": "disabling TLS is not supported",
    "cafile": "custom CA files are not supported",
    "capath": "custom CA paths are not supported",
    "github-domains": "custom GitHub domains are private hosts",
    "gitlab-domains": "custom GitLab domains are private hosts",
    "github-protocols": "Git protocols imply source installs",
    "use-github-api": "disabling the GitHub API changes dist URLs",
    "cache-dir": "custom cache locations are controlled by the resolver",
    "cache-files-dir": "custom cache locations are controlled by the resolver",
    "cache-read-only": "a read-only cache hides what the build needs",
    "cache-files-ttl": "cache expiry would invalidate the authored cache",
    "cache-files-maxsize": "cache size limits would evict authored archives",
    "home": "custom Composer home is controlled by the resolver",
}  # fmt: skip


GITHUB_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9_])?"
GITHUB_REPO_PATH = re.compile(rf"/({GITHUB_NAME})/({GITHUB_NAME})")
SHA40 = re.compile(r"[0-9a-f]{40}")


def github_vcs_declaration(repo: dict, key: object) -> str:
    """A declared vcs repository is only tolerated as an exact public https://github.com/owner/repo.

    It is never fetched here: the lock must still carry a dist archive for everything installed;
    the declaration only lets the lock's pinned dev package be recognised. Returns ``owner/repo``
    (lower-cased).
    """
    where = f"composer.json repositories[{key!r}]"
    url = repo.get("url")
    if set(repo) != {"type", "url"} or not isinstance(url, str):
        raise ResolverError(f"{where}: a vcs repository may only declare type and url", "source-repository")
    parts = urllib.parse.urlsplit(url)
    match = GITHUB_REPO_PATH.fullmatch(parts.path)
    if (
        parts.scheme != "https"
        or parts.netloc != "github.com"
        or parts.query
        or parts.fragment
        or url != f"https://github.com{parts.path}"
        or not match
        or match.group(2).endswith(".git")
        or ".." in (match.group(1), match.group(2))
        or "." in (match.group(1), match.group(2))
    ):
        raise ResolverError(f"{where}: only exact public https://github.com/<owner>/<repo> is supported", "source-repository")
    return f"{match.group(1)}/{match.group(2)}".lower()


def pinned_github_dev(package: dict, label: str, url: str, github_vcs: frozenset[str]) -> None:
    """A dev version is acceptable only as an immutable, matching GitHub commit (source == dist == URL)."""
    source, dist = package.get("source"), package.get("dist")

    def reject(why: str) -> ResolverError:
        return ResolverError(f"{label}: dev version is not an immutable declared GitHub commit ({why})", "dev-version")

    if not github_vcs:
        raise ResolverError(f"{label}: dev versions follow moving refs and are not supported", "dev-version")
    if not isinstance(source, dict) or source.get("type") != "git" or not isinstance(dist, dict):
        raise reject("needs git source and dist")
    reference = source.get("reference")
    if not isinstance(reference, str) or not SHA40.fullmatch(reference):
        raise reject("source.reference is not a lowercase 40-hex commit")
    if dist.get("reference") != reference:
        raise reject("dist.reference differs from source.reference")
    src = urllib.parse.urlsplit(str(source.get("url")))
    src_match = GITHUB_REPO_PATH.fullmatch(src.path[:-4] if src.path.endswith(".git") else src.path)
    if src.scheme != "https" or src.netloc != "github.com" or src.query or src.fragment or not src_match:
        raise reject("source.url is not https://github.com/<owner>/<repo>")
    repo = f"{src_match.group(1)}/{src_match.group(2)}".lower()
    if repo not in github_vcs:
        raise reject("source repository is not declared in composer.json")
    dist_path = urllib.parse.urlsplit(url).path
    if urllib.parse.urlsplit(url).netloc != "api.github.com" or dist_path.lower() != f"/repos/{repo}/zipball/{reference}":
        raise reject("dist.url is not the api.github.com zipball of the same repo and commit")


def screen_composer_json(raw: bytes) -> dict:
    try:
        data = json.loads(raw)
    except ValueError:
        raise ResolverError("composer.json is not valid JSON", "invalid-composer-json") from None
    if not isinstance(data, dict):
        raise ResolverError("composer.json must be an object", "invalid-composer-json")
    repos = data.get("repositories", [])
    entries = list(repos.items()) if isinstance(repos, dict) else list(enumerate(repos or []))
    github_vcs: set[str] = set()
    for key, repo in entries:
        if repo is False or (isinstance(repo, dict) and set(repo) == {"packagist.org"} and repo["packagist.org"] is False):
            continue
        if isinstance(key, str) and key == "packagist.org" and repo is False:
            continue
        kind = repo.get("type") if isinstance(repo, dict) else None
        if kind == "vcs":
            github_vcs.add(github_vcs_declaration(repo, key))
            continue
        code, why = REPO_REASON.get(kind, ("custom-repository", f"repository type {kind!r} is not supported"))
        raise ResolverError(f"composer.json repositories[{key!r}] type {kind!r}: {why}", code)
    config = data.get("config", {})
    if not isinstance(config, dict):
        raise ResolverError("composer.json config must be an object", "invalid-composer-json")
    for key in config:
        if key in CREDENTIAL_CONFIG:
            raise ResolverError(f"composer.json config.{key}: credentials are never supported", "credentials-config")
        if key in UNSUPPORTED_CONFIG:
            raise ResolverError(f"composer.json config.{key}: {UNSUPPORTED_CONFIG[key]}", "unsupported-config")
    preferred = config.get("preferred-install")
    if preferred not in (None, "dist") and not (isinstance(preferred, dict) and set(preferred.values()) <= {"dist"}):
        raise ResolverError(
            f"composer.json config.preferred-install={preferred!r} may install from source", "unsupported-config"
        )
    scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
    return {"script_events": sorted(scripts), "require": data.get("require") or {}, "github_vcs": sorted(github_vcs)}


def dist_url_ok(url: object, name: str) -> str:
    if not isinstance(url, str) or not url:
        raise ResolverError(f"{name}: dist.url is empty", "no-dist")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ResolverError(f"{name}: dist.url must be an https URL", "unstable-dist-url")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ResolverError(f"{name}: dist.url carries credentials", "private-dist-url")
    if parts.query or parts.fragment:
        raise ResolverError(f"{name}: dist.url has a query/fragment (token-like, not a stable URL)", "unstable-dist-url")
    if not public_host(parts.hostname):
        raise ResolverError(f"{name}: dist.url host is not a public DNS name", "private-dist-url")
    if any(ch in parts.path for ch in " \"'<>\\") or ".." in parts.path.split("/"):
        raise ResolverError(f"{name}: dist.url has an unsafe path", "unstable-dist-url")
    return url


def analyze_lock(lock_raw: bytes, argv_info: dict, github_vcs: object = ()) -> dict:
    """The packages the original flags install, each with a locked https dist archive."""
    try:
        lock = json.loads(lock_raw)
    except ValueError:
        raise ResolverError("composer.lock is not valid JSON", "unlocked") from None
    if not isinstance(lock, dict) or not isinstance(lock.get("packages"), list):
        raise ResolverError("composer.lock has no packages list", "unlocked")
    if not isinstance(lock.get("content-hash"), str) or not lock["content-hash"]:
        raise ResolverError("composer.lock has no content-hash (not a real lock file)", "unlocked")
    github_vcs = frozenset(github_vcs)
    needed = list(lock["packages"])
    excluded = []
    dev = lock.get("packages-dev") or []
    if not isinstance(dev, list):
        raise ResolverError("composer.lock packages-dev is malformed", "unlocked")
    (excluded if argv_info["no_dev"] else needed).extend(dev)
    packages, metapackages, plugins = [], [], []
    for package in needed:
        if not isinstance(package, dict) or not isinstance(package.get("name"), str) or not isinstance(package.get("version"), str):
            raise ResolverError("composer.lock package without name/version", "unlocked")
        name, version = package["name"], package["version"]
        label = f"{name}@{version}"
        is_dev = version.startswith("dev-") or version.endswith("-dev")
        if is_dev and not github_vcs:
            raise ResolverError(f"{label}: dev versions follow moving refs and are not supported", "dev-version")
        dist = package.get("dist")
        if package.get("type") == "metapackage" and not dist:
            metapackages.append(label)
            continue
        if package.get("type") == "composer-plugin":
            plugins.append(label)
        if not isinstance(dist, dict) or not dist.get("url"):
            source = package.get("source") if isinstance(package.get("source"), dict) else {}
            raise ResolverError(
                f"{label}: no dist archive in the lock"
                + (f" (source-only: {source.get('type', '?')})" if source else ""),
                "source-only-package" if source else "no-dist",
            )
        if dist.get("type") == "path" or (package.get("transport-options") or {}).get("symlink"):
            raise ResolverError(f"{label}: path dist is a local directory, not a locked archive", "path-repository")
        if dist.get("type") not in ("zip", "tar"):
            raise ResolverError(f"{label}: dist.type {dist.get('type')!r} is not supported (zip, tar)", "unsupported-dist-type")
        url = dist_url_ok(dist["url"], label)
        if is_dev:
            pinned_github_dev(package, label, url, github_vcs)
        shasum = dist.get("shasum") or ""
        if shasum and not re.fullmatch(r"[0-9a-fA-F]{40}", shasum):
            raise ResolverError(f"{label}: dist.shasum is not a SHA-1", "invalid-lock")
        packages.append(
            {"name": name, "version": version, "url": url, "type": dist["type"],
             "shasum": shasum.lower(), "reference": dist.get("reference") or ""}  # fmt: skip
        )
    packages.sort(key=lambda p: (p["name"], p["version"]))
    seen = set()
    for p in packages:
        if (p["name"], p["version"]) in seen:
            raise ResolverError(f"duplicate locked package {p['name']}@{p['version']}", "invalid-lock")
        seen.add((p["name"], p["version"]))
    return {
        "packages": packages,
        "metapackages": sorted(metapackages),
        "plugins": sorted(plugins),
        "excluded_dev": sorted(f"{p.get('name')}@{p.get('version')}" for p in excluded if isinstance(p, dict)),
        "content_hash": lock["content-hash"],
        "installed_names": sorted(
            {p["name"].lower() for p in packages} | {m.rsplit("@", 1)[0].lower() for m in metapackages}
        ),
    }


# --------------------------------------------------------------------------
# Host inputs
# --------------------------------------------------------------------------
def read_input(directory: str, name: str, limit: int) -> bytes:
    path = Path(directory) / name
    try:
        info = path.lstat()
    except OSError:
        raise ResolverError(f"{name} not found in source_dir", "source-missing" if name == "composer.json" else "unlocked") from None
    if not stat.S_ISREG(info.st_mode):
        raise ResolverError(f"{name} must be a regular file (no symlinks)", "unsafe-source")
    if info.st_size > limit:
        raise ResolverError(f"{name} exceeds {limit} bytes", "limit-exceeded")
    return path.read_bytes()


def read_phar(composer: dict, limit: int) -> bytes | None:
    if composer["source"] != "phar":
        return None
    path = Path(composer["phar_path"])
    try:
        info = path.lstat()
    except OSError:
        raise ResolverError("composer.phar_path not found", "toolchain-input-missing") from None
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ResolverError("composer.phar_path must be a regular file within limits", "unsafe-source")
    data = path.read_bytes()
    if sha256_hex(data) != composer["sha256"]:
        raise ResolverError("composer PHAR sha256 differs from the declared sha256", "toolchain-mismatch")
    return data


def tar_bytes(members: dict[str, bytes], mode: int = 0o644) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in sorted(members.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Manager-generated cache files (explicit, closed set)
# --------------------------------------------------------------------------
# Composer 2.8.12 Factory.php: file_put_contents($dir.'/.htaccess', 'Deny from all') for the cache,
# home and data directories. Exact bytes: 13 ASCII characters, no newline.
HTACCESS_CONTENT = "Deny from all"
HTACCESS_SHA256 = sha256_hex(HTACCESS_CONTENT.encode("ascii"))
KNOWN_GENERATED_FILES = {".htaccess": HTACCESS_CONTENT}
GENERATED_KEYS = {"path", "content", "sha256"}


def check_generated_files(value: object, artifact_paths: object = ()) -> list[dict]:
    """Validate a lock's ``generated_files``: only the known ``.htaccess`` with its exact bytes.

    Rejects unknown paths or contents, a hash that does not match the content, duplicates, and any
    collision (equal, file-under-file or file-over-directory) with a locked archive path.
    """
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > len(KNOWN_GENERATED_FILES):
        raise ResolverError("generated_files must be a short list of {path, content, sha256}", "invalid-generated-file")
    seen: set[str] = set()
    out = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != GENERATED_KEYS:
            raise ResolverError("each generated file must be exactly {path, content, sha256}", "invalid-generated-file")
        path, content, digest = entry["path"], entry["content"], entry["sha256"]
        if not isinstance(path, str) or path not in KNOWN_GENERATED_FILES:
            raise ResolverError(f"generated file path {path!r} is not a known manager-generated file", "invalid-generated-file")
        if not isinstance(content, str) or content != KNOWN_GENERATED_FILES[path] or not content.isascii():
            raise ResolverError(f"generated file {path} content is not the known manager content", "invalid-generated-file")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest) or digest != sha256_hex(content.encode("ascii")):
            raise ResolverError(f"generated file {path} sha256 does not match its content", "invalid-generated-file")
        if path in seen:
            raise ResolverError(f"duplicate generated file {path}", "invalid-generated-file")
        seen.add(path)
        for other in artifact_paths:
            if other == path or other.startswith(path + "/") or path.startswith(other + "/"):
                raise ResolverError(f"generated file {path} collides with locked path {other}", "invalid-generated-file")
        out.append({"path": path, "content": content, "sha256": digest})
    return sorted(out, key=lambda e: e["path"])


# --------------------------------------------------------------------------
# Cache inventory and byte binding (pure given a directory)
# --------------------------------------------------------------------------
def walk_cache(root: Path, limits: dict) -> dict[str, str]:
    """{relative path: sha256} of the copied-out cache; symlinks/special files fail closed."""
    out: dict[str, str] = {}
    total = 0
    for current, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(current) / name
            info = path.lstat()
            rel = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ResolverError(f"cache entry {rel!r} is a symlink or special file", "unsafe-cache")
            if any(ord(ch) < 32 for ch in rel) or "\\" in rel:
                raise ResolverError("cache entry has an unsafe name", "unsafe-cache")
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > limits["max_cache_bytes"]:
                    raise ResolverError("cache exceeds max_cache_bytes", "limit-exceeded")
                out[rel] = file_digests(path)["sha256"]
    return out


def bind_cache(cache: dict[str, str], expected: list[dict], accepted: list[dict]) -> dict:
    """Bind each locked archive to the cache file with the same bytes; explain every file.

    Known manager-generated files (``.htaccess`` with its exact bytes) are explained as such and
    returned under ``generated``; a known path with other bytes is rejected, never accepted.
    """
    by_hash: dict[str, list[str]] = {}
    for rel, digest in cache.items():
        by_hash.setdefault(digest, []).append(rel)
    artifacts, claimed = [], set()
    for package in expected:
        label = f"{package['name']}@{package['version']}"
        paths = [p for p in by_hash.get(package["sha256"], []) if p.startswith(FILES_DIR + "/")]
        if not paths:
            raise ResolverError(
                f"{label}: no file under {FILES_DIR}/ in the Composer cache has the bytes of the locked archive",
                "cache-missing-artifact",
            )
        if len(paths) > 1 or paths[0] in claimed:
            raise ResolverError(f"{label}: archive bytes match more than one cache file", "ambiguous-cache-file")
        claimed.add(paths[0])
        artifacts.append(
            {"path": paths[0], "urls": [package["url"]], "sha256": package["sha256"],
             "size": package["size"], "package": package["name"], "version": package["version"]}  # fmt: skip
        )
    generated = []
    for path in sorted(p for p in cache if p in KNOWN_GENERATED_FILES and p not in claimed):
        if cache[path] != sha256_hex(KNOWN_GENERATED_FILES[path].encode("ascii")):
            raise ResolverError(
                f"manager-generated cache file {path} does not have the known content", "generated-file-mismatch"
            )
        generated.extend(check_generated_files(
            [{"path": path, "content": KNOWN_GENERATED_FILES[path], "sha256": cache[path]}], [a["path"] for a in artifacts]
        ))
        claimed.add(path)
    accepted_map = {a["path"]: a["sha256"] for a in accepted}
    if set(accepted_map) & set(KNOWN_GENERATED_FILES):
        raise ResolverError(
            "manager-generated files are locked as generated_files, not listed in accept_unmapped_cache", "invalid-request"
        )
    unmapped = sorted(p for p in cache if p not in claimed)
    bad = [p for p in unmapped if accepted_map.get(p) != cache[p]]
    if bad:
        raise ResolverError(
            "cache files not explained by any locked archive (review, then list path+sha256 in "
            f"accept_unmapped_cache): {', '.join(bad[:20])}" + (" ..." if len(bad) > 20 else ""),
            "unexplained-cache-file",
        )
    stale = sorted(set(accepted_map) - set(cache))
    if stale:
        raise ResolverError(f"accept_unmapped_cache lists files that are not in the cache: {stale[:10]}", "invalid-request")
    return {
        "artifacts": sorted(artifacts, key=lambda a: a["path"]),
        "generated_files": generated,
        "accepted_unmapped": [{"path": p, "sha256": cache[p]} for p in unmapped],
    }


def cache_tar(root: Path, rels: list[str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for rel in sorted(rels):
            path = root / rel
            info = tarfile.TarInfo(rel)
            info.size, info.mode = path.stat().st_size, 0o644
            with path.open("rb") as stream:
                archive.addfile(info, stream)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Container
# --------------------------------------------------------------------------
class Container:
    """A disposable bounded root container on the PREPARED image ID, removed on exit."""

    PROOF = "docker"

    def __init__(self, image: str, network: str = "bridge") -> None:
        self.image, self.network = image, network
        self.name = f"mango-composerdeps-{uuid.uuid4().hex[:12]}"

    def __enter__(self) -> "Container":
        require_remote_author()
        run(
            [
                "docker", "run", "-d", "--rm",
                "--platform", "linux/amd64",
                "--cpus", "1.5", "--memory", "4g", "--memory-swap", "4g", "--pids-limit", "1024",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--network", self.network,
                "--name", self.name,
                "--user", "0:0",
                "--label", "mango.remote-composer-deps=1",
                "--entrypoint", "sleep",
                self.image, "infinity",
            ]
        )  # fmt: skip
        return self

    def __exit__(self, *exc) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, text=True)

    def exec(self, argv, env=None, workdir=None, check=True, timeout=None):
        command = ["docker", "exec"]
        for key, value in (env or {}).items():
            command += ["-e", f"{key}={value}"]
        if workdir:
            command += ["-w", workdir]
        return run([*command, self.name, *argv], check=check, timeout=timeout)

    def copy_in_tar(self, data: bytes, destination: str) -> None:
        try:
            result = subprocess.run(
                ["docker", "cp", "-", f"{self.name}:{destination}"], input=data, capture_output=True
            )  # fmt: skip
        except OSError as exc:
            raise ResolverError(f"docker cp failed: {exc}", "docker") from None
        if result.returncode != 0:
            raise ResolverError(f"docker cp failed: {redact(result.stderr.decode('utf-8', 'replace')[-1000:])}", "docker")

    def copy_out(self, source: str, destination: Path) -> None:
        run(["docker", "cp", f"{self.name}:{source}/.", str(destination)])

    def image_id(self) -> str:
        return run(["docker", "inspect", "--format", "{{.Image}}", self.name]).stdout.strip()

    def image_platform(self) -> str:
        image = self.image_id()
        return run(["docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image]).stdout.strip()

    def network_mode(self) -> str:
        return run(["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", self.name]).stdout.strip()


def composer_env(request: dict) -> dict[str, str]:
    """Hermetic environment: fresh HOME/COMPOSER_HOME/cache; no inherited Composer variables."""
    return {
        **request["env"],
        "HOME": HOME,
        "COMPOSER_HOME": f"{HOME}/.composer",
        "COMPOSER_CACHE_DIR": CACHE,
        "http_proxy": "", "https_proxy": "", "HTTP_PROXY": "", "HTTPS_PROXY": "",
        "no_proxy": "", "NO_PROXY": "",
    }  # fmt: skip


def composer_cmd(request: dict) -> list[str]:
    return ["php", PHAR] if request["composer"]["source"] == "phar" else [IMAGE_COMPOSER]


def parse_php_modules(text: str) -> list[str]:
    """Sorted lowercase names under ``[PHP Modules]`` of ``php -m`` output (Zend modules excluded)."""
    names, active = set(), False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            active = line == "[PHP Modules]"
        elif line and active:
            names.add(line.lower())
    return sorted(names)


def stage_toolchain(request: dict, container, phar: bytes | None, label: str) -> dict:
    """Verify image ID, platform, PHP and Composer inside a container; return evidence."""
    prepared = request["prepared_image"]
    if container.image_id() != prepared["id"]:
        raise ResolverError(f"{label}: container is not running the declared prepared_image ID", "prepared-image-mismatch")
    platform = container.image_platform()
    if platform != "linux/amd64":
        raise ResolverError(f"{label}: image is {platform}, not linux/amd64", "base-platform")
    arch = container.exec(["uname", "-m"], check=False)
    if arch.returncode != 0 or arch.stdout.strip() not in ("x86_64", "amd64"):
        raise ResolverError(f"{label}: container is not x86_64", "base-platform")
    container.exec(["mkdir", "-p", PROJECT, HOME, CACHE])
    names = [line.partition("=")[0] for line in container.exec(["env"]).stdout.splitlines()]
    bad = sorted(n for n in names if n.startswith("COMPOSER_") and n not in ENV_ALLOWED)
    if bad:
        raise ResolverError(f"{label}: image sets Composer variables the resolver controls: {bad}", "image-composer-env")
    php = container.exec(["php", "-r", "echo PHP_VERSION;"], check=False)
    if php.returncode != 0 or php.stdout.strip() != request["php_version"]:
        raise ResolverError(
            f"{label}: php is {php.stdout.strip() or 'missing'}, expected exactly {request['php_version']}",
            "toolchain-mismatch",
        )
    modules = container.exec(["php", "-m"], check=False)
    loaded = parse_php_modules(modules.stdout or "") if modules.returncode == 0 else None
    if not loaded:
        raise ResolverError(f"{label}: `php -m` is unreadable; PHP extensions are not verified", "toolchain-mismatch")
    absent = sorted(set(request["required_php_extensions"]) - set(loaded))
    if absent:
        raise ResolverError(
            f"{label}: the prepared image lacks required PHP extensions {absent} "
            "(platform checks are never bypassed)",
            "platform-requirements",
        )
    if phar is not None:
        container.copy_in_tar(tar_bytes({"composer.phar": phar}), WORK)
        binary = PHAR
    else:
        binary = IMAGE_COMPOSER
    digest = container.exec(["sha256sum", binary], check=False)
    got = digest.stdout.split()[0] if digest.returncode == 0 and digest.stdout.split() else ""
    if got != request["composer"]["sha256"]:
        raise ResolverError(f"{label}: composer binary sha256 {got or 'unreadable'} != declared", "toolchain-mismatch")
    env = composer_env(request)
    version = container.exec([*composer_cmd(request), "--version", "--no-ansi"], env=env, workdir="/", check=False, timeout=300)
    match = re.search(r"Composer (?:version )?(\d+\.\d+\.\d+)", version.stdout or "")
    if version.returncode != 0 or not match or match.group(1) != request["composer"]["version"]:
        raise ResolverError(
            f"{label}: composer is {match.group(1) if match else 'unusable'}, expected exactly {request['composer']['version']}",
            "toolchain-mismatch",
        )
    return {
        "image_id": prepared["id"],
        "platform": platform,
        "php_version": php.stdout.strip(),
        "php_extensions": loaded,
        "php_extensions_sha256": sha256_hex("\n".join(loaded).encode()),
        "composer_version": match.group(1),
        "composer_sha256": got,
        "composer_source": request["composer"]["source"],
    }


def stage_project(container, files: dict[str, bytes], label: str) -> None:
    container.copy_in_tar(tar_bytes(files), PROJECT)
    out = container.exec(["sh", "-c", 'cd "$1" && sha256sum composer.json composer.lock', "sh", PROJECT]).stdout
    seen = {line.split()[1]: line.split()[0] for line in out.splitlines() if len(line.split()) == 2}
    if seen != {name: sha256_hex(data) for name, data in files.items()}:
        raise ResolverError(f"{label}: project files in the container differ from the host copy", "source-mutated")


def classify_failure(text: str) -> tuple[str, str]:
    low = text.lower()
    if "not up to date" in low or "lock file is not up" in low:
        return "lock-out-of-date", "composer.lock does not match composer.json"
    if "ext-" in low and ("missing from your system" in low or "requires" in low and "extension" in low):
        return "platform-requirements", "the prepared image lacks a PHP extension/version the lock needs (not ignored)"
    if "plugin" in low and ("allow-plugins" in low or "do not allow" in low):
        return "plugin-not-allowed", "a Composer plugin is not allowed by composer.json"
    if "could not resolve host" in low or "curl error" in low or "failed to download" in low:
        return "download-failed", "Composer could not download a locked archive"
    return "composer-failed", "composer failed"


def author_in_container(request: dict, container, phar, files: dict[str, bytes], work: Path, receipt) -> dict:
    env = composer_env(request)
    evidence = stage_toolchain(request, container, phar, "authoring")
    receipt("toolchain-verified", **evidence)
    stage_project(container, files, "authoring")
    cmd = composer_cmd(request)
    validate = container.exec(
        [*cmd, "validate", "--no-check-publish", "--no-check-all", "--no-ansi", "--no-interaction"],
        env=env, workdir=PROJECT, check=False, timeout=600,
    )  # fmt: skip
    if validate.returncode != 0:
        text = redact((validate.stdout or "")[-3000:] + (validate.stderr or "")[-1000:])
        code, why = classify_failure(text)
        raise ResolverError(f"composer validate failed ({why}): {text[-1200:]}", "lock-out-of-date" if code == "lock-out-of-date" else "invalid-composer-json")
    receipt("lock-validated")
    helped = container.exec([*cmd, "install", "--help", "--no-ansi"], env=env, workdir=PROJECT, check=False, timeout=300)
    if "--download-only" not in (helped.stdout or ""):
        raise ResolverError(
            f"Composer {request['composer']['version']} has no documented `install --download-only`; "
            "no other version is substituted",
            "download-only-unsupported",
        )
    where = container.exec([*cmd, "config", "--global", "cache-files-dir", "--no-ansi"], env=env, workdir=PROJECT, check=False, timeout=300)
    if where.returncode != 0 or where.stdout.strip() != f"{CACHE}/{FILES_DIR}":
        raise ResolverError(
            f"cache-files-dir is {where.stdout.strip() or 'unreadable'}, expected {CACHE}/{FILES_DIR}", "cache-layout"
        )
    argv = [*cmd, *authoring_argv(request["argv_info"])]
    PROGRESS(f"{request['id']}: {' '.join(authoring_argv(request['argv_info']))}")
    build = container.exec(argv, env=env, workdir=PROJECT, check=False, timeout=request["limits"]["build_timeout_seconds"])
    if build.returncode != 0:
        text = redact((build.stdout or "")[-4000:] + (build.stderr or "")[-4000:])
        code, why = classify_failure(text)
        raise ResolverError(f"{why}: {text[-1500:]}", code)
    receipt("download-phase-complete", argv=argv)
    size_kb = container.exec(["sh", "-c", 'du -sk "$1" | cut -f1', "sh", CACHE]).stdout.strip()
    if not size_kb.isdigit() or int(size_kb) * 1024 > request["limits"]["max_cache_bytes"]:
        raise ResolverError("Composer cache exceeds max_cache_bytes", "limit-exceeded")
    copy = work / "cache"
    copy.mkdir()
    container.copy_out(CACHE, copy)
    return {**evidence, "authoring_argv": argv, "cache_dir": copy}


def replay_offline(request: dict, container, phar, files: dict[str, bytes], cache_root: Path, rels: list[str], plan: dict, receipt) -> dict:
    """ORIGINAL argv, unchanged, in a fresh --network none container with a writable cache copy."""
    env = composer_env(request)
    evidence = stage_toolchain(request, container, phar, "replay")
    mode = container.network_mode()
    if mode != "none":
        raise ResolverError(f"replay container network mode is {mode!r}, expected 'none'", "replay-network")
    stage_project(container, files, "replay")
    container.copy_in_tar(cache_tar(cache_root, rels), CACHE)
    before = container_cache_manifest(container)
    argv = [*composer_cmd(request), *request["composer_argv"][1:]]
    PROGRESS(f"{request['id']}: offline replay: {' '.join(request['composer_argv'])}")
    build = container.exec(argv, env=env, workdir=PROJECT, check=False, timeout=request["limits"]["build_timeout_seconds"])
    tail = redact((build.stdout or "")[-1500:] + (build.stderr or "")[-1500:])
    if build.returncode != 0:
        raise ResolverError(f"offline replay of the original command failed ({build.returncode}): {tail[-1500:]}", "replay-failed")
    shown = container.exec([*composer_cmd(request), "show", "--format=json", "--no-ansi", "--no-plugins", "--no-scripts"], env=env, workdir=PROJECT, check=False, timeout=600)
    try:
        listing = json.loads(shown.stdout)
        entries = listing["installed"] if isinstance(listing, dict) else listing
        installed = sorted({str(e["name"]).lower() for e in entries})
    except (ValueError, KeyError, TypeError):
        raise ResolverError("composer show --format=json returned an unexpected shape; install set not verified", "replay-unverified-install-set") from None
    if installed != plan["installed_names"]:
        missing = sorted(set(plan["installed_names"]) - set(installed))
        extra = sorted(set(installed) - set(plan["installed_names"]))
        raise ResolverError(f"offline install set differs from the lock: missing {missing[:10]}, extra {extra[:10]}", "replay-install-mismatch")
    after = container_cache_manifest(container)
    changed = sorted(p for p in before if p in after and after[p] != before[p])
    removed = sorted(p for p in before if p not in after)
    if changed or removed:
        raise ResolverError(f"offline replay modified verified cache files: {(changed + removed)[:10]}", "replay-cache-mutated")
    receipt("offline-replay-complete", installed=len(installed))
    return {
        **evidence,
        "network_mode": mode,
        "argv": argv,
        "returncode": 0,
        "installed_packages": len(installed),
        "new_cache_files": sorted(p for p in after if p not in before)[:50],
        "output_tail": tail,
    }


def container_cache_manifest(container) -> dict[str, str]:
    out = container.exec(["sh", "-c", 'cd "$1" && find . -type f -exec sha256sum {} +', "sh", CACHE]).stdout
    manifest = {}
    for line in out.splitlines():
        digest, sep, name = line.partition("  ")
        if not sep or not SHA256_RE.match(digest) or not name.startswith("./"):
            raise ResolverError("container reported an unsafe cache file name", "unsafe-cache")
        manifest[name[2:]] = digest
    return manifest


# --------------------------------------------------------------------------
# Verify locked archives on the host
# --------------------------------------------------------------------------
def fetch_archives(request: dict, plan: dict, scratch: Path, fetch) -> list[dict]:
    limits, pins = request["limits"], request["pinned_sha256"]
    if len(plan["packages"]) > limits["max_archives"]:
        raise ResolverError("too many locked archives", "limit-exceeded")
    if pins:
        missing = [f"{p['name']}@{p['version']}" for p in plan["packages"] if f"{p['name']}@{p['version']}" not in pins]
        unknown = sorted(set(pins) - {f"{p['name']}@{p['version']}" for p in plan["packages"]})
        if missing or unknown:
            raise ResolverError(f"pinned_sha256 must cover exactly the locked archives (missing {missing[:5]}, unknown {unknown[:5]})", "invalid-request")
    total, out = 0, []
    for package in plan["packages"]:
        label = f"{package['name']}@{package['version']}"
        target = scratch / f"{len(out)}.bin"
        digest = fetch(package["url"], target, min(limits["max_archive_bytes"], limits["max_total_bytes"] - total))
        size = target.stat().st_size if target.exists() else 0
        total += size
        if total > limits["max_total_bytes"]:
            raise ResolverError("total download limit exceeded", "limit-exceeded")
        if package["shasum"] and target.exists() and file_digests(target)["sha1"] != package["shasum"]:
            raise ResolverError(f"{label}: downloaded bytes differ from the lock shasum", "lock-shasum-mismatch")
        if pins and pins[label] != digest:
            raise ResolverError(f"{label}: sha256 {digest} differs from pinned_sha256", "pinned-sha256-mismatch")
        target.unlink(missing_ok=True)
        out.append({**package, "sha256": digest, "size": size})
    return out


def bind_or_explain(cache: dict[str, str], archives: list[dict], accepted: list[dict]) -> dict:
    try:
        return bind_cache(cache, archives, accepted)
    except ResolverError as exc:
        if exc.code == "cache-missing-artifact":
            raise ResolverError(f"{exc} (unstable-dist-bytes if the URL serves different bytes than Composer cached)", "unstable-dist-bytes") from None
        raise


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------
def build_result(request: dict, plan: dict, bound: dict, authoring: dict, replay: dict, files: dict[str, bytes], proof: bool, receipts: list[dict]) -> dict:
    verified = bool(proof)
    warnings = []
    if plan["plugins"]:
        warnings.append("composer-plugin packages are locked; they ran only in the offline replay: " + ", ".join(plan["plugins"]))
    if bound["accepted_unmapped"]:
        warnings.append("accepted_unmapped_cache files were reviewed and are carried in the lock")
    if not verified:
        warnings.append("offline replay did not run in a real Docker container: NOT verified (mock/fake containers are not runtime proof)")
    argv_info = request["argv_info"]
    return {
        "id": request["id"],
        "verification": "offline-replay-verified" if verified else "unproven-no-real-offline-replay",
        "lock": {
            "schema_version": 1,
            "kind": "composer-cache",
            "cache_dir_layout": f"relative paths under the Composer cache dir (COMPOSER_CACHE_DIR); archives under {FILES_DIR}/",
            "artifacts": [{k: a[k] for k in ("path", "urls", "sha256")} for a in bound["artifacts"]],
            "unmapped_files": bound["accepted_unmapped"],
            **({"generated_files": bound["generated_files"]} if bound["generated_files"] else {}),
        },
        "offline": {
            "placeholders": {
                "MANGO_COMPOSER_CACHE": "absolute path of a fresh WRITABLE copy of the fetched cache (never /nix/store)",
                "MANGO_HOME": "fresh writable HOME for the build",
            },
            "network": "none",
            "env": {**request["env"], "HOME": "${MANGO_HOME}", "COMPOSER_HOME": "${MANGO_HOME}/.composer", "COMPOSER_CACHE_DIR": "${MANGO_COMPOSER_CACHE}"},
            "argv": list(request["composer_argv"]),
            "inputs": {"composer.json": sha256_hex(files["composer.json"]), "composer.lock": sha256_hex(files["composer.lock"])},
            "verified_by_offline_replay": verified,
            "qualified": False,
            "vendor_output_bundled": False,
            "limitations": [
                "authoring evidence only; Mango's own offline build must still succeed",
                "bytes at each URL were compared once, at authoring time",
                "only packages installed by these exact flags are covered",
                "final vendor/ output is never bundled; it is rebuilt offline from this cache",
            ],
        },
        "package_request": {k: request[k] for k in ("base", "prepared_image", "platform", "php_version", "composer", "composer_argv", "env", "limits", "pinned_sha256", "accept_unmapped_cache", "required_php_extensions")},
        "evidence": {
            "resolver_version": RESOLVER_VERSION,
            "prepared_image": request["prepared_image"],
            "runtime_image": request["prepared_image"]["id"],
            "toolchain": {
                k: authoring[k]
                for k in ("php_version", "php_extensions", "php_extensions_sha256", "composer_version", "composer_sha256", "composer_source")
            },
            "authoring_argv": authoring["authoring_argv"],
            "offline_replay": {k: v for k, v in replay.items() if k != "output_tail"} | {"output_tail": replay["output_tail"]},
            "lock_content_hash": plan["content_hash"],
            "no_dev": argv_info["no_dev"],
            "excluded_dev_packages": len(plan["excluded_dev"]),
            "metapackages": plan["metapackages"],
            "plugins": plan["plugins"],
            "archives": [{k: a[k] for k in ("path", "sha256", "size", "package", "version", "urls")} for a in bound["artifacts"]],
            "archive_count": len(bound["artifacts"]),
            "generated_files": bound["generated_files"],
            "cache_inventory": sorted(
                [{"path": a["path"], "sha256": a["sha256"], "kind": "archive"} for a in bound["artifacts"]]
                + [{"path": g["path"], "sha256": g["sha256"], "kind": "generated"} for g in bound["generated_files"]]
                + [{"path": u["path"], "sha256": u["sha256"], "kind": "accepted-unmapped"} for u in bound["accepted_unmapped"]],
                key=lambda e: e["path"],
            ),
            "warnings": warnings,
            "receipts": receipts,
        },
    }


def resolve(
    request: dict,
    fetch: Callable[[str, Path, int], str] = download,
    container_factory: Callable[..., object] = Container,
    on_receipt: Callable[[dict], None] | None = None,
) -> dict:
    require_remote_author()
    request = validate_request(request)
    limits = request["limits"]
    receipts: list[dict] = []

    def receipt(stage: str, **data) -> None:
        entry = {"id": request["id"], "stage": stage, **json.loads(json.dumps(data, default=str))}
        receipts.append(entry)
        if on_receipt:
            on_receipt(entry)

    composer_json = read_input(request["source_dir"], "composer.json", limits["max_input_bytes"])
    lock_raw = read_input(request["source_dir"], "composer.lock", limits["max_input_bytes"])
    screened = screen_composer_json(composer_json)
    plan = analyze_lock(lock_raw, request["argv_info"], screened["github_vcs"])
    if not plan["packages"]:
        raise ResolverError("the lock installs no dist archives with these flags", "empty-inventory")
    phar = read_phar(request["composer"], limits["max_input_bytes"])
    files = {"composer.json": composer_json, "composer.lock": lock_raw}
    receipt(
        "request-validated",
        inputs={k: sha256_hex(v) for k, v in files.items()},
        packages=len(plan["packages"]),
        prepared_image=request["prepared_image"],
        script_events=screened["script_events"],
    )
    prepared_id = request["prepared_image"]["id"]
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        with container_factory(prepared_id) as container:
            authoring = author_in_container(request, container, phar, files, work, receipt)
        cache = walk_cache(authoring["cache_dir"], limits)
        scratch = work / "scratch"
        scratch.mkdir()
        archives = fetch_archives(request, plan, scratch, fetch)
        receipt("archives-fetched", count=len(archives), sha256={f"{a['name']}@{a['version']}": a["sha256"] for a in archives})
        bound = bind_or_explain(cache, archives, request["accept_unmapped_cache"])
        receipt(
            "cache-bound", artifacts=len(bound["artifacts"]), generated=len(bound["generated_files"]),
            unmapped=len(bound["accepted_unmapped"]),
        )
        rels = (
            [a["path"] for a in bound["artifacts"]]
            + [g["path"] for g in bound["generated_files"]]
            + [u["path"] for u in bound["accepted_unmapped"]]
        )
        with container_factory(prepared_id, "none") as replay_container:
            replay = replay_offline(request, replay_container, phar, files, authoring["cache_dir"], rels, plan, receipt)
            if replay["php_extensions_sha256"] != authoring["php_extensions_sha256"]:
                raise ResolverError("PHP extensions differ between authoring and replay containers", "toolchain-mismatch")
            proof = getattr(replay_container, "PROOF", None) == "docker"
    return build_result(request, plan, bound, authoring, replay, files, proof, receipts)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def request_signature(request: dict) -> str:
    """Hash of the request, the input files it names, and the resolver version."""
    payload: dict = {"request": request, "resolver": RESOLVER_VERSION}
    try:
        valid = validate_request(request)
        payload["inputs"] = {
            name: sha256_hex(read_input(valid["source_dir"], name, valid["limits"]["max_input_bytes"]))
            for name in ("composer.json", "composer.lock")
        }
    except (ResolverError, OSError, TypeError) as exc:
        payload["inputs"] = f"unreadable:{getattr(exc, 'code', 'os')}"
    return sha256_hex(json.dumps(payload, sort_keys=True, default=str).encode())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mango-lock-composer", description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("requests", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, help="JSONL stage receipts (default: <output>.receipts.jsonl)")
    parser.add_argument("--retry-failures", action="store_true")
    args = parser.parse_args(argv)
    try:
        require_remote_author()
    except ResolverError as exc:
        parser.error(str(exc))
    receipts_path = args.receipts or args.output.with_suffix(args.output.suffix + ".receipts.jsonl")
    receipts_path.parent.mkdir(parents=True, exist_ok=True)

    def on_receipt(entry: dict) -> None:
        with receipts_path.open("a") as stream:
            stream.write(json.dumps(entry, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    requests = json.loads(args.requests.read_text())["requests"]
    results = json.loads(args.output.read_text()) if args.output.exists() else {"results": {}}
    for request in requests:
        key = request.get("id") if isinstance(request, dict) else None
        if not isinstance(key, str):
            parser.error("every request needs a string id")
        signature = request_signature(request)
        previous = results["results"].get(key, {})
        if previous.get("request_sha256") == signature and (previous.get("status") == "completed" or not args.retry_failures):
            continue
        try:
            result = resolve(request, on_receipt=on_receipt)
            result["status"] = "completed"
        except Exception as exc:
            result = {"id": key, "status": "failed", "reason_code": getattr(exc, "code", "unexpected"), "error": redact(str(exc))}
            on_receipt({"id": key, "stage": "failed", "reason_code": result["reason_code"]})
        result["request_sha256"] = signature
        results["results"][key] = result
        atomic_json(args.output, results)
        print(json.dumps({"id": key, "status": result["status"], "verification": result.get("verification")}), flush=True)
    return int(any(r["status"] != "completed" for r in results["results"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
