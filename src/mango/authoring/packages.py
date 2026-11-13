"""Resolve distro package installs into exact, hashed files for an offline build.

Authoring-time only. The package manager runs inside the digest-pinned base
image, with network access and only the author's explicit sources (normally
dated archive snapshots), and selects the files each install needs. Every
selected file is downloaded once and checked against the archive's own index
hash (apt) before its SHA-256 is recorded in ``inputs.lock.json``. Builds read
only that lock and install with downloads disabled.

A request is a base, its sources and ordered *stages*. Each stage is one
package-manager transaction, resolved after the previous stages are installed,
so a later stage sees exactly what the earlier ones left behind. A stage may
prefer a suite (apt ``-t``) and add sources of its own.

Resolution needs Docker and the network, so results are cached outside any
environment, keyed by the request's content: ``MANGO_LOCK_CACHE`` (default
``~/.cache/mango/locks``). ``MANGO_LOCK_OFFLINE=1`` turns a cache miss into
an error. Tests install a resolver with :func:`use_resolver`.

    mango-lock-packages --lock inputs.lock.json --manager apt \\
        --base docker.io/library/ubuntu@sha256:<digest> \\
        --source 'deb https://snapshot.ubuntu.com/ubuntu/20261001T000000Z noble main universe' \\
        --stage 'ca-certificates python3' --stage 'build-essential'

Install a stage offline in ``mkLinuxImage`` with the harness's stage-debs script:

    sh /.nix-inputs/files/stage-debs.sh /var/cache/apt/archives \\
        /.nix-inputs/files/apt/<key>/0/*.deb
    apt-get install -y --no-download --no-install-recommends /var/cache/apt/archives/*.deb
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

CACHE_ENV = "MANGO_LOCK_CACHE"
OFFLINE_ENV = "MANGO_LOCK_OFFLINE"
# Older apt caches could omit URI lines whose indexes no longer carry MD5;
# older apk caches described recursive fetches rather than exact transactions.
RESOLVER_VERSIONS = {"apt": 2, "apk": 3, "opkg": 1}
CA_BUNDLE = "/etc/mango-lock-ca.crt"
STAGE_MARKER = "@@mango-stage "
APT_URI = re.compile(
    r"^'(?P<url>[^']+)' (?P<name>\S+) (?P<size>\d+) (?P<kind>[A-Za-z0-9]+):(?P<hash>[0-9a-f]+)$"
)
OPKG_URI = re.compile(r"^Downloading (?P<url>https?://\S+?\.ipk)\.?$")
APK_FILE = re.compile(
    r"^@@mango-apk (?P<sha256>[0-9a-f]{64}) (?P<name>[^/\s]+\.apk) (?P<url>https?://\S+)$"
)
ALGORITHMS = {"SHA512": "sha512", "SHA256": "sha256", "SHA1": "sha1", "MD5Sum": "md5"}


class LockError(RuntimeError):
    """Packages could not be resolved into pinned files."""


class PinnedFile(BaseModel):
    """Explicit verified bytes copied to an original in-container pathname."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    urls: tuple[str, ...] = ()
    author_source: str | None = None

    @model_validator(mode="after")
    def validate_input(self):
        from mango.authoring.pin_file import validate_url
        if (not self.path.startswith("/") or self.path.endswith("/")
                or any(part in (".", "..") for part in self.path.split("/"))
                or any(ord(c) < 32 for c in self.path)):
            raise ValueError("input path must be an absolute, normalized filename")
        if not self.urls and not self.author_source:
            raise ValueError("input requires public URLs or an author source file")
        if self.author_source and not Path(self.author_source).is_absolute():
            raise ValueError("author_source must be an absolute path on the author host")
        for url in self.urls:
            validate_url(url)
        return self


class LocalDeb(PinnedFile):
    @model_validator(mode="after")
    def validate_deb(self):
        if not self.path.endswith(".deb"):
            raise ValueError("local Debian input must use a .deb filename")
        return self


class RepositoryKey(PinnedFile):
    """A hash-pinned apt signing key, optionally converted from ASCII armor."""

    dearmor: bool = True
    mode: int = Field(default=0o644, ge=0, le=0o777)


class AptPreference(BaseModel):
    """An exact apt preferences file, including its original pathname."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    path: str
    content: str

    @model_validator(mode="after")
    def validate_preference(self):
        if (self.path != "/etc/apt/preferences" and not self.path.startswith("/etc/apt/preferences.d/")):
            raise ValueError("apt preference path must be under /etc/apt/preferences.d")
        if (self.path.endswith("/") or any(part in (".", "..") for part in self.path.split("/"))
                or any(ord(c) < 32 for c in self.path) or "\0" in self.content):
            raise ValueError("invalid apt preference path or content")
        return self


class PackageStage(BaseModel):
    """One package-manager transaction, resolved after the stages before it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    packages: tuple[str, ...] = ()
    #: apt ``-t``: prefer this suite for the whole transaction.
    target_release: str | None = None
    #: Sources added (after the request's own) before this stage resolves.
    extra_sources: tuple[str, ...] = ()
    #: Replace all apt repository source lines before this stage (None retains them).
    sources: tuple[str, ...] | None = None
    #: False is explicit --no-install-recommends; None inherits the base policy.
    install_recommends: bool | None = False
    local_debs: tuple[LocalDeb, ...] = ()
    operation: Literal["apt_install", "dpkg_then_fix", "apt_update"] = "apt_install"
    dpkg_options: tuple[str, ...] = ()
    clear_lists_before: bool = False
    update_before: bool = False
    debconf_selections: tuple[str, ...] = ()
    repository_keys: tuple[RepositoryKey, ...] = ()
    apt_preferences: tuple[AptPreference, ...] = ()
    #: Preserve a common original service-start policy before this transaction.
    policy_rc_d: Literal["deny"] | None = None

    @model_validator(mode="after")
    def validate_transaction(self):
        if self.operation == "apt_install" and not self.packages:
            raise ValueError("apt_install requires packages")
        if self.operation == "dpkg_then_fix" and (self.packages or not self.local_debs):
            raise ValueError("dpkg_then_fix uses local_debs and no apt package roots")
        if self.operation == "apt_update" and (self.packages or self.local_debs):
            raise ValueError("apt_update does not install packages or local inputs")
        allowed = {"--force-confold", "--force-confnew", "--force-downgrade", "--force-overwrite"}
        if set(self.dpkg_options) - allowed:
            raise ValueError("Unsupported dpkg installation option")
        if self.dpkg_options and self.operation != "dpkg_then_fix":
            raise ValueError("dpkg_options require dpkg_then_fix")
        paths = [item.path for item in self.local_debs]
        if len(set(paths)) != len(paths):
            raise ValueError("Duplicate local .deb target path")
        if any(not value.strip() or "\n" in value or "\r" in value
               for value in self.debconf_selections):
            raise ValueError("debconf selections must be individual nonempty lines")
        return self


class LockRequest(BaseModel):
    """Everything that decides which files an image's installs select."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manager: Literal["apt", "opkg", "apk"]
    #: Digest-pinned registry reference the package manager runs in.
    base: str = Field(pattern=r"@sha256:[0-9a-f]{64}$")
    sources: tuple[str, ...] = ()
    stages: tuple[PackageStage, ...] = Field(min_length=1)

    def cache_document(self) -> dict:
        """Canonical request while retaining keys of older unchanged requests."""
        value = self.model_dump(mode="json")
        # Preserve existing cache keys when no repository replacement is authored.
        for stage in value["stages"]:
            if stage.get("sources") is None:
                stage.pop("sources", None)
            for name, default in (("install_recommends", False), ("local_debs", []),
                                  ("operation", "apt_install"), ("dpkg_options", []),
                                  ("clear_lists_before", False), ("update_before", False)):
                if stage.get(name) == default:
                    stage.pop(name, None)
            if not stage.get("debconf_selections"):
                stage.pop("debconf_selections", None)
            for field in ("local_debs", "repository_keys"):
                for item in stage.get(field, []):
                    item.pop("author_source", None)
            for field in ("repository_keys", "apt_preferences"):
                if not stage.get(field):
                    stage.pop(field, None)
            if stage.get("policy_rc_d") is None:
                stage.pop("policy_rc_d", None)
        return value

    @property
    def key(self) -> str:
        """Content-derived name, used for the lock prefixes and the cache."""
        document = json.dumps(self.cache_document(), sort_keys=True).encode()
        return hashlib.sha256(document).hexdigest()[:16]

    def stage_prefix(self, index: int) -> str:
        """Where stage ``index``'s files live in the lock and the input mount."""
        return f"{self.manager}/{self.key}/{index}"


class LockedFile(BaseModel):
    """One selected package file: its archive name, URL and SHA-256."""

    name: str
    url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


Resolver = Callable[[LockRequest], list[list[LockedFile]]]
_resolver: Resolver | None = None


@contextlib.contextmanager
def use_resolver(resolver: Resolver) -> Iterator[None]:
    """Resolve with ``resolver`` instead of Docker (tests, alternative backends)."""
    global _resolver
    previous, _resolver = _resolver, resolver
    try:
        yield
    finally:
        _resolver = previous


# -- cache -----------------------------------------------------------------------


def cache_dir() -> Path:
    configured = os.environ.get(CACHE_ENV)
    return Path(configured) if configured else Path.home() / ".cache" / "mango" / "locks"


def _cache_path(request: LockRequest) -> Path:
    return cache_dir() / f"{request.manager}-{request.key}.json"


def _read_cache(request: LockRequest) -> list[list[LockedFile]] | None:
    path = _cache_path(request)
    if not path.is_file():
        return None
    document = json.loads(path.read_text())
    if document.get("request") != request.cache_document():
        return None
    if document.get("resolver_version", 1) != RESOLVER_VERSIONS[request.manager]:
        return None
    if any(stage.local_debs for stage in request.stages) and not (
        cache_dir() / f"apt-{request.key}-local-debs.json"
    ).exists():
        return None
    return [[LockedFile.model_validate(item) for item in stage] for stage in document["stages"]]


def _write_cache(request: LockRequest, stages: list[list[LockedFile]]) -> None:
    path = _cache_path(request)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "resolver_version": RESOLVER_VERSIONS[request.manager],
        "request": request.cache_document(),
        "stages": [[item.model_dump() for item in stage] for stage in stages],
    }
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
    os.replace(stream.name, path)


def resolve(request: LockRequest) -> list[list[LockedFile]]:
    """Each stage's files, from the cache or a fresh resolution."""
    cached = _read_cache(request)
    if cached is not None:
        return cached
    if os.environ.get(OFFLINE_ENV) == "1":
        raise LockError(
            f"{request.manager} lock for {request.base} is not cached and "
            f"{OFFLINE_ENV}=1 forbids resolving it"
        )
    stages = (_resolver or default_resolver)(request)
    if len(stages) != len(request.stages):
        raise LockError(f"resolver returned {len(stages)} stages for {len(request.stages)}")
    _write_cache(request, stages)
    return stages


def lock_artifacts(request: LockRequest, stages: list[list[LockedFile]]) -> list[dict]:
    """``inputs.lock.json`` artifacts for a resolved request, under its prefixes."""
    return [
        {"path": f"{request.stage_prefix(index)}/{item.name}", "urls": [item.url], "sha256": item.sha256}
        for index, stage in enumerate(stages)
        for item in stage
    ]


def update_lock(lock: Path, artifacts: list[dict], replace_prefixes: list[str] = ()) -> dict:
    """Write ``artifacts`` into ``lock``, replacing entries under ``replace_prefixes``."""
    document = (
        json.loads(lock.read_text()) if lock.exists() else {"schema_version": 1, "artifacts": []}
    )
    if document.get("schema_version") != 1:
        raise ValueError("Unsupported inputs lock schema")
    stale = tuple(prefix.rstrip("/") + "/" for prefix in replace_prefixes)
    paths = {artifact["path"] for artifact in artifacts}
    kept = [
        a
        for a in document["artifacts"]
        if a["path"] not in paths and not (stale and a["path"].startswith(stale))
    ]
    document["artifacts"] = sorted(kept + artifacts, key=lambda a: a["path"])
    lock.write_text(json.dumps(document, indent=2) + "\n")
    return document


# -- resolution inside the base image -------------------------------------------------


def download_hash(record: dict, cache: Path) -> str:
    """Stream and verify an archive, reusing a content-addressed local cache.

    Verify cached bytes on every use. Downloads become visible only after their
    size and index hash pass; concurrent writers can publish only identical bytes.
    """
    algorithm = ALGORITHMS.get(record["kind"])
    if algorithm is None:
        raise LockError(f"Unsupported apt hash type: {record['kind']}")
    expected = record["hash"]
    if not re.fullmatch(f"[0-9a-f]{{{hashlib.new(algorithm).digest_size * 2}}}", expected):
        raise LockError(f"Invalid {record['kind']} archive hash")
    target = cache / algorithm / expected
    target.parent.mkdir(parents=True, exist_ok=True)

    def consume(source, destination=None):
        indexed, sha256 = hashlib.new(algorithm), hashlib.sha256()
        size = 0
        for chunk in iter(lambda: source.read(1 << 20), b""):
            size += len(chunk)
            indexed.update(chunk)
            sha256.update(chunk)
            if destination is not None:
                destination.write(chunk)
        if size != int(record["size"]) or indexed.hexdigest() != expected:
            raise LockError(f"Downloaded bytes do not match apt's index: {record['url']}")
        return sha256.hexdigest()

    if target.exists():
        try:
            with target.open("rb") as source:
                return consume(source)
        except LockError:
            target.unlink(missing_ok=True)
            raise
    temporary = None
    try:
        with (urllib.request.urlopen(record["url"], timeout=120) as source,
              tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output):
            temporary = Path(output.name)
            digest = consume(source, output)
        os.replace(temporary, target)
        return digest
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _run_in_base(request: LockRequest, script: str) -> str:
    from mango.authoring.package_inputs import mount_args
    ca = os.environ.get("SSL_CERT_FILE") or "/etc/ssl/certs/ca-certificates.crt"
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--platform", "linux/amd64",
            "-v", f"{Path(ca).resolve()}:{CA_BUNDLE}:ro",
            *mount_args(request),
            "--entrypoint", "/bin/sh", request.base, "-c", script,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise LockError(
            f"{request.manager} resolution on {request.base} failed:\n" + result.stderr[-4000:]
        )
    return result.stdout


def _split_stages(output: str, count: int, pattern: re.Pattern) -> list[list[dict]]:
    stages: list[list[dict]] = [[] for _ in range(count)]
    current = -1
    for line in output.splitlines():
        line = line.strip()
        if line.startswith(STAGE_MARKER):
            current = int(line.removeprefix(STAGE_MARKER))
            continue
        match = pattern.match(line)
        if match and current >= 0:
            stages[current].append(match.groupdict())
    return stages


def apt_script(request: LockRequest, *, start: int = 0, initialize: bool = True,
               install_last: bool = False) -> str:
    """Resolve each stage, then install it so the next resolves against it."""
    # Dated snapshots publish an expired Valid-Until; the date is the pin.
    options = (
        "-o Acquire::Check-Valid-Until=false "
        "-o Acquire::ForceHash=SHA256 "
        f"-o Acquire::https::CaInfo={CA_BUNDLE} -o Acquire::Retries=5"
    )
    lines = [
        "set -eu",
        "export DEBIAN_FRONTEND=noninteractive",
    ]
    if initialize:
        lines += [
        "rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*",
        "printf '%s\\n' " + " ".join(shlex.quote(s) for s in request.sources)
        + " > /etc/apt/sources.list.d/mango-lock.list",
        ]
    last = len(request.stages) - 1
    for index, stage in enumerate(request.stages):
        if index < start:
            continue
        if stage.policy_rc_d == "deny":
            lines += ["printf '%s' " + shlex.quote("#!/bin/sh\nexit 101\n")
                      + " > /usr/sbin/policy-rc.d", "chmod +x /usr/sbin/policy-rc.d"]
        for key in stage.repository_keys:
            path = shlex.quote(key.path)
            source = f"/mango/author-keys/{key.sha256}"
            lines += [f"mkdir -p {shlex.quote(str(Path(key.path).parent))}", f"rm -f {path}"]
            if key.dearmor:
                lines.append(f"gpg --batch --yes --dearmor --output {path} {source}")
            else:
                lines.append(f"cp {source} {path}")
            lines.append(f"chmod {key.mode:04o} {path}")
        for preference in stage.apt_preferences:
            lines += [f"mkdir -p {shlex.quote(str(Path(preference.path).parent))}",
                      f"printf '%s' {shlex.quote(preference.content)} > {shlex.quote(preference.path)}"]
        if initialize and index == start:
            lines.append(f"apt-get -qq {options} update >&2")
        if stage.sources is not None:
            lines += [
                "rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*",
                "printf '%s\\n' " + " ".join(shlex.quote(s) for s in stage.sources)
                + " > /etc/apt/sources.list.d/mango-lock.list",
                f"apt-get -qq {options} update >&2",
            ]
        if stage.extra_sources:
            lines += [
                "printf '%s\\n' " + " ".join(shlex.quote(s) for s in stage.extra_sources)
                + " >> /etc/apt/sources.list.d/mango-lock.list",
                f"apt-get -qq {options} update >&2",
            ]
        target = f"-t {shlex.quote(stage.target_release)} " if stage.target_release else ""
        packages = " ".join(shlex.quote(p) for p in stage.packages)
        recommendation = ({False: "--no-install-recommends", True: "--install-recommends"}
                          .get(stage.install_recommends, ""))
        common = f"apt-get -qq {options} {recommendation} --allow-downgrades -y"
        for item in stage.local_debs:
            lines += [f"mkdir -p {shlex.quote(str(Path(item.path).parent))}",
                      f"cp /mango/author-debs/{item.sha256}.deb {shlex.quote(item.path)}",
                      f"chmod 0644 {shlex.quote(item.path)}"]
        if stage.clear_lists_before:
            lines.append("rm -rf /var/lib/apt/lists/*")
        already_updated = ((initialize and index == start) or stage.sources is not None
                           or bool(stage.extra_sources)) and not stage.clear_lists_before
        if stage.update_before or (stage.operation == "apt_update" and not already_updated):
            lines.append(f"apt-get -qq {options} update >&2")
        if stage.debconf_selections:
            lines.append("printf '%s\\n' " + " ".join(shlex.quote(s) for s in stage.debconf_selections)
                         + " | debconf-set-selections")
        lines.append(f"echo '{STAGE_MARKER}{index}'")
        for item in stage.local_debs:
            name = shlex.quote(item.path)
            lines.append("printf '@@mango-local-deb\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "
                         f"{shlex.quote(item.sha256)} {name} "
                         f'"$(dpkg-deb -f {name} Package)" "$(dpkg-deb -f {name} Version)" '
                         f'"$(dpkg-deb -f {name} Architecture)"')
        if stage.operation == "apt_update":
            continue
        if stage.operation == "dpkg_then_fix":
            dpkg = shlex.join(["dpkg", "-i", *stage.dpkg_options,
                              *(item.path for item in stage.local_debs)])
            lines += [f"if {dpkg} >&2; then :; else",
                      f"apt-get -s {options} {recommendation} {target}-f install > /tmp/mango-repair-plan",
                      "if grep -q '^Remv ' /tmp/mango-repair-plan; then "
                      "echo 'Mango does not lock repair transactions that remove packages' >&2; exit 1; fi",
                      f"{common} --print-uris {target}-f install"]
            if index != last or install_last:
                lines.append(f"{common} {target}-f install >&2")
            lines.append("fi")
        else:
            lines.append(f"{common} --print-uris {target}install {packages}")
            if index != last or install_last:
                lines.append(f"{common} {target}install {packages} >&2")
    return "\n".join(lines)


def opkg_script(request: LockRequest) -> str:
    """opkg names each file it downloads; installing lets the next stage see them."""
    lines = ["set -eu", "mkdir -p /var/lock", "opkg update >&2"]
    for index, stage in enumerate(request.stages):
        if (stage.local_debs or stage.operation != "apt_install" or stage.clear_lists_before
                or stage.update_before or stage.install_recommends is not False or stage.debconf_selections
                or stage.repository_keys or stage.apt_preferences or stage.policy_rc_d is not None):
            raise LockError("opkg stages do not support Debian input operations")
        if stage.sources is not None:
            raise LockError("opkg stages do not support apt source replacement")
        packages = " ".join(shlex.quote(p) for p in stage.packages)
        lines += [f"echo '{STAGE_MARKER}{index}'", f"opkg install {packages} 2>&1"]
    return "\n".join(lines)


def filter_local_deb_uris(request: LockRequest, output: str) -> str:
    """Local roots are supplied inputs, never downloadable apt dependencies."""
    current = -1
    kept = []
    for line in output.splitlines():
        if line.startswith(STAGE_MARKER):
            current = int(line.removeprefix(STAGE_MARKER))
        if line.startswith("'file:"):
            url = line.split("'", 2)[1]
            path = unquote(urlsplit(url).path)
            if not 0 <= current < len(request.stages) or path not in {
                item.path for item in request.stages[current].local_debs
            }:
                raise LockError("apt selected an undeclared local Debian input")
            continue
        kept.append(line)
    return "\n".join(kept)


def local_deb_metadata(request: LockRequest) -> list[list[dict]]:
    path = cache_dir() / f"apt-{request.key}-local-debs.json"
    value = json.loads(path.read_text())
    if value["request"] != request.cache_document():
        raise LockError("Local Debian metadata does not match the request")
    return value["stages"]


def _resolve_apt(request: LockRequest) -> list[list[LockedFile]]:
    if os.environ.get("MANGO_APT_PREFIX_CACHE") == "1" and len(request.stages) > 1:
        from mango.authoring.apt_prefix_cache import resolve_output
        output = resolve_output(request)
    else:
        output = _run_in_base(request, apt_script(request))
    output = filter_local_deb_uris(request, output)
    lines = [line.strip() for line in output.splitlines()]
    if [line for line in lines if line.startswith(STAGE_MARKER)] != [
        f"{STAGE_MARKER}{i}" for i in range(len(request.stages))
    ]:
        raise LockError("apt resolution did not report every requested stage")
    if any(line.startswith("'") and not APT_URI.fullmatch(line) for line in lines):
        raise LockError("apt returned an archive URI without a supported index hash")
    stages = _split_stages(output, len(request.stages), APT_URI)
    # Most corpus environments share distro dependencies. Retain verified files
    # across different requests and bound transfers instead of downloading each
    # whole transaction serially into a discarded temporary directory.
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {}
        for records in stages:
            for record in records:
                key = (record["kind"], record["hash"], record["size"])
                if key not in pending:
                    pending[key] = pool.submit(download_hash, record, cache_dir() / "downloads")
        result = [[LockedFile(
            name=r["name"], url=r["url"],
            sha256=pending[(r["kind"], r["hash"], r["size"])].result(),
        ) for r in records] for records in stages]
    if any(stage.local_debs for stage in request.stages):
        metadata = [[] for _ in request.stages]
        current = -1
        for line in lines:
            if line.startswith(STAGE_MARKER):
                current = int(line.removeprefix(STAGE_MARKER))
            if line.startswith("@@mango-local-deb\t"):
                _, sha256, path, package, version, architecture = line.split("\t")
                if (not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", package)
                        or not re.fullmatch(r"[A-Za-z0-9.+:~_-]+", version)
                        or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", architecture)):
                    raise LockError("Invalid local Debian package control metadata")
                metadata[current].append({"sha256": sha256, "path": path, "package": package,
                                          "version": version, "architecture": architecture})
        for stage, values in zip(request.stages, metadata, strict=True):
            if {(v["path"], v["sha256"]) for v in values} != {
                (v.path, v.sha256) for v in stage.local_debs
            }:
                raise LockError("Missing local Debian input metadata")
        path = cache_dir() / f"apt-{request.key}-local-debs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
            json.dump({"request": request.cache_document(), "stages": metadata}, stream)
        os.replace(stream.name, path)
    return result


def _resolve_opkg(request: LockRequest) -> list[list[LockedFile]]:
    stages = _split_stages(_run_in_base(request, opkg_script(request)), len(request.stages), OPKG_URI)
    resolved = []
    for records in stages:
        files = []
        for record in records:
            with urllib.request.urlopen(record["url"]) as response:
                data = response.read()
            files.append(
                LockedFile(
                    name=record["url"].rsplit("/", 1)[-1],
                    url=record["url"],
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            )
        resolved.append(files)
    return resolved


def apk_script(request: LockRequest) -> str:
    """Fetch and verify the exact APK transaction on the target platform."""
    if not request.sources:
        raise LockError("apk requires explicitly supplied repository URLs")
    lines = [
        "set -eu",
        f"export SSL_CERT_FILE={CA_BUNDLE}",
        "printf '%s\\n' " + " ".join(shlex.quote(s) for s in request.sources)
        + " > /etc/apk/repositories",
        "apk update >&2",
    ]
    for index, stage in enumerate(request.stages):
        if (stage.local_debs or stage.operation != "apt_install" or stage.clear_lists_before
                or stage.update_before or stage.install_recommends is not False or stage.debconf_selections
                or stage.repository_keys or stage.apt_preferences or stage.policy_rc_d is not None):
            raise LockError("apk stages do not support Debian input operations")
        if stage.target_release or stage.extra_sources or stage.sources is not None:
            raise LockError("apk stages do not support apt release/source options")
        packages = " ".join(shlex.quote(p) for p in stage.packages)
        directory = f"/tmp/mango-apk-{index}"
        lines += [
            f"mkdir -p {directory}",
            f"apk add --simulate {packages} > {directory}/transaction",
            # Recursive fetch resolves a fresh world, potentially upgrading
            # installed dependencies that apk add would retain. Lock only the
            # exact versions selected by the original add transaction instead.
            "awk '\n"
            "  /^[[:space:]]*\\([[:space:]]*[0-9]+\\/[[:space:]]*[0-9]+\\)/ {\n"
            "    sub(/^[[:space:]]*\\([[:space:]]*[0-9]+\\/[[:space:]]*[0-9]+\\)[[:space:]]*/, \"\");\n"
            "    if ($1 != \"Installing\" && $1 != \"Upgrading\" &&\n"
            "        $1 != \"Downgrading\" && $1 != \"Reinstalling\") exit 1;\n"
            "    version=$NF; gsub(/[()]/, \"\", version);\n"
            "    print $2 \"=\" version; next;\n"
            "  }\n"
            "  /Installing|Upgrading|Downgrading|Reinstalling|Purging/ { exit 1 }' "
            + f"{directory}/transaction > {directory}/selected",
            f": > {directory}/urls",
            "while IFS= read -r selected; do\n"
            # apk-tools 2.14 fetch accepts package names, not name=version.
            # Verify its chosen archive against the simulated exact version
            # before downloading, rather than silently selecting another one.
            "  package=${selected%%=*}; version=${selected#*=}\n"
            "  expected=$package-$version.apk\n"
            "  url=$(apk fetch --url \"$package\")\n"
            "  case \"$url\" in http://*/\"$expected\"|https://*/\"$expected\") ;;\n"
            "    *) echo \"APK fetch disagrees with selected version: $selected\" >&2; exit 1 ;; esac\n"
            f"  printf '%s\\n' \"$url\" >> {directory}/urls\n"
            f"  apk fetch --output {directory} \"$package\" >&2\n"
            f"  test -f {directory}/\"$expected\"\n"
            f"done < {directory}/selected",
            f"echo '{STAGE_MARKER}{index}'",
            f"while IFS= read -r url; do\n"
            "  case \"$url\" in http://*.apk|https://*.apk) ;; *) continue ;; esac\n"
            "  name=${url##*/}\n"
            f"  file={directory}/\"$name\"\n"
            "  apk verify \"$file\" >&2\n"
            "  digest=$(sha256sum \"$file\"); digest=${digest%% *}\n"
            "  printf '@@mango-apk %s %s %s\\n' \"$digest\" \"$name\" \"$url\"\n"
            f"done < {directory}/urls",
            # Authoring is online. Installing the original request preserves
            # /etc/apk/world; adding every closure archive would make transitive
            # dependencies explicit and change later remove transactions.
            f"apk add {packages} >&2",
        ]
    return "\n".join(lines)


def _resolve_apk(request: LockRequest) -> list[list[LockedFile]]:
    output = _run_in_base(request, apk_script(request))
    markers = [line.strip() for line in output.splitlines() if line.startswith(STAGE_MARKER)]
    if markers != [f"{STAGE_MARKER}{i}" for i in range(len(request.stages))]:
        raise LockError("apk resolution did not report every requested stage")
    if any(line.startswith("@@mango-apk ") and not APK_FILE.fullmatch(line)
           for line in output.splitlines()):
        raise LockError("apk returned a malformed hashed package record")
    records = _split_stages(output, len(request.stages), APK_FILE)
    # An empty transaction is valid when all requested packages are installed.
    return [[LockedFile(**record) for record in stage] for stage in records]


def default_resolver(request: LockRequest) -> list[list[LockedFile]]:
    return {"apt": _resolve_apt, "opkg": _resolve_opkg, "apk": _resolve_apk}[request.manager](request)


def lock_request(request: LockRequest, lock: Path) -> list[str]:
    """Resolve ``request`` into ``lock``; returns the stage prefixes to install from."""
    stages = resolve(request)
    prefixes = [request.stage_prefix(index) for index in range(len(stages))]
    update_lock(lock, lock_artifacts(request, stages), replace_prefixes=prefixes)
    return prefixes


def main():
    parser = argparse.ArgumentParser(
        prog="mango-lock-packages", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--lock", type=Path, required=True, help="inputs.lock.json to update")
    parser.add_argument("--request", type=Path, help="complete typed LockRequest JSON, including repository timeline")
    parser.add_argument("--receipt", type=Path, help="write a portable package-state receipt")
    parser.add_argument("--manager", choices=["apt", "opkg", "apk"])
    parser.add_argument("--base", help="pinned base image@sha256:...")
    parser.add_argument("--source", action="append", default=[], help="package source line")
    parser.add_argument(
        "--stage", action="append",
        help="space-separated packages for one transaction; repeat for ordered stages",
    )
    parser.add_argument("--target-release", help="apt -t suite for every stage")
    args = parser.parse_args()
    if args.request:
        if args.base or args.stage or args.source or args.target_release or args.manager:
            parser.error("--request cannot be combined with individual request flags")
        request = LockRequest.model_validate_json(args.request.read_text())
    else:
        if not args.base or not args.stage:
            parser.error("--base and --stage are required unless --request is supplied")
        request = LockRequest(
            manager=args.manager or "apt",
            base=args.base,
            sources=tuple(args.source),
            stages=tuple(
                PackageStage(packages=tuple(stage.split()), target_release=args.target_release)
                for stage in args.stage
            ),
        )
    if args.receipt:
        from mango.authoring.package_state import resolve_packages, write_receipt
        value = resolve_packages(request, args.lock)
        write_receipt(args.receipt, value)
        prefixes = value["prefixes"]
    else:
        prefixes = lock_request(request, args.lock)
    print(json.dumps({"prefixes": prefixes}))


if __name__ == "__main__":
    main()
