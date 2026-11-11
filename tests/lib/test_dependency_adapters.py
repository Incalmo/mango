"""Adapter integration tests. Run with nix-shell --run 'python -m unittest ...'."""

import base64
import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PREFIX = f"""let pkgs = import (import {ROOT}/lib/pins/nixpkgs.nix) {{ system = "x86_64-linux"; }};
 a = import {ROOT}/lib/adapters {{ inherit pkgs; }}; in """
PAYLOAD = b"pinned dependency fixture\n"
HASH = hashlib.sha256(PAYLOAD).hexdigest()
RESPONSES = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = RESPONSES.get(self.path, PAYLOAD)
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class Adapters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/fixture"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def evaluate(self, expr):
        return subprocess.run(
            ["nix-instantiate", "--eval", "--strict", "--expr", PREFIX + expr],
            capture_output=True,
            check=False,
            text=True,
        )

    def build(self, expr):
        result = subprocess.run(
            ["nix-build", "--no-out-link", "--expr", PREFIX + expr],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return Path(result.stdout.strip().splitlines()[-1])

    def test_explicit_input_lock_fetches_only_listed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "inputs.lock.json"
            lock.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "artifacts": [
                            {
                                "path": "service/cache/file.bin",
                                "urls": [self.url],
                                "sha256": HASH,
                            }
                        ],
                    }
                )
            )
            output = self.build(f"a.fetchLockedInputs {{ lock = {lock}; }}")
            self.assertEqual((output / "service/cache/file.bin").read_bytes(), PAYLOAD)
            self.assertEqual(
                [str(p.relative_to(output)) for p in output.rglob("*") if p.is_file()],
                ["service/cache/file.bin"],
            )

    def test_sha512_only_inputs_and_exactly_one_hash(self):
        sha512 = hashlib.sha512(PAYLOAD).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "inputs.lock.json"

            def write(record):
                lock.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "artifacts": [
                                {"path": "sha512/file.bin", "urls": [self.url], **record}
                            ],
                        }
                    )
                )

            write({"sha512": sha512})
            output = self.build(f"a.fetchLockedInputs {{ lock = {lock}; }}")
            self.assertEqual((output / "sha512/file.bin").read_bytes(), PAYLOAD)
            write({"sha512": sha512, "sha256": HASH})
            result = self.evaluate(f"(a.fetchLockedInputs {{ lock = {lock}; }}).drvPath")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exactly one content hash", result.stderr)

    def test_explicit_input_lock_rejects_invalid_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "inputs.lock.json"
            for document in [
                {"artifacts": []},
                {"schema_version": 2, "artifacts": []},
                {"schema_version": 1},
                {"schema_version": 1, "artifacts": {}},
                {
                    "schema_version": 1,
                    "artifacts": [
                        {"path": "../outside", "urls": [self.url], "sha256": HASH}
                    ],
                },
                {
                    "schema_version": 1,
                    "artifacts": [{"path": "file", "urls": [self.url]}],
                },
            ]:
                lock.write_text(json.dumps(document))
                result = self.evaluate(
                    f"(a.fetchLockedInputs {{ lock = {lock}; }}).drvPath"
                )
                self.assertNotEqual(result.returncode, 0, document)

    def test_percent_encoded_cache_names_preserved_with_safe_store_names(self):
        package = "apt/zlib1g-dev_1%3a1.2.13_amd64.deb"
        metadata = "metadata/release%3aindex.json"
        content = "pinned metadata"
        records = {
            "artifacts": [{"path": package, "urls": [self.url], "sha256": HASH}],
            "metadata": [
                {
                    "path": metadata,
                    "content": content,
                    "sha256": hashlib.sha256(content.encode()).hexdigest(),
                }
            ],
        }
        expression = f"builtins.fromJSON {json.dumps(json.dumps(records))}"
        output = self.build(
            f'a.fetchDependencyCache ({{ name = "encoded-name-fixture"; }} // ({expression}))'
        )
        self.assertTrue((output / package).is_symlink())
        self.assertTrue((output / metadata).is_symlink())
        self.assertEqual((output / package).read_bytes(), PAYLOAD)
        self.assertEqual((output / metadata).read_text(), content)
        self.assertNotIn("%", (output / package).resolve().name)
        self.assertNotIn("%", (output / metadata).resolve().name)
        # Already-valid names must retain their previous content-addressed path.
        arguments = (
            f'{{ name = "valid-name.deb"; urls = ["{self.url}"]; sha256 = "{HASH}"; }}'
        )
        expected = self.build(
            f"pkgs.fetchurl ({arguments} // {{ preferLocalBuild = false; }})"
        )
        self.assertEqual(self.build(f"a.fetchDependency {arguments}"), expected)
        old_metadata = self.build('pkgs.writeText "valid-name.json" "pinned metadata"')
        cache = self.build(
            'a.fetchDependencyCache { name = "valid-metadata-fixture"; artifacts = []; metadata = [{ path = "valid-name.json"; content = "pinned metadata"; sha256 = "'
            + hashlib.sha256(content.encode()).hexdigest()
            + '"; }]; }'
        )
        self.assertEqual((cache / "valid-name.json").resolve(), old_metadata)

    def test_paths_and_metadata_fail_closed(self):
        for paths in [["../escape"], ["/absolute"], ["a", "a"], ["a", "a/b"], ["a//b"]]:
            records = [
                {
                    "path": p,
                    "content": "fixture",
                    "sha256": hashlib.sha256(b"fixture").hexdigest(),
                }
                for p in paths
            ]
            expr = f'(a.fetchDependencyCache {{ name = "test"; artifacts = []; metadata = builtins.fromJSON {json.dumps(json.dumps(records))}; }}).drvPath'
            self.assertNotEqual(self.evaluate(expr).returncode, 0, paths)
        result = self.evaluate(
            '(a.fetchDependencyCache { name = "test"; artifacts = []; metadata = [{ path = "a"; content = "wrong"; sha256 = "bad"; }]; }).drvPath'
        )
        self.assertNotEqual(result.returncode, 0)

    def test_credential_urls_rejected(self):
        for url in [
            "https://user:secret@example.com/a",
            "https://example.com/a?token=secret",
            "file:///etc/passwd",
        ]:
            result = self.evaluate(
                f'(a.fetchDependency {{ name = "test"; urls = [{json.dumps(url)}]; sha256 = "{HASH}"; }}).drvPath'
            )
            self.assertNotEqual(result.returncode, 0, url)

    def test_maven_links_and_private_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "lock.json"
            lock.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "artifacts": [
                            {
                                "path": "org/example/1/example-1.jar",
                                "urls": [self.url],
                                "sha256": HASH,
                            },
                            {
                                "path": "org/example/1/example-1.pom",
                                "urls": [self.url],
                                "sha256": HASH,
                            },
                        ],
                    }
                )
            )
            cache = self.build(f"a.fetchMavenCache {{ lock = {lock}; }}")
            artifact = cache / "org/example/1/example-1.jar"
            self.assertTrue(artifact.is_symlink())
            self.assertEqual(artifact.read_bytes(), PAYLOAD)
            marker = (cache / "org/example/1/_remote.repositories").read_text()
            self.assertIn("example-1.jar>=", marker)
            self.assertIn("example-1.pom>=", marker)
            prepare = self.build("a.prepareCache") / "bin/prepare-dependency-cache"
            target = Path(directory) / "cache"
            subprocess.run([prepare, cache, target], check=True)
            (target / "org/example/1/example-1.jar").write_text("modified")
            self.assertEqual(artifact.read_bytes(), PAYLOAD)
            self.assertNotEqual(
                subprocess.run(
                    [prepare, cache, target], capture_output=True, check=False
                ).returncode,
                0,
            )

    def test_wheelhouse_and_incomplete_hash(self):
        expr = f'a.fetchPythonWheelhouse {{ artifacts = [{{ path = "demo.whl"; urls = ["{self.url}"]; sha256 = "{HASH}"; }}]; }}'
        cache = self.build(expr)
        self.assertEqual((cache / "demo.whl").read_bytes(), PAYLOAD)
        self.assertNotEqual(
            self.evaluate(
                '(a.fetchPythonWheelhouse { artifacts = [{path = "source.tar.gz";}]; }).drvPath'
            ).returncode,
            0,
        )
        self.assertNotEqual(
            self.evaluate(
                '(a.fetchDependency { name = "x"; urls = []; sha256 = ""; }).drvPath'
            ).returncode,
            0,
        )

    def test_application_is_network_isolated(self):
        result = self.build("""a.mkOfflineApplication { name = "offline-adapter-test";
          dontUnpack = true; nativeBuildInputs = [pkgs.python3];
          buildPhase = ''
            runHook preBuild
            python - <<'CHECK'
import socket
s = socket.socket()
s.settimeout(1)
try:
    s.connect(('1.1.1.1', 443))
except OSError:
    pass
else:
    raise RuntimeError('network available')
CHECK
          '';
          installPhase = "echo passed > $out";
        }""")
        self.assertEqual(result.read_text().strip(), "passed")
        self.assertNotEqual(
            self.evaluate(
                '(a.mkOfflineApplication { name = "bad"; outputHash = "bad"; }).drvPath'
            ).returncode,
            0,
        )

    def test_bun_workspace_files_and_cache_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            (project / "packages/lib").mkdir(parents=True)
            for file in ["package.json", "bun.lock", "packages/lib/package.json"]:
                (project / file).write_text("{}")
            expected = root / "expected"
            expected.mkdir()
            (expected / "value").write_text("workspace cached\n")
            hash_value = subprocess.check_output(
                ["nix", "hash", "path", "--type", "sha256", str(expected)], text=True
            ).strip()
            expr = f'''let bun = pkgs.writeShellScriptBin "bun" ''
              test -f packages/lib/package.json
              test "$*" = "install --frozen-lockfile --ignore-scripts"
              echo "workspace cached" > "$BUN_INSTALL_CACHE_DIR/value"
            ''; in a.fetchBunCache {{ inherit bun; source = {project}; hash = "{hash_value}";
              projectFiles = ["package.json" "bun.lock" "packages/lib/package.json"];
            }}'''
            result = self.build(expr)
            self.assertEqual((result / "value").read_text(), "workspace cached\n")

    def test_npm_and_system_archive_validation(self):
        for manager, suffix in [("Apt", "deb"), ("Apk", "apk")]:
            expr = f'a.fetch{manager}Cache {{ artifacts = [{{ path = "fixture.{suffix}"; urls = ["{self.url}"]; sha256 = "{HASH}"; }}]; }}'
            self.assertEqual(
                (self.build(expr) / f"fixture.{suffix}").read_bytes(), PAYLOAD
            )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "package.json").write_text('{"name":"fixture","version":"1.0.0"}')
            lock = {
                "name": "fixture",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "packages": {"": {"name": "fixture", "version": "1.0.0"}},
            }
            (source / "package-lock.json").write_text(json.dumps(lock))
            cache = self.build(f"a.fetchNpmCache {{ source = {source}; }}")
            self.assertTrue((cache / "package-lock.json").exists())
            lock["packages"]["node_modules/bad"] = {
                "resolved": "git+https://example.com/mutable.git"
            }
            (source / "package-lock.json").write_text(json.dumps(lock))
            self.assertNotEqual(
                self.evaluate(
                    f"(a.fetchNpmCache {{ source = {source}; }}).drvPath"
                ).returncode,
                0,
            )

    def test_npm_installs_pinned_package_offline(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            for name, data in {
                "package/package.json": b'{"name":"fixture-dep","version":"1.0.0","main":"index.js"}',
                "package/index.js": b"module.exports = 42;",
            }.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                tar.addfile(entry, io.BytesIO(data))
        payload = archive.getvalue()
        RESPONSES["/package.tgz"] = payload
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            package = {
                "name": "fixture",
                "version": "1.0.0",
                "dependencies": {"fixture-dep": "1.0.0"},
            }
            (source / "package.json").write_text(json.dumps(package))
            lock = {
                "name": "fixture",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "packages": {
                    "": package,
                    "node_modules/fixture-dep": {
                        "version": "1.0.0",
                        "resolved": f"http://127.0.0.1:{self.server.server_port}/package.tgz",
                        "integrity": "sha512-"
                        + base64.b64encode(hashlib.sha512(payload).digest()).decode(),
                    },
                },
            }
            (source / "package-lock.json").write_text(json.dumps(lock))
            result = self.build(f"""a.mkOfflineApplication {{
              name = "npm-offline-fixture"; src = {source};
              nativeBuildInputs = [pkgs.nodejs a.npmConfigHook];
              npmDeps = a.fetchNpmCache {{ source = {source}; }};
              buildPhase = ''
                runHook preBuild
                node -e "if (require('fixture-dep') !== 42) process.exit(1)"
              '';
              installPhase = "echo passed > $out";
            }}""")
            self.assertEqual(result.read_text().strip(), "passed")

    def test_bun_missing_inputs_are_rejected(self):
        self.assertNotEqual(
            self.evaluate(
                '(a.fetchBunCache { bun = pkgs.bun; source = /tmp; hash = "bad"; projectFiles = ["package.json"]; }).drvPath'
            ).returncode,
            0,
        )


if __name__ == "__main__":
    unittest.main()
