#!/usr/bin/env python3
"""Resolve Go module dependencies to a hash-pinned module cache, REMOTE HOST ONLY.

Authoring-time resolver. Its output feeds Mango's runtime *offline* build through
``inputs.lock.json`` (``fetchLockedInputs``); it neither translates nor reproduces
the original image. Run it only on the remote authoring host with
``MANGO_REMOTE_AUTHOR=1`` and Docker there. Nothing here is a runtime proof: the
offline consumption settings in the result are derived from Go's documented
module-cache layout and have to be exercised by the offline Mango build itself.

Request file::

    {"requests": [{
        "id": "casdoor",
        "base": "golang@sha256:<64 hex>",     # immutable linux/amd64 reviewed base
        "goproxy": "https://proxy.golang.org", # explicit public proxy, one https URL
        # Exactly one module source:
        "modules": [{"dir": ".", "go_mod": "<exact text>", "go_sum": "<exact text>"}],
        # or a READ-ONLY directory on the remote host (only go.mod/go.sum are read):
        "source_path": "/srv/sources/casdoor", "module_dirs": ["."],
        # optional
        "scope": "main",                # "main" = `go mod download`; "all" adds `all`
        "gosumdb": "sum.golang.org",    # kept ON during authoring
        "graph_mod_files": true,        # also lock go.sum-listed ``/go.mod`` files
        "allow_missing_graph_mods": false,
        "limits": {"max_zip_bytes": 536870912, "max_total_bytes": 4294967296}
    }]}

What happens, per request:

1. The request is validated and statically screened (no network): local/unversioned
   ``replace`` directives, ``go.work``, a ``go``/``toolchain`` directive newer
   than the base's Go, and malformed input all fail closed with a specific code.
2. A disposable root container runs the pinned base with ``--platform
   linux/amd64``. Only ``go.mod``/``go.sum`` are copied in (never source or binaries)
   and the *base's own* ``go`` runs ``go mod download -json`` with ``GOTOOLCHAIN=local``,
   ``GOPROXY=<the one https URL>`` (no ``direct``/``off``), ``GOENV=off``, ``GOWORK=off``,
   ``GOFLAGS=``, and every private/insecure knob cleared. No ``go get``, no upgrade:
   module selection is exactly what the original ``go.mod`` and ``go.sum`` select.
3. Every ``.zip``/``.mod``/``.info`` Go cached is downloaded again by the HOST from the
   proxy URL implied by the proxy protocol (bounded, https only). The bytes' SHA-256
   becomes the lock hash, and must equal the SHA-256 of the file in the container's
   cache. For ``.info`` only, full JSON equality also accepts Go's field reordering;
   duplicate fields are rejected and the fetched proxy bytes remain hash-pinned.
4. Independently of Go, the Go ``h1:`` dirhash of each zip and ``.mod`` is computed and
   compared with ``go.sum`` (and with Go's own ``Sum``/``GoModSum``). The ``h1:`` value
   is a hash of per-file SHA-256 lines, NOT the SHA-256 of the file; the lock's
   ``sha256`` is always the plain SHA-256 of the downloaded bytes.
5. ``go.mod``/``go.sum`` are hashed before and after (in the container and, for
   ``source_path``, on the host); any change fails the request.

Limitations (documented, not hidden): modules only reachable through packages that
``go mod download`` does not enumerate are not covered (Go >= 1.17 ``go.mod`` files
list everything needed; older ones are walked transitively); ``@v/list`` is not
produced so version *queries* (``@latest``) do not work offline, only exact pins;
``vendor/`` directories are not used or checked; cgo/system packages are out of
scope; an offline build is not run here.

Results are checkpointed after every request, like remote_python_deps.py.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
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
import zipfile
from pathlib import Path
from typing import Callable

RESOLVER_VERSION = "2"
REQUEST_KEYS = {
    "id",
    "base",
    "goproxy",
    "modules",
    "source_path",
    "module_dirs",
    "scope",
    "gosumdb",
    "graph_mod_files",
    "allow_missing_graph_mods",
    "limits",
    "platform",
}
MODULE_KEYS = {"dir", "go_mod", "go_sum"}
BASE_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
H1_RE = re.compile(r"^h1:[A-Za-z0-9+/]{43}=$")
SUMDB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9](/[A-Za-z0-9._-]+)*$")
MODPATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]*(/[A-Za-z0-9._~-]+)*$")
VERSION_RE = re.compile(
    r"^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$"
)
DIR_RE = re.compile(
    r"^(\.|[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)*)$"
)
GO_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:(?:\.(\d+))?|(alpha|beta|rc)(\d+))$")
WORK = "/tmp/mango-go-deps"
MODCACHE = f"{WORK}/modcache"

DEFAULT_LIMITS = {
    "max_zip_bytes": 512 << 20,
    "max_mod_bytes": 16 << 20,
    "max_info_bytes": 1 << 20,
    "max_go_sum_bytes": 64 << 20,
    "max_total_bytes": 4 << 30,
    "max_artifacts": 6000,
    "max_zip_entries": 200_000,
    "max_zip_unpacked_bytes": 2 << 30,
}
HARD_LIMITS = {
    "max_zip_bytes": 1 << 30,
    "max_mod_bytes": 16 << 20,
    "max_info_bytes": 1 << 20,
    "max_go_sum_bytes": 64 << 20,
    "max_total_bytes": 16 << 30,
    "max_artifacts": 20_000,
    "max_zip_entries": 500_000,
    "max_zip_unpacked_bytes": 4 << 30,
}


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


PROGRESS: Callable[[str], None] = _progress


class ResolverError(ValueError):
    """A request or resolution result this resolver refuses; ``code`` says why."""

    def __init__(self, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# Gate, redaction
# --------------------------------------------------------------------------
def require_remote_author() -> None:
    if os.environ.get("MANGO_REMOTE_AUTHOR") != "1":
        raise ResolverError(
            "Set MANGO_REMOTE_AUTHOR=1 only on the remote authoring host", "gate"
        )


_URL_AUTH_RE = re.compile(r"(://)[^/@\s]+@")
_URL_QUERY_RE = re.compile(r"(https?://[^\s?#]+)[?#][^\s]*")


def redact(text: str) -> str:
    """Strip URL userinfo, query strings and fragments from diagnostic text."""
    return _URL_QUERY_RE.sub(r"\1?<redacted>", _URL_AUTH_RE.sub(r"\1<redacted>@", text))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# Go versions, go.mod and go.sum (pure)
# --------------------------------------------------------------------------
def version_key(version: str) -> tuple:
    """Order Go versions: 1.21 < 1.21rc1 < 1.21.0 < 1.21.1 (``go`` line semantics)."""
    match = GO_VERSION_RE.match(version)
    if not match:
        raise ResolverError(f"unparseable Go version {version!r}", "go-version")
    major, minor, patch, pre, prenum = match.groups()
    if pre:
        return (
            int(major),
            int(minor),
            1,
            ("alpha", "beta", "rc").index(pre),
            int(prenum),
        )
    if patch is not None:
        return (int(major), int(minor), 2, int(patch), 0)
    return (int(major), int(minor), 0, 0, 0)


def local_go_version(goversion: str) -> str:
    """``go1.26.10`` (``go env GOVERSION``) -> ``1.26.10``; reject devel builds."""
    match = re.match(r"^go(\d+\.\d+(?:\.\d+|(?:alpha|beta|rc)\d+)?)(?:\s|$)", goversion)
    if not match:
        raise ResolverError(f"unsupported Go build {goversion!r}", "go-version")
    return match.group(1)


def _strip_comment(line: str) -> str:
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in '"`':
            quote = ch
        elif line.startswith("//", i):
            return line[:i]
    return line


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in '"`':
        return token[1:-1]
    return token


def parse_go_mod(data: bytes) -> dict:
    """Extract the directives this resolver screens; not a full go.mod parser."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ResolverError("go.mod is not UTF-8", "invalid-go-mod") from None
    parsed: dict = {
        "module": None,
        "go": None,
        "toolchain": None,
        "requires": [],
        "replaces": [],
    }
    block = None
    for raw in text.splitlines():
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if block:
            if line == ")":
                block = None
                continue
            directive, tokens = block, line.split()
        else:
            tokens = line.split()
            directive = tokens[0]
            tokens = tokens[1:]
            if tokens and tokens[0] == "(":
                block = directive
                continue
        tokens = [_unquote(t) for t in tokens]
        if directive == "module" and tokens:
            parsed["module"] = tokens[0]
        elif directive in ("go", "toolchain") and tokens:
            parsed[directive] = tokens[0]
        elif directive == "require" and len(tokens) >= 2:
            parsed["requires"].append((tokens[0], tokens[1]))
        elif directive == "replace":
            if "=>" not in tokens or tokens.index("=>") == len(tokens) - 1:
                raise ResolverError("malformed replace directive", "invalid-go-mod")
            arrow = tokens.index("=>")
            parsed["replaces"].append(
                {"old": " ".join(tokens[:arrow]), "new": tokens[arrow + 1 :]}
            )
    return parsed


def is_local_path(target: str) -> bool:
    return (
        target.startswith(("./", "../", "/", "~"))
        or target in (".", "..")
        or bool(re.match(r"^[A-Za-z]:[\\/]", target))
        or "\\" in target
    )


def screen_go_mod(parsed: dict, go_version: str | None, where: str) -> None:
    """Fail closed on inputs this resolver cannot reproduce faithfully."""
    for replace in parsed["replaces"]:
        target = replace["new"][0]
        if is_local_path(target) or len(replace["new"]) == 1:
            raise ResolverError(
                f"{where}: replace {replace['old']} => local path {target!r}; "
                "local/replaced source modules are not supported",
                "go-mod-replace-local",
            )
        old = replace["old"].split()
        if len(replace["new"]) != 2 or len(old) not in (1, 2):
            raise ResolverError(
                f"{where}: malformed versioned module replacement", "go-mod-replace"
            )
        check_module(old[0], old[1] if len(old) == 2 else "v0.0.0")
        check_module(target, replace["new"][1])
        # Keep the original directive unchanged. Go selects and reports the
        # replacement Path/Version; the normal cache/proxy/original-go.sum
        # checks below must all pass for those actual replacement bytes.
    if go_version is None:
        return
    for key, label in (("go", "go"), ("toolchain", "toolchain")):
        wanted = parsed.get(key)
        if not wanted:
            continue
        wanted = (
            wanted[2:] if key == "toolchain" and wanted.startswith("go") else wanted
        )
        if key == "toolchain" and wanted == "default":
            continue
        try:
            newer = version_key(wanted) > version_key(go_version)
        except ResolverError:
            raise ResolverError(
                f"{where}: unparseable {label} directive {wanted!r}", "invalid-go-mod"
            ) from None
        if newer:
            raise ResolverError(
                f"{where}: {label} {wanted} is newer than the base's go{go_version}; "
                "automatic Go toolchain download is not supported "
                "(use a base image with that toolchain)",
                "toolchain-newer-than-base",
            )


def parse_go_sum(data: bytes) -> dict[tuple[str, str, bool], set[str]]:
    """{(module, version, is_gomod): {h1...}} from go.sum; non-h1 hashes ignored."""
    sums: dict[tuple[str, str, bool], set[str]] = {}
    for number, raw in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
        if not raw.strip():
            continue
        fields = raw.split()
        if len(fields) != 3:
            raise ResolverError(f"go.sum line {number}: malformed", "invalid-go-sum")
        module, version, digest = fields
        is_mod = version.endswith("/go.mod")
        if is_mod:
            version = version[: -len("/go.mod")]
        if digest.startswith("h1:"):
            if not H1_RE.match(digest):
                raise ResolverError(
                    f"go.sum line {number}: malformed h1 hash", "invalid-go-sum"
                )
            sums.setdefault((module, version, is_mod), set()).add(digest)
    return sums


# --------------------------------------------------------------------------
# Go module cache naming and dirhash (pure)
# --------------------------------------------------------------------------
def escape(value: str) -> str:
    """Go module path/version escaping: upper-case letters become ``!`` + lower."""
    return "".join("!" + c.lower() if "A" <= c <= "Z" else c for c in value)


def check_module(path: object, version: object) -> tuple[str, str]:
    if (
        not isinstance(path, str)
        or not MODPATH_RE.match(path)
        or any(part in ("", ".", "..") for part in path.split("/"))
    ):
        raise ResolverError(f"unsafe module path {path!r}", "invalid-module")
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise ResolverError(
            f"{path}: unsafe module version {version!r}", "invalid-module"
        )
    return path, version


def cache_relpath(path: str, version: str, ext: str) -> str:
    return f"cache/download/{escape(path)}/@v/{escape(version)}.{ext}"


def proxy_url(proxy: str, path: str, version: str, ext: str) -> str:
    return f"{proxy}/{escape(path)}/@v/{escape(version)}.{ext}"


def dirhash_h1(entries: list[tuple[str, str]]) -> str:
    """Go ``dirhash.Hash1`` from (name, file-SHA-256-hex) pairs.

    ``h1:`` is base64(SHA-256(sorted lines "<hex>  <name>\\n")): a hash of hashes,
    never the SHA-256 of an archive.
    """
    lines = []
    for name, digest in sorted(entries):
        if "\n" in name:
            raise ResolverError("file name contains newline", "bad-zip")
        lines.append(f"{digest}  {name}\n")
    summary = hashlib.sha256("".join(lines).encode()).digest()
    return "h1:" + base64.b64encode(summary).decode()


def h1_gomod(data: bytes) -> str:
    return dirhash_h1([("go.mod", sha256_hex(data))])


def h1_zip(path: Path, module: str, version: str, limits: dict) -> str:
    prefix = f"{module}@{version}/"
    entries, seen, unpacked = [], set(), 0
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > limits["max_zip_entries"]:
                raise ResolverError(
                    f"{module}@{version}: too many zip entries", "limit-exceeded"
                )
            for info in infos:
                name = info.filename
                if name.endswith("/"):
                    raise ResolverError(
                        f"{module}@{version}: zip has directory entry", "bad-zip"
                    )
                if not name.startswith(prefix) or ".." in name.split("/"):
                    raise ResolverError(
                        f"{module}@{version}: zip entry outside module prefix",
                        "bad-zip",
                    )
                if name in seen:
                    raise ResolverError(
                        f"{module}@{version}: duplicate zip entry", "bad-zip"
                    )
                seen.add(name)
                unpacked += info.file_size
                if unpacked > limits["max_zip_unpacked_bytes"]:
                    raise ResolverError(
                        f"{module}@{version}: zip unpacks beyond limit",
                        "limit-exceeded",
                    )
                digest = hashlib.sha256()
                read = 0
                with archive.open(info) as member:
                    for chunk in iter(lambda: member.read(1 << 20), b""):
                        read += len(chunk)
                        if read > info.file_size:  # header lied; do not trust it
                            raise ResolverError(
                                f"{module}@{version}: corrupt zip", "bad-zip"
                            )
                        digest.update(chunk)
                entries.append((name, digest.hexdigest()))
    except zipfile.BadZipFile as exc:
        raise ResolverError(
            f"{module}@{version}: invalid zip ({exc})", "bad-zip"
        ) from None
    return dirhash_h1(entries)


# --------------------------------------------------------------------------
# Request validation (pure)
# --------------------------------------------------------------------------
def check_goproxy(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ResolverError("goproxy must be an explicit https URL", "invalid-goproxy")
    if any(token in value for token in (",", "|")) or value in ("direct", "off"):
        raise ResolverError(
            "goproxy must be ONE https URL: no lists, no direct/off fallback",
            "invalid-goproxy",
        )
    parts = urllib.parse.urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise ResolverError("goproxy must be an https URL", "invalid-goproxy")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ResolverError("goproxy must not carry credentials", "invalid-goproxy")
    if parts.query or parts.fragment:
        raise ResolverError(
            "goproxy must not have a query or fragment", "invalid-goproxy"
        )
    host = parts.hostname.lower()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if (
        ip is not None
        or host == "localhost"
        or "." not in host
        or host.endswith((".local", ".internal", ".localhost", ".lan", ".corp"))
    ):
        raise ResolverError(
            "goproxy must be a public DNS host (no IP literals or private names)",
            "invalid-goproxy",
        )
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path.rstrip("/"), "", "")
    )


def _check_dir(value: object) -> str:
    if (
        not isinstance(value, str)
        or not DIR_RE.match(value)
        or ".." in value.split("/")
    ):
        raise ResolverError(f"unsafe module dir {value!r}", "invalid-request")
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
    for key in ("id", "base", "goproxy"):
        if key not in request:
            raise ResolverError(f"missing request key: {key}", "invalid-request")
    if not isinstance(request["id"], str) or not ID_RE.match(request["id"]):
        raise ResolverError("id must match " + ID_RE.pattern, "invalid-request")
    if not isinstance(request["base"], str) or not BASE_RE.match(request["base"]):
        raise ResolverError(
            "base must be pinned as <image>@sha256:<64 hex>", "invalid-base"
        )
    if request.get("platform", "linux/amd64") != "linux/amd64":
        raise ResolverError("only linux/amd64 bases are supported", "base-platform")
    normal = dict(request)
    normal["platform"] = "linux/amd64"
    normal["goproxy"] = check_goproxy(request["goproxy"])
    scope = request.get("scope", "main")
    if scope not in ("main", "all"):
        raise ResolverError("scope must be 'main' or 'all'", "invalid-request")
    normal["scope"] = scope
    gosumdb = request.get("gosumdb", "sum.golang.org")
    if not isinstance(gosumdb, str) or not SUMDB_RE.match(gosumdb) or gosumdb == "off":
        raise ResolverError(
            "gosumdb must be a sum database name (not off)", "invalid-request"
        )
    normal["gosumdb"] = gosumdb
    for key, default in (
        ("graph_mod_files", True),
        ("allow_missing_graph_mods", False),
    ):
        normal[key] = request.get(key, default)
        if not isinstance(normal[key], bool):
            raise ResolverError(f"{key} must be a boolean", "invalid-request")
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
    has_inline, has_path = "modules" in request, "source_path" in request
    if has_inline == has_path:
        raise ResolverError(
            "give exactly one of modules (inline) or source_path (remote read-only dir)",
            "invalid-request",
        )
    if has_inline:
        if "module_dirs" in request:
            raise ResolverError(
                "module_dirs only goes with source_path", "invalid-request"
            )
        modules = request["modules"]
        if not isinstance(modules, list) or not modules:
            raise ResolverError("modules must be a non-empty list", "invalid-request")
        dirs = []
        for index, module in enumerate(modules):
            if not isinstance(module, dict) or set(module) - MODULE_KEYS:
                raise ResolverError(
                    f"modules[{index}]: unsupported shape", "invalid-request"
                )
            if not isinstance(module.get("go_mod"), str):
                raise ResolverError(
                    f"modules[{index}]: go_mod text required", "invalid-request"
                )
            if not isinstance(module.get("go_sum", ""), str):
                raise ResolverError(
                    f"modules[{index}]: go_sum must be text", "invalid-request"
                )
            dirs.append(_check_dir(module.get("dir", ".")))
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
        dirs = request.get("module_dirs", ["."])
        if not isinstance(dirs, list) or not dirs:
            raise ResolverError(
                "module_dirs must be a non-empty list", "invalid-request"
            )
        dirs = [_check_dir(d) for d in dirs]
    if len(set(dirs)) != len(dirs):
        raise ResolverError("duplicate module dir", "invalid-request")
    return normal


def load_sources(request: dict) -> list[dict]:
    """Exact go.mod/go.sum bytes per module dir. ``source_path`` is read, never written."""
    limits = request["limits"]
    sources = []
    if "modules" in request:
        for module in request["modules"]:
            sources.append(
                {
                    "dir": module.get("dir", "."),
                    "go_mod": module["go_mod"].encode(),
                    "go_sum": module.get("go_sum", "").encode(),
                    "origin": "inline",
                }
            )
    else:
        root = Path(request["source_path"])
        if not root.is_dir() or root.is_symlink():
            raise ResolverError(
                "source_path is not a real directory on this host", "source-missing"
            )
        if (root / "go.work").exists():
            raise ResolverError(
                "go.work present: workspace module selection is not reproduced",
                "go-work",
            )
        for rel in request.get("module_dirs", ["."]):
            base = root / rel
            entry = {"dir": rel, "origin": "source_path"}
            for name, cap, required in (
                ("go.mod", limits["max_mod_bytes"], True),
                ("go.sum", limits["max_go_sum_bytes"], False),
            ):
                file = base / name
                if not os.path.lexists(file):
                    if required:
                        raise ResolverError(f"{rel}/go.mod not found", "source-missing")
                    entry["go_sum"] = b""
                    continue
                stat = file.lstat()
                if not file.is_file() or file.is_symlink():
                    raise ResolverError(
                        f"{rel}/{name} is not a regular file", "source-missing"
                    )
                if stat.st_size > cap:
                    raise ResolverError(
                        f"{rel}/{name} exceeds {cap} bytes", "limit-exceeded"
                    )
                entry["go_mod" if name == "go.mod" else "go_sum"] = file.read_bytes()
            sources.append(entry)
    for source in sources:
        if len(source["go_mod"]) > limits["max_mod_bytes"]:
            raise ResolverError("go.mod too large", "limit-exceeded")
        if len(source["go_sum"]) > limits["max_go_sum_bytes"]:
            raise ResolverError("go.sum too large", "limit-exceeded")
    return sources


def source_digests(sources: list[dict]) -> dict:
    return {
        s["dir"]: {"go.mod": sha256_hex(s["go_mod"]), "go.sum": sha256_hex(s["go_sum"])}
        for s in sources
    }


def request_key(request: dict) -> str:
    return sha256_hex(json.dumps(request, sort_keys=True).encode())[:16]


def lock_prefix(request: dict) -> str:
    return f"go-mod-cache/{request_key(request)}"


# --------------------------------------------------------------------------
# go failure classification (pure)
# --------------------------------------------------------------------------
_CLASSES = [
    (
        "go-mod-needs-update",
        ("updates to go.mod needed", "go: updates to go.sum needed"),
    ),
    ("go-sum-missing", ("missing go.sum entry", "missing go.sum")),
    ("go-sum-mismatch", ("SECURITY ERROR", "checksum mismatch", "verifying module")),
    (
        "toolchain-newer-than-base",
        ("GOTOOLCHAIN=local", "requires go >=", "toolchain not available"),
    ),
    (
        "module-unavailable",
        (
            "404 Not Found",
            "410 Gone",
            "unrecognized import path",
            "terminal prompts disabled",
            "no matching versions",
            "module lookup disabled",
            "git ls-remote",
            "invalid version: unknown revision",
            "reading https://",
        ),
    ),
    (
        "network",
        (
            "dial tcp",
            "no such host",
            "i/o timeout",
            "TLS handshake",
            "connection refused",
        ),
    ),
]


def classify_go_failure(text: str) -> tuple[str, str]:
    for code, needles in _CLASSES:
        if any(n in text for n in needles):
            reason = {
                "go-mod-needs-update": "go tooling wanted to modify go.mod/go.sum (refused)",
                "go-sum-missing": "go.sum lacks entries needed for the selected modules",
                "go-sum-mismatch": "downloaded module bytes do not match go.sum",
                "toolchain-newer-than-base": "needs a newer Go than the base; toolchain downloads are disabled",
                "module-unavailable": (
                    "module not served by the public GOPROXY (private, VCS-only, "
                    "retracted or removed upstream)"
                ),
                "network": "network error reaching the GOPROXY",
            }[code]
            return code, reason
    return "go-download-failed", "go mod download failed"


def parse_download_json(stdout: str) -> list[dict]:
    """``go mod download -json`` prints concatenated objects, not an array."""
    decoder, index, items = json.JSONDecoder(), 0, []
    while True:
        while index < len(stdout) and stdout[index].isspace():
            index += 1
        if index >= len(stdout):
            return items
        try:
            value, index = decoder.raw_decode(stdout, index)
        except json.JSONDecodeError:
            raise ResolverError(
                "go mod download -json output was not JSON", "go-output"
            ) from None
        if not isinstance(value, dict):
            raise ResolverError(
                "go mod download -json item was not an object", "go-output"
            )
        items.append(value)


# --------------------------------------------------------------------------
# Remote execution (Docker and the network, remote host only)
# --------------------------------------------------------------------------
def run(
    command: list[str], check: bool = True, **kwargs
) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if check and result.returncode != 0:
        tail = redact((result.stderr or result.stdout or "")[-2000:])
        raise ResolverError(
            f"{' '.join(command[:3])} failed ({result.returncode}): {tail}", "docker"
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


def download(url: str, destination: Path, max_bytes: int) -> str:
    """Fetch an https ``url`` to ``destination``; return SHA-256; abort past ``max_bytes``."""
    digest, size = hashlib.sha256(), 0
    request = urllib.request.Request(
        url, headers={"User-Agent": "mango-remote-go-deps"}
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
                digest.update(chunk)
                out.write(chunk)
    except urllib.error.HTTPError as exc:
        destination.unlink(missing_ok=True)
        code = "module-unavailable" if exc.code in (404, 410) else "download-failed"
        raise ResolverError(f"HTTP {exc.code} fetching {redact(url)}", code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        destination.unlink(missing_ok=True)
        raise ResolverError(
            f"fetching {redact(url)} failed: {redact(str(exc))}", "download-failed"
        ) from None
    except ResolverError:
        destination.unlink(missing_ok=True)
        raise
    return digest.hexdigest()


class Container:
    """A disposable root container on the pinned linux/amd64 base, removed on exit."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.name = f"mango-godeps-{uuid.uuid4().hex[:12]}"

    def __enter__(self) -> "Container":
        require_remote_author()
        run(
            [
                "docker", "run", "-d", "--rm",
                "--platform", "linux/amd64",
                "--cpus", "1.5", "--memory", "4g", "--pids-limit", "512",
                "--name", self.name,
                "--user", "0:0",
                "--label", "mango.remote-go-deps=1",
                "--entrypoint", "sleep",
                self.base, "infinity",
            ]
        )  # fmt: skip
        return self

    def __exit__(self, *exc) -> None:
        subprocess.run(
            ["docker", "rm", "-f", self.name], capture_output=True, text=True
        )

    def exec(
        self,
        argv: list[str],
        env: dict[str, str] | None = None,
        workdir: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        command = ["docker", "exec"]
        for key, value in (env or {}).items():
            command += ["-e", f"{key}={value}"]
        if workdir:
            command += ["-w", workdir]
        return run([*command, self.name, *argv], check=check)

    def copy_in(self, source: Path, destination: str) -> None:
        run(["docker", "cp", str(source), f"{self.name}:{destination}"])

    def image_platform(self) -> str:
        image = run(
            ["docker", "inspect", "--format", "{{.Image}}", self.name]
        ).stdout.strip()
        return run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Os}}/{{.Architecture}}",
                image,
            ]
        ).stdout.strip()


def go_env(request: dict) -> dict[str, str]:
    """Hermetic Go environment for authoring; everything private/insecure is cleared."""
    return {
        "GOPROXY": request["goproxy"],  # one URL, no ",direct"
        "GOSUMDB": request["gosumdb"],
        "GOTOOLCHAIN": "local",  # never download a toolchain
        "GOENV": "off",  # ignore `go env -w` files baked into the image
        "GOWORK": "off",
        "GOFLAGS": "",
        "GONOPROXY": "",
        "GONOSUMDB": "",
        "GONOSUMCHECK": "",
        "GOPRIVATE": "",
        "GOINSECURE": "",
        "GOMODCACHE": MODCACHE,
        "GOPATH": f"{WORK}/gopath",
        "GOCACHE": f"{WORK}/gocache",
        "HOME": f"{WORK}/home",
        "GIT_TERMINAL_PROMPT": "0",
    }


def container_hashes(container, paths: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for start in range(0, len(paths), 100):
        out = container.exec(["sha256sum", "--", *paths[start : start + 100]]).stdout
        for line in out.splitlines():
            digest, _, name = line.partition("  ")
            if SHA256_RE.match(digest):
                result[name.strip()] = digest
    missing = [p for p in paths if p not in result]
    if missing:
        raise ResolverError(
            f"container file missing or unhashable: {missing[0]}", "cache-mismatch"
        )
    return result


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------
class Budget:
    def __init__(self, limits: dict) -> None:
        self.limits, self.total, self.count = limits, 0, 0

    def room(self, kind: str) -> int:
        self.count += 1
        if self.count > self.limits["max_artifacts"]:
            raise ResolverError("artifact count limit exceeded", "limit-exceeded")
        left = self.limits["max_total_bytes"] - self.total
        if left <= 0:
            raise ResolverError("total download limit exceeded", "limit-exceeded")
        return min(self.limits[f"max_{kind}_bytes"], left)


def fetch_verified(url, kind, work, budget, fetch) -> tuple[str, int]:
    """Download one artifact into ``work``; return (sha256, size). File stays for the caller."""
    destination = work / f"artifact.{kind}"
    destination.unlink(missing_ok=True)
    digest = fetch(url, destination, budget.room(kind))
    size = destination.stat().st_size
    budget.total += size
    if not SHA256_RE.match(digest):
        raise ResolverError("fetcher returned a malformed digest", "download-failed")
    return digest, size


def same_info_json(proxy: bytes, cached: str, version: str) -> bool:
    """Go reorders Origin fields when reserializing proxy .info metadata."""

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    try:
        left = json.loads(proxy, object_pairs_hook=unique_object)
        right = json.loads(cached, object_pairs_hook=unique_object)
        # Python value equality conflates true with 1 and 1 with 1.0; accept
        # object ordering differences without relaxing JSON value types.
        identical = json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
            right, sort_keys=True, allow_nan=False
        )
    except (ValueError, UnicodeError):
        return False
    return isinstance(left, dict) and left.get("Version") == version and identical


def resolve_module_dir(
    container,
    request,
    source,
    index,
    work,
    budget,
    fetch,
    state,
    *,
    directory=None,
    environment=None,
    sums=None,
) -> None:
    """Run go in one module dir and verify/lock everything it cached."""
    limits, proxy = request["limits"], request["goproxy"]
    directory = f"{WORK}/src/{index}" if directory is None else directory
    sums = parse_go_sum(source["go_sum"]) if sums is None else sums
    args = ["go", "mod", "download", "-json"] + (
        ["all"] if request["scope"] == "all" else []
    )
    done = container.exec(
        args,
        env=go_env(request) if environment is None else environment,
        workdir=directory,
        check=False,
    )
    stdout = done.stdout or ""
    items = parse_download_json(stdout) if stdout.strip() else []
    errors = [i for i in items if i.get("Error")]
    if done.returncode != 0 or errors:
        text = "\n".join([str(i.get("Error")) for i in errors] + [done.stderr or ""])
        code, reason = classify_go_failure(text)
        names = ", ".join(
            sorted(f"{i.get('Path')}@{i.get('Version')}" for i in errors)[:5]
        )
        detail = redact(text.strip()[-600:])
        raise ResolverError(
            f"{source['dir']}: {reason}"
            + (f" [{names}]" if names else "")
            + f": {detail}",
            code,
        )
    for item in items:
        path, version = check_module(item.get("Path"), item.get("Version"))
        sum_zip = sums.get((path, version, False))
        sum_mod = sums.get((path, version, True))
        if not sum_zip or not sum_mod:
            raise ResolverError(
                f"go.sum lacks {'zip' if not sum_zip else 'go.mod'} hash for {path}@{version}",
                "go-sum-missing",
            )
        root = f"{MODCACHE}/"
        files = {}
        for ext, field in (("zip", "Zip"), ("mod", "GoMod"), ("info", "Info")):
            reported = item.get(field)
            expected = root + cache_relpath(path, version, ext)
            if reported != expected:  # go's layout must equal ours before we infer URLs
                raise ResolverError(
                    f"{path}@{version}: unexpected cache path for {ext}",
                    "cache-mismatch",
                )
            files[ext] = expected
        digests = container_hashes(container, list(files.values()))
        ziphash_path = root + cache_relpath(path, version, "ziphash")
        ziphash = container.exec(["cat", "--", ziphash_path]).stdout.strip()
        got: dict[str, tuple[str, int]] = {}
        h1 = {}
        for ext in ("zip", "mod", "info"):
            url = proxy_url(proxy, path, version, ext)
            sha, size = fetch_verified(url, ext, work, budget, fetch)
            artifact = work / f"artifact.{ext}"
            normalized_info = (
                ext == "info"
                and sha != digests[files[ext]]
                and same_info_json(
                    artifact.read_bytes(),
                    container.exec(["cat", "--", files[ext]]).stdout,
                    version,
                )
            )
            if sha != digests[files[ext]] and not normalized_info:
                raise ResolverError(
                    f"{path}@{version}: proxy bytes for .{ext} differ from the module cache "
                    "bytes Go used; URL inference rejected",
                    "proxy-bytes-mismatch",
                )
            if ext == "zip":
                h1["zip"] = h1_zip(artifact, path, version, limits)
            elif ext == "mod":
                h1["mod"] = h1_gomod(artifact.read_bytes())
            artifact.unlink()
            got[ext] = (sha, size)
        if h1["zip"] not in sum_zip or h1["mod"] not in sum_mod:
            raise ResolverError(
                f"{path}@{version}: downloaded bytes do not match go.sum h1",
                "go-sum-mismatch",
            )
        if item.get("Sum") not in (None, "", h1["zip"]) or item.get("GoModSum") not in (
            None, "", h1["mod"],
        ):  # fmt: skip
            raise ResolverError(
                f"{path}@{version}: go's reported sum disagrees with the bytes",
                "go-sum-mismatch",
            )
        if ziphash != h1["zip"]:
            raise ResolverError(
                f"{path}@{version}: .ziphash disagrees with zip", "cache-mismatch"
            )
        state["modules"][(path, version)] = {
            "h1_zip": h1["zip"],
            "h1_mod": h1["mod"],
            "files": got,
            "ziphash": ziphash,
        }
    state["graph_sums"].append((source["dir"], sums))


def resolve_graph_mods(request, work, budget, fetch, state) -> None:
    """Lock ``.mod`` files go.sum lists but ``go mod download`` did not need zips for."""
    proxy = request["goproxy"]
    for dirname, sums in state["graph_sums"]:
        for (path, version, is_mod), hashes in sorted(sums.items()):
            if (
                not is_mod
                or (path, version) in state["modules"]
                or (path, version) in state["graph"]
            ):
                continue
            try:
                check_module(path, version)
                sha, size = fetch_verified(
                    proxy_url(proxy, path, version, "mod"), "mod", work, budget, fetch
                )
                artifact = work / "artifact.mod"
                computed = h1_gomod(artifact.read_bytes())
                artifact.unlink()
                if computed not in hashes:
                    raise ResolverError(
                        f"{path}@{version}: .mod bytes do not match go.sum h1",
                        "go-sum-mismatch",
                    )
            except ResolverError as exc:
                if (
                    exc.code in ("module-unavailable",)
                    and request["allow_missing_graph_mods"]
                ):
                    state["missing_graph"].append(f"{path}@{version}")
                    continue
                if exc.code == "module-unavailable":
                    raise ResolverError(
                        f"{dirname}: go.sum lists {path}@{version}/go.mod but the proxy does not "
                        "serve it (set allow_missing_graph_mods to accept; offline module-graph "
                        "loading may then fail)",
                        "graph-mod-unavailable",
                    ) from None
                raise
            state["graph"][(path, version)] = {
                "h1_mod": computed,
                "sha256": sha,
                "size": size,
            }


def build_result(request, state, digests, evidence) -> dict:
    prefix = lock_prefix(request)
    proxy = request["goproxy"]
    artifacts, metadata = [], []
    for (path, version), info in state["modules"].items():
        for ext, (sha, _) in info["files"].items():
            artifacts.append(
                {
                    "path": f"{prefix}/{cache_relpath(path, version, ext)}",
                    "urls": [proxy_url(proxy, path, version, ext)],
                    "sha256": sha,
                }
            )
        content = info["ziphash"]  # Go writes the h1 text without a newline
        metadata.append(
            {
                "path": f"{prefix}/{cache_relpath(path, version, 'ziphash')}",
                "content": content,
                "sha256": sha256_hex(content.encode()),
            }
        )
    for (path, version), info in state["graph"].items():
        artifacts.append(
            {
                "path": f"{prefix}/{cache_relpath(path, version, 'mod')}",
                "urls": [proxy_url(proxy, path, version, "mod")],
                "sha256": info["sha256"],
            }
        )
    artifacts.sort(key=lambda a: a["path"])
    metadata.sort(key=lambda m: m["path"])
    root = "${MANGO_GO_CACHE}"
    common = {
        "GOTOOLCHAIN": "local",
        "GOWORK": "off",
        "GOENV": "off",
        "GOFLAGS": "-mod=readonly",
    }
    return {
        "id": request["id"],
        "lock": {"schema_version": 1, "artifacts": artifacts},
        "cache": {
            "prefix": prefix,
            "download_root": f"{prefix}/cache/download",
            "metadata": metadata,
        },
        "offline": {
            "placeholder": "MANGO_GO_CACHE = absolute path of the writable copy of `prefix`",
            "proxy_mode": {
                "env": {
                    **common,
                    "GOPROXY": f"file://{root}/cache/download",
                    "GOSUMDB": "off",
                },
                "note": (
                    "Preferred. go.sum stays the integrity authority (build runs with "
                    "-mod=readonly and the ORIGINAL go.mod/go.sum); GOSUMDB=off only because "
                    "no sum database is reachable offline; Mango's SHA-256 lock hashes guard "
                    "the cache bytes."
                ),
            },
            "modcache_mode": {
                "env": {
                    **common,
                    "GOMODCACHE": root,
                    "GOPROXY": "off",
                    "GOSUMDB": "off",
                },
                "note": "Go re-extracts zips from cache/download; .ziphash metadata is required.",
            },
            "source_files_sha256": digests,
            "verified_by_offline_build": False,
            "limitations": [
                "authoring evidence only; an offline Mango build must still succeed",
                "exact pinned versions only: no @v/list, so version queries fail offline",
                "modules not enumerated by `go mod download` for this go.mod are absent",
                "vendor/ is neither used nor checked",
            ],
        },
        "package_request": {
            k: request[k]
            for k in ("base", "platform", "goproxy", "gosumdb", "scope", "limits")
        }
        | {"module_dirs": sorted(digests)},
        "evidence": evidence
        | {
            "modules": sorted(
                (
                    {
                        "module": p,
                        "version": v,
                        "zip_sha256": i["files"]["zip"][0],
                        "mod_sha256": i["files"]["mod"][0],
                        "info_sha256": i["files"]["info"][0],
                        "h1_zip": i["h1_zip"],
                        "h1_mod": i["h1_mod"],
                    }
                    for (p, v), i in state["modules"].items()
                ),
                key=lambda m: (m["module"], m["version"]),
            ),
            "graph_mod_files": len(state["graph"]),
            "missing_graph_mods": sorted(state["missing_graph"]),
            "note": "h1 values are Go dirhashes, not file SHA-256; lock sha256 is of downloaded bytes",
            "total_bytes": state["bytes"],
        },
    }


def resolve(
    request: dict,
    fetch: Callable[[str, Path, int], str] = download,
    container_factory: Callable[[str], object] = Container,
) -> dict:
    require_remote_author()
    request = validate_request(request)
    sources = load_sources(request)
    before = source_digests(sources)
    for source in sources:
        screen_go_mod(parse_go_mod(source["go_mod"]), None, f"{source['dir']}/go.mod")
    state = {
        "modules": {}, "graph": {}, "graph_sums": [], "missing_graph": [], "bytes": 0,
    }  # fmt: skip
    budget = Budget(request["limits"])
    with (
        container_factory(request["base"]) as container,
        tempfile.TemporaryDirectory() as tmp,
    ):
        work = Path(tmp)
        platform = container.image_platform()
        if platform != "linux/amd64":
            raise ResolverError(
                f"base resolves to {platform}, not linux/amd64", "base-platform"
            )
        container.exec(
            [
                "mkdir",
                "-p",
                MODCACHE,
                f"{WORK}/gopath",
                f"{WORK}/gocache",
                f"{WORK}/home",
            ]
        )
        probe = container.exec(
            ["go", "env", "-json", "GOVERSION", "GOOS", "GOARCH"],
            env=go_env(request),
            workdir="/",
            check=False,
        )
        if probe.returncode != 0:
            raise ResolverError("base image has no usable go toolchain", "go-missing")
        info = json.loads(probe.stdout)
        if (info.get("GOOS"), info.get("GOARCH")) != ("linux", "amd64"):
            raise ResolverError("base go is not linux/amd64", "base-platform")
        go_version = local_go_version(info["GOVERSION"])
        for source in sources:
            screen_go_mod(
                parse_go_mod(source["go_mod"]), go_version, f"{source['dir']}/go.mod"
            )
        in_container = []
        for index, source in enumerate(sources):
            directory = f"{WORK}/src/{index}"
            container.exec(["mkdir", "-p", directory])
            for name, key in (("go.mod", "go_mod"), ("go.sum", "go_sum")):
                local = work / f"{index}-{name}"
                local.write_bytes(source[key])
                local.chmod(0o600)
                container.copy_in(local, f"{directory}/{name}")
                local.unlink()
                in_container.append(f"{directory}/{name}")
        staged = container_hashes(container, in_container)
        for index, source in enumerate(sources):
            directory = f"{WORK}/src/{index}"
            for name, key in (("go.mod", "go_mod"), ("go.sum", "go_sum")):
                if staged[f"{directory}/{name}"] != sha256_hex(source[key]):
                    raise ResolverError(
                        "go.mod/go.sum changed during copy", "source-mutated"
                    )
        for index, source in enumerate(sources):
            PROGRESS(f"{request['id']}: module dir {source['dir']}")
            resolve_module_dir(
                container, request, source, index, work, budget, fetch, state
            )
        after_container = container_hashes(container, in_container)
        if after_container != staged:
            raise ResolverError(
                "go tooling modified go.mod or go.sum in the container (original selection "
                "was not preserved)",
                "go-mod-mutated",
            )
        if request["graph_mod_files"]:
            resolve_graph_mods(request, work, budget, fetch, state)
        state["bytes"] = budget.total
        go_version_text = container.exec(
            ["go", "version"], env=go_env(request), workdir="/"
        ).stdout
    if "source_path" in request:
        after = source_digests(load_sources(request))
        if after != before:
            raise ResolverError(
                "source go.mod/go.sum changed while resolving", "source-mutated"
            )
    evidence = {
        "go_version": redact(go_version_text.strip()),
        "go_env": {
            k: v
            for k, v in go_env(request).items()
            if k.startswith("GO") and k != "GOPATH"
        },
        "base_platform": "linux/amd64",
        "resolver_version": RESOLVER_VERSION,
    }
    return build_result(request, state, before, evidence)


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
        if previous.get("request_sha256") == signature and (
            previous.get("status") == "completed" or not args.retry_failures
        ):
            continue
        try:
            result = resolve(request)
            result["status"] = "completed"
        except Exception as exc:
            result = {
                "id": key,
                "status": "failed",
                "reason_code": getattr(exc, "code", "unexpected"),
                "error": redact(str(exc)),
            }
        result["request_sha256"] = signature
        results["results"][key] = result
        atomic_json(args.output, results)
        print(json.dumps({"id": key, "status": result["status"]}), flush=True)
    return int(any(r["status"] != "completed" for r in results["results"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
