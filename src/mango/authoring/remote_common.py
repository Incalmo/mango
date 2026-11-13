"""Shared helpers for the remote authoring resolvers (Composer, PHP prefix, ...).

Everything here is small, pure or thin: the ``MANGO_REMOTE_AUTHOR=1`` gate, redaction,
hashing, public-host checks, a bounded https download, a subprocess wrapper and an
atomic JSON writer. Resolvers that talk to Docker or the network import from here; this
module never starts a container itself.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
BASE_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HEX_LEN = {"md5": 32, "sha1": 40, "sha256": 64, "sha512": 128}
USER_AGENT = "mango-remote-author"


class ResolverError(ValueError):
    """A request or resolution result a resolver refuses; ``code`` says why."""

    def __init__(self, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.code = code


def require_remote_author() -> None:
    if os.environ.get("MANGO_REMOTE_AUTHOR") != "1":
        raise ResolverError("Set MANGO_REMOTE_AUTHOR=1 only on the remote authoring host", "gate")


_URL_AUTH_RE = re.compile(r"(://)[^/@\s]+@")
_URL_QUERY_RE = re.compile(r"(https?://[^\s?#]+)[?#][^\s]*")


def redact(text: str) -> str:
    """Strip URL userinfo, query strings and fragments from diagnostic text."""
    return _URL_QUERY_RE.sub(r"\1?<redacted>", _URL_AUTH_RE.sub(r"\1<redacted>@", text))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digests(path: Path) -> dict[str, str]:
    hashers = {name: hashlib.new(name) for name in HEX_LEN}
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            for hasher in hashers.values():
                hasher.update(chunk)
    return {name: hasher.hexdigest() for name, hasher in hashers.items()}


def public_host(host: str | None) -> bool:
    if not host:
        return False
    host = host.lower()
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return not (host == "localhost" or "." not in host
                or host.endswith((".local", ".internal", ".localhost", ".lan", ".corp")))


def run(command: list[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    except subprocess.TimeoutExpired:
        raise ResolverError(f"{' '.join(command[:3])} timed out", "build-timeout") from None
    if check and result.returncode != 0:
        tail = redact((result.stderr or "")[-1000:] + (result.stdout or "")[-2000:])
        raise ResolverError(f"{' '.join(command[:3])} failed ({result.returncode}): {tail}", "docker")
    return result


def _assert_public_resolution(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return  # resolution failures surface as download failures
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ResolverError(f"{host} resolves to a non-public address", "private-repository")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme != "https" or parts.username or "@" in parts.netloc:
            raise ResolverError("download redirected away from plain https", "insecure-redirect")
        if not public_host(parts.hostname):
            raise ResolverError("download redirected to a non-public host", "private-repository")
        _assert_public_resolution(parts.hostname or "")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SafeRedirect)


def download(url: str, destination: Path, max_bytes: int) -> str:
    """Fetch an https ``url`` to ``destination``; return its SHA-256; abort past ``max_bytes``."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.query or parts.username or not public_host(parts.hostname):
        raise ResolverError(f"refusing to fetch {redact(url)}", "private-repository")
    _assert_public_resolution(parts.hostname or "")
    digest, size = hashlib.sha256(), 0
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with _OPENER.open(request, timeout=120) as response, destination.open("wb") as out:
            if urllib.parse.urlsplit(response.geturl()).scheme != "https":
                raise ResolverError("download redirected away from https", "insecure-redirect")
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ResolverError(f"download exceeds {max_bytes} bytes", "limit-exceeded")
            for chunk in iter(lambda: response.read(1 << 20), b""):
                size += len(chunk)
                if size > max_bytes:
                    raise ResolverError(f"download exceeds {max_bytes} bytes", "limit-exceeded")
                digest.update(chunk)
                out.write(chunk)
    except urllib.error.HTTPError as exc:
        destination.unlink(missing_ok=True)
        code = "artifact-unavailable" if exc.code in (404, 410) else "download-failed"
        raise ResolverError(f"HTTP {exc.code} fetching {redact(url)}", code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        destination.unlink(missing_ok=True)
        raise ResolverError(f"fetching {redact(url)} failed: {redact(str(exc))}", "download-failed") from None
    except ResolverError:
        destination.unlink(missing_ok=True)
        raise
    return digest.hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    os.replace(stream.name, path)
