"""Exercise authenticated cache reads without cloud credentials or services."""

import contextlib
import http.client
import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from mango import cache, launcher

KEY = "fixture:" + "A" * 43 + "="
URL = "https://cache.example.com"


class CacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "default.nix").write_text("{}")
        trust = patch.object(cache, "daemon_trusts_client", return_value=True)
        trust.start()
        self.addCleanup(trust.stop)
        self.user = {"MANGO_CREDENTIALS": str(self.root / "credentials.toml")}
        user = patch.dict(os.environ, self.user)
        user.start()
        self.addCleanup(user.stop)
        self.credentials(auth="bearer", token_env="MANGO_TEST_TOKEN")

    def config(self, *caches, **overrides):
        caches = caches or ({"url": URL, "public_key": KEY, **overrides},)
        (self.root / "mango.toml").write_text("".join(
            "[[cache]]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in entry.items())
            for entry in caches
        ))

    def credentials(self, origin=URL, **entry):
        (self.root / "credentials.toml").write_text(
            f"[{json.dumps(origin)}]\n"
            + "".join(f"{key} = {json.dumps(value)}\n" for key, value in entry.items())
        )

    def test_invalid_config_fails_before_credentials_or_build(self):
        for overrides in (
            {"url": "https://cache.example.com\nsubstitute = false"},
            {"url": "https://user:secret@cache.example.com"},
            {"url": "http://cache.example.com"},
            {"url": "https:///missing-host"},
            {"public_key": "bad key"},
            # An environment may not choose credentials.
            {"auth": "bearer"}, {"token_env": "MANGO_TEST_TOKEN"}, {"token": "secret"},
        ):
            with self.subTest(overrides=overrides):
                self.config(**overrides)
                with self.assertRaises(ValueError):
                    cache.read_config(self.root)
        self.config({"url": URL, "public_key": KEY}, {"url": URL + "/", "public_key": KEY})
        with self.assertRaisesRegex(cache.CacheError, "twice"):
            cache.read_config(self.root)

    def test_invalid_credentials_are_rejected(self):
        self.config()
        for entry in (
            {"auth": "other"}, {"auth": "bearer"}, {"auth": "basic", "token_env": "bad-name"},
            {"auth": "google", "token_env": "MANGO_TEST_TOKEN"},
            {"auth": "bearer", "token_env": "MANGO_TEST_TOKEN", "token": "secret"},
        ):
            with self.subTest(entry=entry):
                self.credentials(**entry)
                with self.assertRaises(cache.CacheError):
                    with cache.session(self.root):
                        self.fail("invalid credentials accepted")

    def test_missing_token_is_actionable(self):
        self.config()
        with patch.dict(os.environ, self.user, clear=True):
            with self.assertRaisesRegex(cache.CacheError, "Set MANGO_TEST_TOKEN"):
                with cache.session(self.root):
                    self.fail("missing token accepted")

    def test_environment_cannot_direct_a_credential_to_another_host(self):
        self.config(url="https://attacker.example")
        used = []

        @contextlib.contextmanager
        def bridge(endpoint, *, tokens, scheme, hint):
            used.append((endpoint, tokens, scheme))
            yield "http://127.0.0.1:1/prefix"

        with (
            patch.dict(os.environ, {"MANGO_TEST_TOKEN": "private-token"}),
            patch.object(cache, "bridge", bridge),
            patch.object(cache, "identity_token", side_effect=AssertionError("token minted")),
        ):
            with cache.session(self.root) as env:
                # The token is still withheld from the build.
                self.assertNotIn("MANGO_TEST_TOKEN", env)
        self.assertEqual(used, [("https://attacker.example", None, None)])

    def test_each_cache_uses_its_own_credential(self):
        other = "https://other.example.com"
        self.config({"url": URL, "public_key": KEY}, {"url": other, "public_key": KEY})
        with (self.root / "credentials.toml").open("a") as stream:
            stream.write(f'["{other}/"]\nauth = "basic"\ntoken_env = "OTHER_TOKEN"\n')
        used = {}

        @contextlib.contextmanager
        def bridge(endpoint, *, tokens, scheme, hint):
            used[endpoint] = (tokens(), scheme)
            yield "http://127.0.0.1:1/" + scheme

        secrets = {"MANGO_TEST_TOKEN": "one", "OTHER_TOKEN": "two"}
        with patch.dict(os.environ, secrets), patch.object(cache, "bridge", bridge):
            with cache.session(self.root) as env:
                self.assertFalse(set(secrets) & set(env))
                self.assertEqual(env["NIX_CONFIG"].count("extra-substituters"), 2)
        self.assertEqual(used, {URL: ("one", "Bearer"), other: ("two", "Basic")})

    def test_no_config_preserves_environment(self):
        with cache.session(self.root) as env:
            self.assertIsNone(env)

    def test_untrusted_daemon_reports_why_cache_will_not_be_used(self):
        self.config()
        with (
            patch.dict(os.environ, {"MANGO_TEST_TOKEN": "private-token"}),
            patch.object(cache, "daemon_trusts_client", return_value=False),
            self.assertLogs("mango.cache", level="WARNING") as logs,
        ):
            with cache.session(self.root):
                pass
        self.assertIn("will ignore Mango's per-task cache", logs.output[0])

    def test_launcher_applies_cache_to_resolution_and_build_without_token(self):
        self.config()
        for args in (["load", str(self.root), "--builder", "host"], [str(self.root), "fetch"]):
            with (
                patch.dict(os.environ, {"MANGO_TEST_TOKEN": "private-token", "NIX_CONFIG": "sandbox = true"}),
                patch.object(launcher, "resolve", return_value=Path("/locked")) as resolve,
                patch.object(launcher.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run,
            ):
                launcher.ensure_lock(self.root)
                with self.assertRaises(SystemExit) as result:
                    launcher.main(args)
                self.assertEqual(result.exception.code, 0)
                env = run.call_args.kwargs["env"]
                self.assertEqual(resolve.call_args.kwargs["env"], env)
                self.assertNotIn("MANGO_TEST_TOKEN", env)
                self.assertNotIn("private-token", env["NIX_CONFIG"])
                self.assertIn("sandbox = true", env["NIX_CONFIG"])
                self.assertIn(KEY, env["NIX_CONFIG"])
                url = env["NIX_CONFIG"].split("extra-substituters = ")[1].splitlines()[0]
            with self.assertRaises(OSError):
                urllib.request.urlopen(url + "/nix-cache-info", timeout=1)

    def test_google_token_is_refreshed_and_not_written_into_settings(self):
        self.config()
        self.credentials(auth="google")
        with patch.object(cache, "identity_token", return_value="google-token") as fetch:
            with cache.session(self.root) as env:
                fetch.assert_called_once_with("https://cache.example.com")
                self.assertNotIn("google-token", env["NIX_CONFIG"])
        source = cache.Tokens("audience", fetch=lambda _: "fresh")
        with patch.object(source, "fetch", side_effect=["one", "two"]) as fetch:
            self.assertEqual(source(), "one")
            self.assertEqual(source(), "one")
            self.assertEqual(source.refresh(), "two")
            self.assertEqual(fetch.call_count, 2)

    def test_bridge_authenticates_restricts_paths_and_passes_redirects(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                if self.path.startswith("/nar/"):
                    self.send_response(302)
                    self.send_header("Location", "https://storage.example.com/signed?signature=example")
                    data = b""
                else:
                    self.send_response(200)
                    data = b"StoreDir: /nix/store\n"
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f"http://127.0.0.1:{server.server_port}"
            tokens = cache.Tokens(endpoint, fetch=lambda _: "private-token")
            with cache.bridge(endpoint, tokens=tokens) as url:
                parsed = urllib.parse.urlsplit(url)
                with contextlib.closing(http.client.HTTPConnection(parsed.netloc)) as client:
                    client.request("GET", parsed.path + "/nix-cache-info")
                    response = client.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"StoreDir", response.read())
                    client.request("GET", parsed.path + "/nar/" + "0" * 52 + ".nar.zst")
                    response = client.getresponse()
                    self.assertEqual(response.status, 302)
                    self.assertEqual(response.getheader("Location"), "https://storage.example.com/signed?signature=example")
                    response.read()
                    # Other caches name NAR files differently (Cachix: hex).
                    client.request("GET", parsed.path + "/nar/" + "e" * 64 + ".nar.xz")
                    response = client.getresponse()
                    self.assertEqual(response.status, 302)
                    response.read()
                    for path in (
                        "/nix-cache-info", parsed.path + "/../private",
                        parsed.path + "/uploads/x", parsed.path + "/nar/a/b",
                        parsed.path + "/nar/..", parsed.path + "/nar/x?y=1",
                    ):
                        client.request("GET", path)
                        response = client.getresponse()
                        self.assertEqual(response.status, 404)
                        response.read()
            self.assertEqual(len(seen), 3)
            self.assertTrue(all(auth == "Bearer private-token" for _, auth in seen))
            # A fixed token is sent once, with a hint that fits its kind.
            del seen[:]
            fixed = cache.Tokens(endpoint, fetch=lambda _: "private-token", renewable=False)
            rejected = "/" + "0" * 32 + ".narinfo"
            with (
                cache.bridge(endpoint, tokens=fixed, scheme="Basic", hint="check MANGO_TEST_TOKEN") as url,
                patch.object(Handler, "do_GET", lambda self: (seen.append(self.headers.get("Authorization")), self.send_response(401), self.send_header("Content-Length", "0"), self.end_headers())),
                self.assertLogs("mango.cache", level="WARNING") as logs,
            ):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(url + rejected, timeout=5)
                self.assertEqual(error.exception.code, 404)
            self.assertEqual(seen, ["Basic OnByaXZhdGUtdG9rZW4="])
            self.assertIn("check MANGO_TEST_TOKEN", logs.output[0])
            self.assertNotIn("run.invoker", logs.output[0])
            del seen[:]
            with cache.bridge(endpoint, scheme=None) as url:
                urllib.request.urlopen(url + "/nix-cache-info", timeout=5).read()
            self.assertEqual(seen, [("/nix-cache-info", None)])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
