"""Per-environment signed cache access, using only the Python standard library.

The read bridge is adapted from incalmo_shared.nix_cache. It keeps authentication
out of Nix settings and passes signed NAR redirects through without credentials.
"""

from __future__ import annotations

import base64
import contextlib
import http.client
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)
CACHE_KEY = re.compile(
    r"nix-cache-info|[0-9a-df-np-sv-z]{32}\.narinfo"
    r"|nar/[0-9a-df-np-sv-z]{52}\.nar(?:\.xz|\.zst|\.bz2|\.br)?"
)
PUBLIC_KEY_FORMAT = re.compile(r"[A-Za-z0-9.\-_]+:[A-Za-z0-9+/]{43}=")
TOKEN_LIFETIME = 45 * 60
REDIRECTS = (301, 302, 303, 307, 308)
CHUNK = 1 << 20


class CacheError(ValueError):
    pass


def read_config(root: Path):
    path = root / "mango.toml"
    if not path.exists():
        return None
    with path.open("rb") as stream:
        document = tomllib.load(stream)
    if set(document) - {"cache"}:
        raise CacheError(f"{path}: unknown Mango configuration section")
    config = document.get("cache")
    if config is None:
        return None
    allowed = {"url", "public_key", "auth", "token_env"}
    if not isinstance(config, dict) or set(config) - allowed:
        raise CacheError(f"{path}: unknown cache setting")
    if not all(isinstance(config.get(key), str) for key in ("url", "public_key", "auth")):
        raise CacheError(f"{path}: cache requires url, public_key and auth strings")
    url = check_endpoint(config["url"])
    if not url.startswith("https://"):
        raise CacheError(f"{path}: cache URL must use HTTPS")
    check_public_key(config["public_key"])
    if len(base64.b64decode(config["public_key"].split(":")[1], validate=True)) != 32:
        raise CacheError(f"{path}: invalid cache public key")
    if config["auth"] == "token":
        name = config.get("token_env")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise CacheError(f"{path}: token authentication requires token_env")
    elif config["auth"] != "google" or "token_env" in config:
        raise CacheError(f"{path}: auth must be token or google; token_env is for token auth only")
    return {**config, "url": url}


def daemon_trusts_client():
    try:
        result = subprocess.run(
            ["nix", "store", "info", "--json"], capture_output=True,
            text=True, timeout=15, check=True,
        )
        return json.loads(result.stdout).get("trusted", True)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


@contextlib.contextmanager
def session(root: Path):
    config = read_config(root)
    if config is None:
        yield None
        return
    env = dict(os.environ)
    if config["auth"] == "token":
        token = env.pop(config["token_env"], "")
        if not token or any(char.isspace() for char in token):
            raise CacheError(f"Set {config['token_env']} to your cache access token")

        def fetch(_audience):
            return token

    else:
        fetch = identity_token
    tokens = Tokens(config["url"], fetch=fetch)
    # Fail clearly before starting a build if credentials are missing.
    tokens()
    if daemon_trusts_client() is False:
        log.warning(
            "The Nix daemon does not trust this user; it will ignore Mango's "
            "per-task cache settings. Use a builder whose Nix store trusts "
            "the invoking user. Host configuration has not been changed."
        )
    with bridge(config["url"], tokens=tokens) as url:
        env["NIX_CONFIG"] = (
            env.get("NIX_CONFIG", "").rstrip("\n") + "\n"
            + substituter_config(url, config["public_key"])
        )
        yield env


def check_public_key(key: str) -> str:
    if not PUBLIC_KEY_FORMAT.fullmatch(key):
        raise CacheError("A Nix public key looks like name:<44 base64 characters>")
    return key


def _loopback(host: str | None) -> bool:
    try:
        return host == "localhost" or ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def check_endpoint(endpoint: str) -> str:
    """HTTPS (or plain HTTP to this machine, for tests), no credentials."""
    parsed = urllib.parse.urlsplit(endpoint.rstrip("/"))
    if (
        any(char.isspace() for char in endpoint)
        or not parsed.hostname
        or parsed.scheme not in {"https", "http"}
        or (parsed.scheme == "http" and not _loopback(parsed.hostname))
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path
    ):
        raise CacheError(
            "The Nix cache endpoint must be an https:// origin without "
            f"credentials, path or query: {endpoint!r}"
        )
    return endpoint.rstrip("/")


def substituter_config(endpoint: str, public_key: str) -> str:
    """``NIX_CONFIG`` text that adds one substituter and its signing key.

    ``extra-`` keeps the daemon's own substituters (cache.nixos.org) and keys.
    A Nix daemon honours these client settings only for trusted users or
    substituters listed in its ``trusted-substituters``.
    """
    return (
        f"extra-substituters = {endpoint}\n"
        f"extra-trusted-public-keys = {check_public_key(public_key)}\n"
    )


def identity_token(audience: str) -> str:
    """A Google ID token for ``audience``.

    The metadata server first (builders and Cloud Run; a fast probe that is
    remembered when absent), then an explicit service-account credentials
    file, then a developer's ``gcloud`` login. google.auth is not asked
    without a credentials file: off GCE it probes the metadata server for
    about twelve seconds before giving up. Tokens are never passed on command
    lines or written to disk.
    """
    if token := _metadata_token(audience):
        return token
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        with contextlib.suppress(ImportError):
            import google.auth.exceptions
            from google.auth.transport.requests import Request
            from google.oauth2.id_token import fetch_id_token

            try:
                return fetch_id_token(Request(), audience)
            except google.auth.exceptions.DefaultCredentialsError:
                pass
    gcloud = shutil.which("gcloud")
    if gcloud is None:
        raise CacheError("No Google credentials for the Nix cache (ADC or gcloud)")
    result = subprocess.run(
        [gcloud, "auth", "print-identity-token"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise CacheError("gcloud auth print-identity-token failed; check your Google login")
    return result.stdout.strip()


_not_on_gce = threading.Event()


def _metadata_token(audience: str) -> str | None:
    if _not_on_gce.is_set():
        return None
    query = urllib.parse.urlencode({"audience": audience, "format": "full"})
    request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        f"service-accounts/default/identity?{query}",
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.read().decode().strip()
    except (OSError, urllib.error.URLError):
        _not_on_gce.set()
        return None


class Tokens:
    """A cached token source for one audience; ``refresh()`` after a 401."""

    def __init__(self, audience: str, fetch: Callable[[str], str] = identity_token):
        self.audience = audience
        self.fetch = fetch
        self.lock = threading.Lock()
        self.token: str | None = None
        self.expires = 0.0

    def __call__(self) -> str:
        with self.lock:
            if self.token is None or time.monotonic() >= self.expires:
                self.token = self.fetch(self.audience)
                self.expires = time.monotonic() + TOKEN_LIFETIME
            return self.token

    def refresh(self) -> str:
        with self.lock:
            self.token = None
        return self()


# --- Read bridge -----------------------------------------------------------------


class _Upstream:
    """Keep-alive connections to the service, one per bridge thread."""

    def __init__(self, endpoint: str, tokens: Tokens):
        self.endpoint = urllib.parse.urlsplit(endpoint)
        self.tokens = tokens
        self.local = threading.local()

    def _connection(self) -> http.client.HTTPConnection:
        connection = getattr(self.local, "connection", None)
        if connection is None:
            kind = (
                http.client.HTTPSConnection
                if self.endpoint.scheme == "https"
                else http.client.HTTPConnection
            )
            connection = kind(self.endpoint.netloc, timeout=300)
            self.local.connection = connection
        return connection

    def get(self, method: str, key: str) -> http.client.HTTPResponse:
        for attempt in range(3):
            token = self.tokens() if attempt == 0 else self.tokens.refresh()
            connection = self._connection()
            try:
                connection.request(
                    method,
                    "/" + key,
                    headers={
                        "Authorization": "Bearer " + token,
                        "Accept-Encoding": "identity",
                    },
                )
                response = connection.getresponse()
            except (OSError, http.client.HTTPException):
                connection.close()
                self.local.connection = None
                if attempt == 2:
                    raise
                continue
            if response.status == 401 and attempt < 2:
                response.read()
                continue
            return response
        raise AssertionError("unreachable")


@contextlib.contextmanager
def bridge(
    endpoint: str,
    *,
    tokens: Tokens | None = None,
) -> Iterator[str]:
    """Serve authenticated reads on loopback under an unguessable path."""
    endpoint = check_endpoint(endpoint)
    prefix = f"/{secrets.token_urlsafe(24)}/"
    upstream = _Upstream(endpoint, tokens or Tokens(endpoint))
    warned = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            key = self.path[len(prefix) :] if self.path.startswith(prefix) else None
            if key is None or not CACHE_KEY.fullmatch(key):
                self.send_error(404)
                return
            try:
                response = upstream.get(self.command, key)
            except (OSError, http.client.HTTPException, CacheError) as error:
                if not warned.is_set():
                    warned.set()
                    log.warning(
                        "Nix cache unreachable (%s); building without it", error
                    )
                self.send_error(502)
                return
            with contextlib.closing(response):
                if response.status in (401, 403) and not warned.is_set():
                    warned.set()
                    log.warning(
                        "Nix cache %s refused this identity (HTTP %s): you need "
                        "roles/run.invoker on the cache service",
                        endpoint,
                        response.status,
                    )
                if response.status in REDIRECTS and response.getheader("Location"):
                    # A NAR: a short-lived signed URL. Nix follows it itself;
                    # the bridge never fetches bytes it does not need to.
                    response.read()
                    self.send_response(response.status)
                    self.send_header("Location", response.getheader("Location"))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if response.status != 200:
                    response.read()
                    # Nix treats 404 (and 403) as "not cached": build instead.
                    self.send_error(404 if response.status in (401, 403, 404) else 502)
                    return
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    response.getheader("Content-Type", "application/octet-stream"),
                )
                length = response.getheader("Content-Length")
                if length is not None:
                    self.send_header("Content-Length", length)
                else:
                    self.close_connection = True
                self.end_headers()
                if self.command == "HEAD":
                    return
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    while chunk := response.read(CHUNK):
                        self.wfile.write(chunk)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}{prefix.rstrip('/')}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


