"""Managed builds must not involve the host Nix daemon or expose Docker to builds."""

import json
import os
import subprocess
import tarfile
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from mango import builder, cache, launcher
from mango.builder_worker import archive_layout


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "source with 'quotes"
        self.root.mkdir()
        (self.root / "default.nix").write_text("{}")
        launcher.ensure_lock(self.root)
        self.work = Path(self.tmp.name) / "work"

    def test_legacy_lock_gains_builder_without_changing_harness(self):
        path = self.root / "mango.lock"
        document = json.loads(path.read_text())
        document.pop("builder", None)
        path.write_text(json.dumps(document))
        pin = builder.ensure_builder(self.root)
        self.assertEqual(pin["image"], builder.IMAGE)
        self.assertEqual(json.loads(path.read_text())["mango"], document["mango"])
        before = path.read_bytes()
        builder.ensure_builder(self.root)
        self.assertEqual(path.read_bytes(), before)

    def test_mutable_image_pin_is_rejected_without_rewriting_lock(self):
        path = self.root / "mango.lock"
        document = json.loads(path.read_text())
        document["builder"] = {"image": "nixos/nix:latest", "platform": "linux/amd64"}
        before = json.dumps(document)
        path.write_text(before)
        with self.assertRaisesRegex(ValueError, "digest-pinned"):
            builder.ensure_builder(self.root)
        self.assertEqual(path.read_text(), before)

    def test_default_load_does_not_resolve_harness_on_host(self):
        with (
            patch.object(builder, "run") as run,
            patch.object(launcher, "resolve", side_effect=AssertionError("host Nix used")),
        ):
            launcher.main(["load", str(self.root), "--workspace", str(self.work)])
        self.assertEqual(run.call_args.args[:4], (self.root, self.work, "load", []))

    def test_build_container_has_private_store_and_no_docker_socket(self):
        pin = builder.ensure_builder(self.root)
        command = builder.docker_command(self.root, self.work, pin, "builder-test", "mango-test", "true")
        self.assertIn("type=volume,source=mango-test,target=/nix", command)
        self.assertIn(f"type=bind,source={self.root},target={self.root},readonly", command)
        self.assertIn("NIX_REMOTE=local", command)
        self.assertIn("--privileged", command)
        self.assertNotIn("docker.sock", " ".join(command))
        self.assertNotIn("type=bind,source=/nix", " ".join(command))

    def test_failed_builder_cleans_up_and_does_not_replace_runtime(self):
        self.work.mkdir()
        (self.work / "runtime").mkdir()
        (self.work / "runtime/keep").write_text("previous task")
        calls = []

        def execute(command, **kwargs):
            calls.append(command)
            if command[:2] == ["docker", "run"]:
                self.assertNotIn("untrusted.example", kwargs["env"]["NIX_CONFIG"])
                self.assertIn("sandbox = true", kwargs["env"]["NIX_CONFIG"])
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0)

        with (
            patch.dict(os.environ, {"NIX_CONFIG": "extra-substituters = https://untrusted.example"}),
            patch.object(builder, "check_docker", return_value=Path("/var/run/docker.sock")),
            patch.object(builder.subprocess, "run", side_effect=execute),
            patch.object(cache, "daemon_trusts_client", side_effect=AssertionError("host Nix used")),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                builder.run(self.root, self.work, "load", [], launcher.read_lock(self.root / "mango.lock"))
        self.assertEqual((self.work / "runtime/keep").read_text(), "previous task")
        self.assertEqual(calls[-1][:3], ["docker", "rm", "--force"])
        self.assertEqual(list(self.work.glob(".builder-*")), [])

    def test_archive_dereferences_store_blobs_and_keeps_digest(self):
        layout = self.root / "layout"
        (layout / "blobs/sha256").mkdir(parents=True)
        blob = self.root / "store-blob"
        blob.write_bytes(b"retained content")
        (layout / "blobs/sha256/hash").symlink_to(blob)
        (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
        index = {"manifests": [{"digest": "sha256:" + "a" * 64}]}
        (layout / "index.json").write_text(json.dumps(index))
        output = self.root / "image.tar"
        reference = archive_layout(layout, index, output, "mango.local/main")
        self.assertEqual(reference, "mango.local/main@sha256:" + "a" * 64)
        with tarfile.open(output) as archive:
            self.assertTrue(archive.getmember("blobs/sha256/hash").isfile())
            self.assertEqual(archive.extractfile("blobs/sha256/hash").read(), b"retained content")
            copied = json.load(archive.extractfile("index.json"))
            self.assertEqual(copied["manifests"][0]["annotations"]["io.containerd.image.name"], "mango.local/main:" + "a" * 16)

    def test_import_failure_keeps_previous_runtime(self):
        stage = self.root / "stage"
        stage.mkdir()
        (stage / "image.tar").write_bytes(b"not an archive")
        (stage / "archives.json").write_text(json.dumps([{"file": "image.tar", "reference": "test"}]))
        (stage / "images.json").write_text("{}")
        (stage / "controller.json").write_text("{}")
        (stage / "runtime").mkdir()
        (stage / "runtime/task.toml").write_text("")
        self.work.mkdir()
        (self.work / "runtime").mkdir()
        (self.work / "runtime/keep").write_text("previous")
        with patch.object(builder.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "docker")):
            with self.assertRaises(subprocess.CalledProcessError):
                builder.import_result(stage, self.work)
        self.assertEqual((self.work / "runtime/keep").read_text(), "previous")

    def test_runtime_controller_is_separate_and_uses_pinned_tools(self):
        self.work.mkdir()
        (self.work / "controller.json").write_text(json.dumps({
            "python": "/nix/store/pinned-python/bin/python", "path": "/nix/store/pinned-docker/bin",
            "harness": "/nix/store/pinned-harness",
        }))
        socket = self.root / "socket"
        socket.touch()
        with patch.object(builder.subprocess, "run") as run:
            builder.run_runtime(self.root, self.work, "run", ["--agent", "oracle"], builder.ensure_builder(self.root), "store", socket)
        command = run.call_args_list[0].args[0]
        self.assertNotIn("--privileged", command)
        self.assertIn("type=volume,source=store,target=/nix,readonly", command)
        self.assertIn("/nix/store/pinned-python/bin/python", command)
        self.assertIn("--user", command)
        self.assertEqual(command[-2:], ["--agent", "oracle"])


@unittest.skipUnless(os.environ.get("MANGO_TEST_MANAGED_BUILDER") == "1", "opt-in real Docker cache/sandbox test")
class ManagedStoreIntegrationTests(unittest.TestCase):
    def test_signed_authenticated_cache_and_sandbox_in_private_store(self):
        repo = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expression = (
                f"let pkgs = import (import {repo}/lib/pins/nixpkgs.nix) {{}}; in "
                'pkgs.runCommand "mango-managed-cache-fixture" {} "echo managed-cache-ok > $out"'
            )
            output = subprocess.check_output(["nix-build", "--no-out-link", "--expr", expression], text=True).strip()
            subprocess.run(["nix-store", "--generate-binary-cache-key", "mango-test", str(root / "key"), str(root / "key.pub")], check=True)
            subprocess.run(["nix", "copy", "--to", (root / "cache").as_uri() + "?secret-key=" + str(root / "key"), output], check=True)
            requests = []

            class Handler(SimpleHTTPRequestHandler):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, directory=str(root / "cache"), **kwargs)

                def log_message(self, *args):
                    pass

                def do_GET(self):
                    requests.append(self.headers.get("Authorization"))
                    if self.headers.get("Authorization") != "Bearer fixture-token":
                        self.send_error(401)
                    else:
                        super().do_GET()

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                endpoint = f"http://127.0.0.1:{server.server_port}"
                tokens = cache.Tokens(endpoint, fetch=lambda _: "fixture-token")
                with cache.bridge(endpoint, tokens=tokens) as url:
                    for key, success in (((root / "key.pub").read_text().strip(), True), ("wrong:" + "A" * 43 + "=", False)):
                        config = (
                            "experimental-features = nix-command flakes\nsandbox = true\nsandbox-fallback = false\n"
                            f"substituters = {url}\ntrusted-public-keys = {key}\nrequire-sigs = true\n"
                        )
                        result = subprocess.run([
                            "docker", "run", "--rm", "--network", "host", "--privileged",
                            "--env", "NIX_REMOTE=local", "--env", "NIX_CONFIG", builder.IMAGE,
                            "bash", "-ec", 'nix-store --realise "$1"; cat "$1"', "bash", output,
                        ], env={**os.environ, "NIX_CONFIG": config}, capture_output=True, text=True)
                        self.assertEqual(result.returncode == 0, success, result.stderr)
                        if success:
                            self.assertIn("managed-cache-ok", result.stdout)
                        else:
                            self.assertIn("not signed", result.stderr)
                self.assertTrue(requests)
                self.assertTrue(all(value == "Bearer fixture-token" for value in requests))
                # A real sandbox build succeeds without using a host Nix store.
                result = subprocess.run([
                    "docker", "run", "--rm", "--privileged", "--env", "NIX_REMOTE=local",
                    "--env", "NIX_CONFIG=sandbox = true\nsandbox-fallback = false\nbuild-users-group =", builder.IMAGE,
                    "nix-build", "--no-out-link", "--expr",
                    'builtins.derivation { name = "mango-sandbox-probe"; system = "x86_64-linux"; builder = "/bin/sh"; args = [ "-c" "echo sandbox-ok > $out" ]; }',
                ], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
