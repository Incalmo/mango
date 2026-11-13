"""Declarative containers and packages compile to real images and pinned outputs."""

import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2]
PREFIX = f"""let h = import {HARNESS} {{}}; p=h.pkgs;
 c=import {HARNESS}/lib/container.nix {{pkgs=p;}};
 packages=import {HARNESS}/lib/packages.nix {{pkgs=p;}}; in """


def build(expression):
    result = subprocess.run(
        [
            "nix-build",
            "--no-out-link",
            "--option",
            "builders",
            "",
            "--expr",
            PREFIX + expression,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return Path(result.stdout.strip().splitlines()[-1])


def inspect(image):
    with tempfile.TemporaryDirectory() as directory:
        layout = Path(directory) / "oci"
        subprocess.run(
            ["skopeo", "--insecure-policy", "copy", f"nix:{image}", f"oci:{layout}"],
            check=True,
            capture_output=True,
        )
        blobs = layout / "blobs/sha256"

        def blob(descriptor):
            return blobs / descriptor["digest"].split(":")[1]

        index = json.loads((layout / "index.json").read_text())
        manifest = json.loads(blob(index["manifests"][0]).read_text())
        config = json.loads(blob(manifest["config"]).read_text())["config"]
        entries, contents = {}, {}
        for layer in manifest["layers"]:
            with tarfile.open(blob(layer)) as archive:
                for member in archive:
                    name = member.name.removeprefix("./").strip("/")
                    entries[name] = member
                    if member.isfile():
                        contents[name] = archive.extractfile(member).read()
        return config, entries, contents, manifest


class Containers(unittest.TestCase):
    def test_layers_retain_the_bytes_their_digests_describe(self):
        image = build('c { name="retained-layer"; files."/app/data".text="application output"; }')
        layers = json.loads(image.read_text())["layers"]
        self.assertTrue(layers)
        for layer in layers:
            blob = Path(layer["layer-path"])
            data = blob.read_bytes()
            self.assertEqual(layer["digest"], "sha256:" + hashlib.sha256(data).hexdigest())
            self.assertEqual(layer["size"], len(data))
        # Model another builder having different bytes at a dependency path.
        # Export must read the retained blob, not rewalk the source paths.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "changed").write_text("different realization")
            document = json.loads(image.read_text())
            for layer in document["layers"]:
                layer["paths"] = [{"path": str(root / "changed")}]
            cached = root / "cached-image.json"
            cached.write_text(json.dumps(document))
            _, _, contents, _ = inspect(cached)
            self.assertEqual(contents["app/data"], b"application output")

    def test_scratch_root_account_and_explicit_override(self):
        image = build('c { name="scratch-root-account"; }')
        config, entries, contents, _ = inspect(image)
        self.assertEqual(config["User"], "root")
        self.assertEqual(contents["etc/passwd"], b"root:x:0:0:root:/root:/bin/sh\n")
        self.assertEqual(contents["etc/group"], b"root:x:0:\n")
        self.assertEqual(entries["etc/passwd"].mode, 0o644)
        custom = build(
            'c { name="scratch-custom-account"; user="app"; '
            'files."/etc/passwd".text="app:x:1000:1000::/:/bin/sh\\n"; }'
        )
        config, _, contents, _ = inspect(custom)
        self.assertEqual(config["User"], "app")
        self.assertEqual(contents["etc/passwd"], b"app:x:1000:1000::/:/bin/sh\n")

    def test_files_modes_symlinks_packages_and_oci_config(self):
        image = build(r"""let source = p.runCommand "declarative-tree" {} ''
          mkdir -p $out; echo data > $out/data; echo executable > $out/run; chmod +x $out/run;
        ''; in c {
          name="declarative-test";
          packages=[p.coreutils];
          files={
            "/etc/readonly"={text="configuration";mode="0444";};
            "/etc/private"={text="private";mode="0600";};
            "/opt/tree"={inherit source;};
            "/usr/bin/env"={symlink="${p.coreutils}/bin/env";};
          };
          directories=[{path="/work";uid=123;gid=456;mode="0750";} {path="/space dir";}];
          environment={HOME="/work";VALUE="declared";};
          command=["echo"];entrypoint=[];workingDir="/work";user="123:456";ports=[8080 "5353/udp"];
        }""")
        config, entries, contents, _ = inspect(image)
        self.assertEqual(config["Cmd"], ["echo"])
        self.assertEqual(config.get("Entrypoint") or [], [])
        self.assertEqual(config["WorkingDir"], "/work")
        self.assertEqual(config["User"], "123:456")
        self.assertIn("VALUE=declared", config["Env"])
        self.assertIn("8080/tcp", config["ExposedPorts"])
        self.assertIn("5353/udp", config["ExposedPorts"])
        self.assertEqual(entries["etc/readonly"].mode, 0o444)
        self.assertEqual(entries["etc/private"].mode, 0o600)
        self.assertEqual(
            (entries["work"].uid, entries["work"].gid, entries["work"].mode),
            (123, 456, 0o750),
        )
        self.assertTrue(entries["space dir"].isdir())
        self.assertEqual(contents["opt/tree/data"], b"data\n")
        self.assertTrue(entries["opt/tree/run"].mode & 0o111)
        self.assertTrue(entries["usr/bin/env"].issym())
        self.assertIn(entries["usr/bin/env"].linkname.lstrip("/"), entries)
        self.assertTrue(entries["bin/echo"].issym())

    def test_pinned_base_layer_and_inherited_configuration(self):
        image = build(r"""c {
          name="declarative-base-test";
          base={image="docker.io/library/alpine";digest="sha256:bc21487101ae47172509130ee1092cc273a831968d64b617776d1f69c81e42ab";hash="sha256-VJrEWNzP7YSUs7FXJ6UIbPd7YlPrIGXCxJ2UAbL9iCc=";};
          files."/etc/declarative".text="base preserved";
          packages=[p.coreutils];environment.EXTRA="yes";
        }""")
        config, entries, contents, manifest = inspect(image)
        self.assertEqual(config["Cmd"], ["/bin/sh"])
        self.assertEqual(contents["etc/declarative"], b"base preserved")
        self.assertIn("bin/busybox", entries)
        self.assertTrue(entries["opt/nix-tools"].issym())
        self.assertIn("EXTRA=yes", config["Env"])
        path = next(item for item in config["Env"] if item.startswith("PATH="))
        self.assertTrue(path.startswith("PATH=/opt/nix-tools/bin:"))
        self.assertGreaterEqual(len(manifest["layers"]), 2)
        self.assertTrue(entries["bin/echo"].issym())
        self.assertNotIn("/nix/store", entries["bin/echo"].linkname)

    def test_explicit_empty_command_clears_base_and_invalid_ports_rejected(self):
        image = build(r"""c {
          name="declarative-base-reset";
          base={image="docker.io/library/alpine";digest="sha256:bc21487101ae47172509130ee1092cc273a831968d64b617776d1f69c81e42ab";hash="sha256-VJrEWNzP7YSUs7FXJ6UIbPd7YlPrIGXCxJ2UAbL9iCc=";};
          command=[];entrypoint=[];user="123";workingDir="/work";
        }""")
        config = json.loads(image.read_text())["image-config"]
        self.assertEqual(config["Cmd"], [])
        self.assertEqual(config["Entrypoint"], [])
        self.assertEqual(config["User"], "123")
        self.assertEqual(config["WorkingDir"], "/work")
        for ports in ("[0]", "[65536]", '["80/sctp"]', "[true]"):
            result = subprocess.run(
                [
                    "nix-instantiate",
                    "--eval",
                    "--strict",
                    "--expr",
                    PREFIX + f'(c {{name="invalid-port";ports={ports};}}).drvPath',
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Container ports must", result.stderr)

    def test_source_patch_build_and_reusable_patched_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "message").write_text("before\n")
            patch = root / "change.patch"
            patch.write_text(
                "--- a/message\n+++ b/message\n@@ -1 +1 @@\n-before\n+after\n"
            )
            expression = f"""packages.mkSourcePackage {{
              pname="patched-package";version="1";src={source};patches=[{patch}];
              build="cp message built";
              install="mkdir -p $out/share; cp built $out/share/message";
              doInstallCheck=true; installCheckPhase="grep -qx after $out/share/message";
            }}"""
            package = build(expression)
            self.assertEqual((package / "share/message").read_text(), "after\n")
            patched = build(f"({expression}).patchedSource")
            self.assertEqual((patched / "message").read_text(), "after\n")
            self.assertEqual((source / "message").read_text(), "before\n")

    def test_source_patch_flags_control_the_patch_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "message").write_text("prefix\nbefore\n")
            patch = root / "change.patch"
            patch.write_text("--- message\n+++ message\n@@ -1 +1 @@\n-before\n+after\n")
            expression = f"""packages.mkSourcePackage {{
              pname="custom-patch-flags"; version="1"; src={source}; patches=[{patch}];
              patchFlags=["-p0" "--no-backup-if-mismatch"];
              build="test ! -e message.orig";
              install="mkdir -p $out; cp message $out/message";
            }}"""
            package = build(expression)
            patched = build(f"({expression}).patchedSource")
            self.assertEqual((package / "message").read_text(), "prefix\nafter\n")
            self.assertEqual((patched / "message").read_text(), "prefix\nafter\n")
            self.assertFalse((patched / "message.orig").exists())
            self.assertEqual((source / "message").read_text(), "prefix\nbefore\n")

    def test_pinned_binary_layout_and_custom_installer(self):
        payload = b"#!/bin/sh\nprintf pinned\\n\n"

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            package = build(f'''packages.mkBinaryPackage {{pname="pinned-binary";version="1";
              urls=["http://127.0.0.1:{server.server_port}/binary"];
              sha256="{hashlib.sha256(payload).hexdigest()}";
              files."bin/tool".mode="0755";
            }}''')
            self.assertEqual((package / "bin/tool").read_bytes(), payload)
            self.assertTrue((package / "bin/tool").stat().st_mode & 0o111)
            binary_bytes = payload
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("release/tool", binary_bytes)
            payload = buffer.getvalue()
            archive_package = build(f'''packages.mkBinaryPackage {{
              pname="pinned-zip-binary";version="1";format="archive";
              urls=["http://127.0.0.1:{server.server_port}/binary.zip"];
              sha256="{hashlib.sha256(payload).hexdigest()}";
              files."bin/tool"={{source="release/tool";mode="0755";}};
            }}''')
            self.assertEqual((archive_package / "bin/tool").read_bytes(), binary_bytes)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        installed = build(r"""packages.mkBinaryPackage {
          pname="custom-binary-install";version="1";
          inputs=p.writeTextDir "payload" "explicit bytes";
          install="mkdir -p $out/share; cp $inputs/payload $out/share/data";
          doInstallCheck=true;installCheckPhase="test -f $out/share/data";
        }""")
        self.assertEqual((installed / "share/data").read_text(), "explicit bytes")

    def test_invalid_file_specs_fail_before_build(self):
        for files in [
            '{ "/../escape".text="bad"; }',
            '{ "/file"={text="x";source=./.;}; }',
            '{ "/link"={symlink="/target";mode="0644";}; }',
        ]:
            result = subprocess.run(
                [
                    "nix-instantiate",
                    "--eval",
                    "--strict",
                    "--expr",
                    PREFIX + f'(c {{name="invalid";files={files};}}).drvPath',
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
