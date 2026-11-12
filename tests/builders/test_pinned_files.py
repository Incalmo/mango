"""Pinned local bytes, explicit routing and bounded command/server lifecycle."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "builders/pinned_files.py"


class PinnedFilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "archive"
        self.source.write_bytes(b"pinned\x00bytes")
        self.mapping = self.root / "map.json"
        self.mapping.write_text(
            json.dumps(
                {
                    "/@scope/pkg/-/pkg.tgz": {"file": str(self.source)},
                    "/literal%2f": {"file": str(self.source)},
                    "/metadata": {
                        "file": str(self.source),
                        "content_type": "application/json",
                    },
                }
            )
        )

    def command(self, code, mapping=None):
        return [
            sys.executable,
            str(SCRIPT),
            "--map",
            str(mapping or self.mapping),
            "--",
            sys.executable,
            "-c",
            code,
        ]

    def invoke(self, code, mapping=None):
        return subprocess.run(
            self.command(code, mapping), capture_output=True, text=True, timeout=15
        )

    def request(self, target, method="GET"):
        return self.invoke(f"""
import http.client, os, urllib.parse, json
url=urllib.parse.urlsplit(os.environ["MANGO_PINNED_FILES_URL"])
c=http.client.HTTPConnection(url.hostname,url.port)
c.request({method!r},{target!r})
r=c.getresponse()
print(json.dumps([r.status,r.getheader("Content-Length"),r.read().hex(),r.getheader("Content-Type")]))
c.close()
""")

    def test_exact_bytes_scoped_encoding_and_head(self):
        for route in [
            "/@scope/pkg/-/pkg.tgz",
            "/@scope%2fpkg/-/pkg.tgz",
            "/literal%252f",
        ]:
            result = self.request(route)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout)[:3],
                [200, "12", self.source.read_bytes().hex()],
            )
        result = self.request("/metadata", "HEAD")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [200, "12", "", "application/json"])

    def test_miss_fails_even_when_child_succeeds(self):
        result = self.request("/missing")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not in pinned file map: GET /missing", result.stderr)
        self.assertEqual(json.loads(result.stdout)[0], 404)

    def test_invalid_targets_and_methods_fail_closed(self):
        for route in [
            "/../metadata",
            "/%2e%2e/metadata",
            "/%ff",
            "/bad%",
            "//metadata",
            "/metadata?q=1",
            "/metadata#x",
            "/x%5cy",
            "http://example.com/metadata",
        ]:
            with self.subTest(route=route):
                result = self.request(route)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(json.loads(result.stdout)[0], 400)
        result = self.request("/metadata", "POST")
        self.assertEqual(result.returncode, 1)
        self.assertIn("POST /metadata", result.stderr)

    def test_map_validation_precedes_child(self):
        for mapping in [
            {"/../bad": {"file": str(self.source)}},
            {"/missing": {"file": str(self.root / "absent")}},
            {"/directory": {"file": str(self.root)}},
            {"/relative": {"file": "archive"}},
        ]:
            self.mapping.write_text(json.dumps(mapping))
            result = self.invoke('print("child started")')
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("child started", result.stdout)

    def test_child_exit_and_parallel_ephemeral_ports(self):
        result = self.invoke("raise SystemExit(23)")
        self.assertEqual(result.returncode, 23)
        code = 'import os,time;print(os.environ["MANGO_PINNED_FILES_URL"],flush=True);time.sleep(.4)'
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(lambda _: self.invoke(code), range(3)))
        self.assertTrue(all(r.returncode == 0 for r in results))
        self.assertEqual(len({r.stdout.strip() for r in results}), 3)
        for result in results:
            port = int(result.stdout.strip().rsplit(":", 1)[1])
            with socket.socket() as connection:
                self.assertNotEqual(connection.connect_ex(("127.0.0.1", port)), 0)

    def test_signal_stops_child_and_listener(self):
        ready = self.root / "ready.json"
        code = f"""
import os,json,time
from pathlib import Path
Path({str(ready)!r}).write_text(json.dumps([os.getpid(),os.environ["MANGO_PINNED_FILES_URL"]]))
time.sleep(60)
"""
        child = subprocess.Popen(
            self.command(code), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self.addCleanup(lambda: child.poll() is None and child.kill())
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready.exists())
        pid, endpoint = json.loads(ready.read_text())
        child.send_signal(signal.SIGTERM)
        child.communicate(timeout=5)
        self.assertEqual(child.returncode, 143)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        with socket.socket() as connection:
            self.assertNotEqual(
                connection.connect_ex(("127.0.0.1", int(endpoint.rsplit(":", 1)[1]))), 0
            )


class PinnedFilesNixTests(unittest.TestCase):
    def test_packaged_command(self):
        harness = SCRIPT.parent.parent
        result = subprocess.run(
            [
                "nix-build",
                "--no-out-link",
                "--option",
                "builders",
                "",
                "--expr",
                f"(import {harness} {{}}).pinnedFiles",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        executable = (
            Path(result.stdout.strip().splitlines()[-1]) / "bin/mango-pinned-files"
        )
        result = subprocess.run(
            [str(executable), "--help"], text=True, capture_output=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--map", result.stdout)

    @unittest.skipUnless(os.environ.get("RUN_LINUX_VM_TESTS") == "1", "opt-in VM test")
    def test_native_base_python_and_loopback(self):
        expression = r"""
let
  h = import HARNESS {};
  map = h.pkgs.writeText "native-pinned-map.json" (builtins.toJSON {
    "/fixture".file = "/.nix-inputs/files/fixture";
  });
  probe = h.pkgs.writeText "probe.py" ''
    import os, urllib.request
    with urllib.request.urlopen(os.environ["MANGO_PINNED_FILES_URL"] + "/fixture") as response:
        assert response.read() == b"explicit pinned fixture"
  '';
in h.mkLinuxImage {
  name = "native-pinned-file-server"; diskSize = 1024; memSize = 512;
  base = h.fetchLinuxBase {
    image = "docker.io/library/python";
    digest = "sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30";
    hash = "sha256-n4iAwBNtKk4LIHUcGKoRCjDZax0soqUE0y1ZDMQTdmk=";
  };
  inputs = h.pkgs.linkFarm "native-pinned-server-inputs" [
    {name="fixture"; path=h.pkgs.writeText "fixture" "explicit pinned fixture";}
    {name="map.json"; path=map;}
    {name="probe.py"; path=probe;}
    {name="serve.py"; path=h.pinnedFilesScript;}
  ];
  buildScript = ''
    python3 /.nix-inputs/files/serve.py --map /.nix-inputs/files/map.json -- \
      python3 /.nix-inputs/files/probe.py
  '';
}
""".replace("HARNESS", str(SCRIPT.parent.parent))
        result = subprocess.run(
            [
                "nix-build",
                "--no-out-link",
                "--option",
                "builders",
                "",
                "--max-jobs",
                "1",
                "--expr",
                expression,
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
