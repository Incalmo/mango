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
environment, keyed by the request's content: ``NIX2RLE_LOCK_CACHE`` (default
``~/.cache/nix2rle/locks``). ``NIX2RLE_LOCK_OFFLINE=1`` turns a cache miss into
an error. Tests install a resolver with :func:`use_resolver`.

    nix2rle-lock-packages --lock inputs.lock.json --manager apt \\
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
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CACHE_ENV = "NIX2RLE_LOCK_CACHE"
OFFLINE_ENV = "NIX2RLE_LOCK_OFFLINE"
CA_BUNDLE = "/etc/nix2rle-lock-ca.crt"
STAGE_MARKER = "@@nix2rle-stage "
APT_URI = re.compile(
    r"^'(?P<url>[^']+)' (?P<name>\S+) (?P<size>\d+) (?P<kind>[A-Za-z0-9]+):(?P<hash>[0-9a-f]+)$"
)
OPKG_URI = re.compile(r"^Downloading (?P<url>https?://\S+?\.ipk)\.?$")
ALGORITHMS = {"SHA512": "sha512", "SHA256": "sha256", "SHA1": "sha1", "MD5Sum": "md5"}


class LockError(RuntimeError):
    """Packages could not be resolved into pinned files."""


class PackageStage(BaseModel):
    """One package-manager transaction, resolved after the stages before it."""

    model_config = ConfigDict(frozen=True)

    packages: tuple[str, ...] = Field(min_length=1)
    #: apt ``-t``: prefer this suite for the whole transaction.
    target_release: str | None = None
    #: Sources added (after the request's own) before this stage resolves.
    extra_sources: tuple[str, ...] = ()


class LockRequest(BaseModel):
    """Everything that decides which files an image's installs select."""

    model_config = ConfigDict(frozen=True)

    manager: Literal["apt", "opkg"]
    #: Digest-pinned registry reference the package manager runs in.
    base: str = Field(pattern=r"@sha256:[0-9a-f]{64}$")
    sources: tuple[str, ...] = ()
    stages: tuple[PackageStage, ...] = Field(min_length=1)

    @property
    def key(self) -> str:
        """Content-derived name, used for the lock prefixes and the cache."""
        document = json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()
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
    return Path(configured) if configured else Path.home() / ".cache" / "nix2rle" / "locks"


def _cache_path(request: LockRequest) -> Path:
    return cache_dir() / f"{request.manager}-{request.key}.json"


def _read_cache(request: LockRequest) -> list[list[LockedFile]] | None:
    path = _cache_path(request)
    if not path.is_file():
        return None
    document = json.loads(path.read_text())
    if document.get("request") != request.model_dump(mode="json"):
        return None
    return [[LockedFile.model_validate(item) for item in stage] for stage in document["stages"]]


def _write_cache(request: LockRequest, stages: list[list[LockedFile]]) -> None:
    path = _cache_path(request)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "request": request.model_dump(mode="json"),
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
    """SHA-256 of a downloaded file, after checking apt's own index hash and size."""
    algorithm = ALGORITHMS.get(record["kind"])
    if algorithm is None:
        raise LockError(f"Unsupported apt hash type: {record['kind']}")
    target = cache / record["name"]
    if not target.exists():
        with (
            urllib.request.urlopen(record["url"]) as response,
            tempfile.NamedTemporaryFile(dir=cache, delete=False) as stream,
        ):
            for chunk in iter(lambda: response.read(1 << 20), b""):
                stream.write(chunk)
        os.replace(stream.name, target)
    data = target.read_bytes()
    if len(data) != int(record["size"]) or hashlib.new(algorithm, data).hexdigest() != record["hash"]:
        target.unlink()
        raise LockError(f"Downloaded bytes do not match apt's index: {record['url']}")
    return hashlib.sha256(data).hexdigest()


def _run_in_base(request: LockRequest, script: str) -> str:
    ca = os.environ.get("SSL_CERT_FILE") or "/etc/ssl/certs/ca-certificates.crt"
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--platform", "linux/amd64",
            "-v", f"{Path(ca).resolve()}:{CA_BUNDLE}:ro",
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


def apt_script(request: LockRequest) -> str:
    """Resolve each stage, then install it so the next resolves against it."""
    # Dated snapshots publish an expired Valid-Until; the date is the pin.
    options = (
        "-o Acquire::Check-Valid-Until=false "
        f"-o Acquire::https::CaInfo={CA_BUNDLE} -o Acquire::Retries=5"
    )
    lines = [
        "set -eu",
        "export DEBIAN_FRONTEND=noninteractive",
        "rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*",
        "printf '%s\\n' " + " ".join(shlex.quote(s) for s in request.sources)
        + " > /etc/apt/sources.list.d/nix2rle-lock.list",
        f"apt-get -qq {options} update >&2",
    ]
    last = len(request.stages) - 1
    for index, stage in enumerate(request.stages):
        if stage.extra_sources:
            lines += [
                "printf '%s\\n' " + " ".join(shlex.quote(s) for s in stage.extra_sources)
                + " >> /etc/apt/sources.list.d/nix2rle-lock.list",
                f"apt-get -qq {options} update >&2",
            ]
        target = f"-t {shlex.quote(stage.target_release)} " if stage.target_release else ""
        packages = " ".join(shlex.quote(p) for p in stage.packages)
        common = f"apt-get -qq {options} --no-install-recommends --allow-downgrades -y"
        lines += [f"echo '{STAGE_MARKER}{index}'", f"{common} --print-uris {target}install {packages}"]
        if index != last:
            lines.append(f"{common} {target}install {packages} >&2")
    return "\n".join(lines)


def opkg_script(request: LockRequest) -> str:
    """opkg names each file it downloads; installing lets the next stage see them."""
    lines = ["set -eu", "mkdir -p /var/lock", "opkg update >&2"]
    for index, stage in enumerate(request.stages):
        packages = " ".join(shlex.quote(p) for p in stage.packages)
        lines += [f"echo '{STAGE_MARKER}{index}'", f"opkg install {packages} 2>&1"]
    return "\n".join(lines)


def _resolve_apt(request: LockRequest) -> list[list[LockedFile]]:
    stages = _split_stages(_run_in_base(request, apt_script(request)), len(request.stages), APT_URI)
    with tempfile.TemporaryDirectory(prefix="nix2rle-apt-lock-") as scratch:
        return [
            [
                LockedFile(name=r["name"], url=r["url"], sha256=download_hash(r, Path(scratch)))
                for r in records
            ]
            for records in stages
        ]


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


def default_resolver(request: LockRequest) -> list[list[LockedFile]]:
    return _resolve_apt(request) if request.manager == "apt" else _resolve_opkg(request)


def lock_request(request: LockRequest, lock: Path) -> list[str]:
    """Resolve ``request`` into ``lock``; returns the stage prefixes to install from."""
    stages = resolve(request)
    prefixes = [request.stage_prefix(index) for index in range(len(stages))]
    update_lock(lock, lock_artifacts(request, stages), replace_prefixes=prefixes)
    return prefixes


def main():
    parser = argparse.ArgumentParser(
        prog="nix2rle-lock-packages", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--lock", type=Path, required=True, help="inputs.lock.json to update")
    parser.add_argument("--manager", choices=["apt", "opkg"], default="apt")
    parser.add_argument("--base", required=True, help="pinned base image@sha256:...")
    parser.add_argument("--source", action="append", default=[], help="package source line")
    parser.add_argument(
        "--stage", action="append", required=True,
        help="space-separated packages for one transaction; repeat for ordered stages",
    )
    parser.add_argument("--target-release", help="apt -t suite for every stage")
    args = parser.parse_args()
    request = LockRequest(
        manager=args.manager,
        base=args.base,
        sources=tuple(args.source),
        stages=tuple(
            PackageStage(packages=tuple(stage.split()), target_release=args.target_release)
            for stage in args.stage
        ),
    )
    print(json.dumps({"prefixes": lock_request(request, args.lock)}))


if __name__ == "__main__":
    main()
