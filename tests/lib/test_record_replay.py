"""The Nix-built proxy records a command's downloads and replays them offline."""

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FETCH = "import sys, urllib.request; print(urllib.request.urlopen(sys.argv[1]).read().decode())"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"served {self.path}".encode()
        self.send_response(200)
        self.send_header("Cache-Control", "max-age=31536000, immutable")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RecordReplay(unittest.TestCase):
    def test_static_proxy_records_then_replays_without_the_server(self):
        # Building also runs the proxy's own Go tests.
        built = subprocess.run(
            ["nix-build", str(ROOT / "default.nix"), "-A", "replayProxy", "--no-out-link"],
            capture_output=True, text=True,
        )
        self.assertEqual(built.returncode, 0, built.stderr)
        proxy = built.stdout.strip().splitlines()[-1]
        self.assertIn("statically linked", subprocess.run(["file", "-L", proxy], capture_output=True, text=True).stdout)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/artifact-1.0.tar"
        with tempfile.TemporaryDirectory() as directory:
            recording = str(Path(directory) / "recording")
            command = [sys.executable, "-c", FETCH, url]
            recorded = subprocess.run([proxy, "record", recording, "--", *command], capture_output=True, text=True)
            self.assertEqual(recorded.returncode, 0, recorded.stderr)
            self.assertEqual(recorded.stdout.strip(), "served /artifact-1.0.tar")
            server.shutdown()
            server.server_close()

            index = json.loads((Path(recording) / "index.json").read_text())
            (entry,) = index["entries"].values()
            self.assertEqual(list(index["entries"]), [f"GET {url}"])
            self.assertTrue(entry["stable"])
            self.assertEqual((Path(recording) / "blobs" / entry["sha256"]).read_text(), "served /artifact-1.0.tar")

            replayed = subprocess.run([proxy, "replay", recording, "--", *command], capture_output=True, text=True)
            self.assertEqual(replayed.returncode, 0, replayed.stderr)
            self.assertEqual(replayed.stdout, recorded.stdout)

            missing = subprocess.run(
                [proxy, "replay", recording, "--", sys.executable, "-c", FETCH, url + ".sig"],
                capture_output=True, text=True,
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn(f"not recorded: GET {url}.sig", missing.stderr)


if __name__ == "__main__":
    unittest.main()
