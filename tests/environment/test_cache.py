"""Exercise authenticated cache reads without cloud credentials or services."""

import contextlib
import http.client
import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from mango import cache, launcher

KEY = "fixture:" + "A" * 43 + "="


class CacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "default.nix").write_text("{}")
        trust = patch.object(cache, "daemon_trusts_client", return_value=True)
        trust.start()
        self.addCleanup(trust.stop)

    def config(self, **overrides):
        settings = {
            "url": "https://cache.example.com", "public_key": KEY,
            "auth": "token", "token_env": "MANGO_TEST_TOKEN", **overrides,
        }
        (self.root / "mango.toml").write_text(
            "[cache]\n" + "\n".join(f"{key} = {json.dumps(value)}" for key, value in settings.items())
        )

    def test_invalid_config_fails_before_credentials_or_build(self):
        for overrides in (
            {"url": "https://cache.example.com\nsubstitute = false"},
            {"url": "https://user:secret@cache.example.com"},
            {"url": "http://cache.example.com"},
            {"url": "https:///missing-host"},
            {"public_key": "bad key"}, {"auth": "other"},
            {"token_env": "bad-name"}, {"token": "secret"},
        ):
            with self.subTest(overrides=overrides):
                self.config(**overrides)
                with self.assertRaises(ValueError):
                    cache.read_config(self.root)

    def test_missing_token_is_actionable(self):
        self.config()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(cache.CacheError, "Set MANGO_TEST_TOKEN"):
                with cache.session(self.root):
                    self.fail("missing token accepted")

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
        for args in (["load", str(self.root)], [str(self.root), "fetch"]):
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
        self.config(auth="google")
        path = self.root / "mango.toml"
        path.write_text(path.read_text().replace('token_env = "MANGO_TEST_TOKEN"', ""))
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
                    for path in ("/nix-cache-info", parsed.path + "/../private", parsed.path + "/uploads/x"):
                        client.request("GET", path)
                        response = client.getresponse()
                        self.assertEqual(response.status, 404)
                        response.read()
            self.assertEqual(len(seen), 2)
            self.assertTrue(all(auth == "Bearer private-token" for _, auth in seen))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
