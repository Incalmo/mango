"""Per-environment signed cache access, using only the Python standard library.

An environment's ``mango.toml`` names caches; it never names a credential.
Credentials belong to the user and are keyed by cache origin in the user's own
credentials file, so an environment cannot direct a secret to a host.

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
# The standard Nix binary cache layout. A narinfo names its own NAR file, and
# caches differ (Nix: base32 file hash; Cachix: hex; Attic: store path hash),
# so accept any single file name under nar/ rather than one hash encoding.
CACHE_KEY = re.compile(
    r"nix-cache-info|[0-9a-df-np-sv-z]{32}\.narinfo"
    r"|nar/[A-Za-z0-9][A-Za-z0-9._-]{0,254}"
)
AUTH_MODES = {"google", "bearer", "basic"}
CREDENTIALS_ENV = "MANGO_CREDENTIALS"
GOOGLE_HINT = "you need roles/run.invoker on the cache service"
PUBLIC_KEY_FORMAT = re.compile(r"[A-Za-z0-9.\-_]+:[A-Za-z0-9+/]{43}=")
TOKEN_LIFETIME = 45 * 60
REDIRECTS = (301, 302, 303, 307, 308)
CHUNK = 1 << 20


class CacheError(ValueError):
    pass


def read_config(root: Path) -> list[dict]:
    """The caches an environment declares: ``[[cache]]`` with url and public_key."""
    path = root / "mango.toml"
    if not path.exists():
        return []
    with path.open("rb") as stream:
        document = tomllib.load(stream)
    if set(document) - {"cache"}:
        raise CacheError(f"{path}: unknown Mango configuration section")
    caches = document.get("cache", [])
    if not isinstance(caches, list):
        raise CacheError(f"{path}: declare each cache as a [[cache]] table")
    result = []
    for config in caches:
        if (
            not isinstance(config, dict)
            or set(config) != {"url", "public_key"}
            or not all(isinstance(value, str) for value in config.values())
        ):
            raise CacheError(
                f"{path}: each [[cache]] takes exactly url and public_key; "
                f"credentials belong in {credentials_file()}"
            )
        url = check_endpoint(config["url"])
        if not url.startswith("https://"):
            raise CacheError(f"{path}: cache URL must use HTTPS")
        check_public_key(config["public_key"])
        if len(base64.b64decode(config["public_key"].split(":")[1], validate=True)) != 32:
            raise CacheError(f"{path}: invalid cache public key")
        if any(url == other["url"] for other in result):
            raise CacheError(f"{path}: cache {url} is declared twice")
        result.append({"url": url, "public_key": config["public_key"]})
    return result


def credentials_file() -> Path:
    if path := os.environ.get(CREDENTIALS_ENV):
        return Path(path)
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "mango" / "credentials.toml"


def read_credentials() -> dict[str, dict]:
    """The user's own credentials: one table per cache origin."""
    path = credentials_file()
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError:
        return {}
    result = {}
    for origin, entry in document.items():
        if not isinstance(entry, dict) or set(entry) - {"auth", "token_env"}:
            raise CacheError(f"{path}: [{origin!r}] takes auth and token_env")
        if entry.get("auth") not in AUTH_MODES:
            raise CacheError(f"{path}: auth must be google, bearer or basic")
        name = entry.get("token_env")
        if entry["auth"] == "google":
            if name is not None:
                raise CacheError(f"{path}: token_env is for bearer and basic auth only")
        elif not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise CacheError(f"{path}: {entry['auth']} auth requires token_env")
        result[check_endpoint(origin)] = entry
    return result


def authorization(url: str, entry: dict | None, values: dict[str, str]):
    """``(tokens, scheme, hint)`` for one cache; fails early on a missing secret."""
    where = credentials_file()
    if entry is None:
        return None, None, f'if it is private, add ["{url}"] to {where}'
    if entry["auth"] == "google":
        tokens = Tokens(url, fetch=identity_token)
        tokens()
        return tokens, "Bearer", GOOGLE_HINT
    name = entry["token_env"]
    token = values[name]
    if not token or any(char.isspace() for char in token):
        raise CacheError(f"Set {name} to your access token for {url} (named in {where})")
    return (
        Tokens(url, fetch=lambda _audience: token, renewable=False),
        entry["auth"].capitalize(),
        f"check that {name} holds a valid, unexpired token for this cache",
    )


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
def session(root: Path, *, check_trust=True, inherit_config=True):
    caches = read_config(root)
    if not caches:
        yield None
        return
    credentials = read_credentials()
    env = dict(os.environ)
    if not inherit_config:
        env.pop("NIX_CONFIG", None)
    # Secrets stay with the launcher: no child process inherits a cache token.
    values = {
        entry["token_env"]: env.pop(entry["token_env"], "")
        for entry in credentials.values() if "token_env" in entry
    }
    with contextlib.ExitStack() as stack:
        settings = []
        for config in caches:
            tokens, scheme, hint = authorization(
                config["url"], credentials.get(config["url"]), values
            )
            url = stack.enter_context(
                bridge(config["url"], tokens=tokens, scheme=scheme, hint=hint)
            )
            settings.append(substituter_config(url, config["public_key"]))
        if check_trust and daemon_trusts_client() is False:
            log.warning(
                "The Nix daemon does not trust this user; it will ignore Mango's "
                "per-task cache settings. Use a builder whose Nix store trusts "
                "the invoking user. Host configuration has not been changed."
            )
        env["NIX_CONFIG"] = (
            env.get("NIX_CONFIG", "").rstrip("\n") + "\n" + "".join(settings)
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
    file, then a developer's ``gcloud`` login (bound to ``audience`` when the
    account allows it; user accounts cannot choose one). google.auth is not asked
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
    for arguments in (["--audiences=" + audience], []):
        result = subprocess.run(
            [gcloud, "auth", "print-identity-token", *arguments],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if not result.returncode and result.stdout.strip():
            return result.stdout.strip()
    raise CacheError("gcloud auth print-identity-token failed; check your Google login")


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

    def __init__(
        self,
        audience: str,
        fetch: Callable[[str], str] = identity_token,
        renewable: bool = True,
    ):
        self.audience = audience
        self.fetch = fetch
        # A fixed token cannot be renewed; do not retry a rejected one.
        self.renewable = renewable
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

    def __init__(self, endpoint: str, tokens: Tokens | None, scheme: str | None):
        self.endpoint = urllib.parse.urlsplit(endpoint)
        self.tokens = tokens
        self.scheme = scheme
        self.local = threading.local()

    def _headers(self, renew: bool) -> dict[str, str]:
        headers = {"Accept-Encoding": "identity"}
        if self.scheme is None:
            return headers
        token = self.tokens.refresh() if renew else self.tokens()
        if self.scheme == "Basic":
            # What curl sends for a netrc entry with a password and no login.
            token = base64.b64encode((":" + token).encode()).decode()
        headers["Authorization"] = f"{self.scheme} {token}"
        return headers
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
        renewable = self.scheme is not None and self.tokens.renewable
        rejected = False
        for attempt in range(3):
            connection = self._connection()
            try:
                connection.request(
                    method, "/" + key, headers=self._headers(renew=rejected)
                )
                response = connection.getresponse()
            except (OSError, http.client.HTTPException):
                connection.close()
                self.local.connection = None
                if attempt == 2:
                    raise
                continue
            if response.status == 401 and attempt < 2 and renewable:
                response.read()
                rejected = True
                continue
            return response
        raise AssertionError("unreachable")


@contextlib.contextmanager
def bridge(
    endpoint: str,
    *,
    tokens: Tokens | None = None,
    scheme: str | None = "Bearer",
    hint: str = GOOGLE_HINT,
) -> Iterator[str]:
    """Serve cache reads on loopback under an unguessable path.

    ``scheme`` is ``Bearer``, ``Basic`` or ``None`` for a public cache.
    """
    endpoint = check_endpoint(endpoint)
    prefix = f"/{secrets.token_urlsafe(24)}/"
    if scheme is not None and tokens is None:
        tokens = Tokens(endpoint)
    upstream = _Upstream(endpoint, tokens, scheme)
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
                        "Nix cache %s refused this request (HTTP %s): %s",
                        endpoint,
                        response.status,
                        hint,
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


