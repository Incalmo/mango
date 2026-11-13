"""Mocked tests for mango.authoring.composer_lock; no Docker, network, PHP or Composer is run.

These are NOT runtime proof: only a real ``--network none`` replay on the remote host can produce
``offline-replay-verified``. They cover the public module, the manager-generated ``.htaccess``
handling and the shared remote_common helpers.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mango.authoring import composer_lock as deps
from mango.authoring import remote_common as common

BASE = "php@sha256:" + "a" * 64
IMAGE = "sha256:" + "b" * 64
PHP = "8.3.30"
COMPOSER_SHA = "d" * 64
ARGV = ["composer", "install", "--no-dev", "--prefer-dist", "--no-interaction", "--no-progress",
        "--no-scripts", "--no-autoloader"]
HTACCESS = b"Deny from all"
URL_A = "https://api.github.com/repos/acme/a/zipball/" + "1" * 40
ARCHIVE_A = b"zip-bytes-a"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def lock_bytes() -> bytes:
    package = {"name": "acme/a", "version": "1.0.0", "type": "library",
               "dist": {"type": "zip", "url": URL_A, "reference": "r", "shasum": ""}}
    return json.dumps({"content-hash": "abc", "packages": [package], "packages-dev": []}).encode()


def make_source(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "composer.json").write_text(json.dumps({"name": "x/y", "require": {"acme/a": "^1"}}))
    (root / "composer.lock").write_bytes(lock_bytes())
    return root


def request(source: Path, **extra) -> dict:
    return {"id": "demo", "base": BASE, "prepared_image": {"id": IMAGE, "base": BASE, "apt_request_sha256": "c" * 64},
            "php_version": PHP, "composer": {"version": "2.8.12", "source": "image", "sha256": COMPOSER_SHA},
            "source_dir": str(source), "composer_argv": list(ARGV),
            "env": {"COMPOSER_ALLOW_SUPERUSER": "1"}, **extra}


class Done:
    def __init__(self, stdout="", rc=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, rc, stderr


class FakeContainer:
    """Scripted container: ``cache`` is what the author phase leaves in COMPOSER_CACHE_DIR."""

    def __init__(self, cache: dict[str, bytes], image: str, network: str, proof: bool):
        self.cache_seed, self.image, self.network = cache, image, network
        self.files: dict[str, bytes] = {}
        self.cache: dict[str, bytes] = {}
        if proof:
            self.PROOF = "docker"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def image_id(self):
        return IMAGE

    def image_platform(self):
        return "linux/amd64"

    def network_mode(self):
        return "none" if self.network == "none" else "bridge"

    def copy_in_tar(self, data: bytes, destination: str):
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            for member in archive.getmembers():
                content = archive.extractfile(member).read()
                (self.cache if destination == deps.CACHE else self.files)[member.name] = content

    def copy_out(self, source, destination: Path):
        for rel, data in self.cache.items():
            target = destination / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def exec(self, argv, env=None, workdir=None, check=True, timeout=None):
        if argv[0] == "uname":
            return Done("x86_64\n")
        if argv[0] == "mkdir":
            return Done()
        if argv[0] == "env":
            return Done("PATH=/usr/bin\nCOMPOSER_ALLOW_SUPERUSER=1\n")
        if argv == ["php", "-m"]:
            return Done("[PHP Modules]\nCore\ngd\nzip\n\n[Zend Modules]\n")
        if argv[0] == "php":
            return Done(PHP)
        if argv[0] == "sha256sum":
            return Done(f"{COMPOSER_SHA}  {argv[1]}\n")
        if argv[-2:] == ["--version", "--no-ansi"]:
            return Done("Composer version 2.8.12 2025-09-19\n")
        if argv[0] == "sh" and "composer.lock" in argv[2]:
            return Done("".join(f"{sha(d)}  {n}\n" for n, d in sorted(self.files.items())))
        if argv[0] == "sh" and "du -sk" in argv[2]:
            return Done("4\n")
        if argv[0] == "sh" and "find" in argv[2]:
            return Done("".join(f"{sha(d)}  ./{n}\n" for n, d in sorted(self.cache.items())))
        if "--help" in argv:
            return Done("  --download-only\n")
        if "validate" in argv:
            return Done()
        if "config" in argv:
            return Done(f"{deps.CACHE}/files\n")
        if "show" in argv:
            return Done(json.dumps({"installed": [{"name": "acme/a"}]}))
        if "install" in argv:
            if "--download-only" in argv:
                self.cache.update(self.cache_seed)
            return Done("ok\n")
        raise AssertionError(f"unexpected exec {argv}")


def resolve(tmp: str, cache: dict[str, bytes], proof: bool = True, **over) -> dict:
    source = make_source(Path(tmp) / "src")

    def fetch(url, destination, max_bytes):
        destination.write_bytes(ARCHIVE_A)
        return sha(ARCHIVE_A)

    with mock.patch.dict(os.environ, {"MANGO_REMOTE_AUTHOR": "1"}):
        return deps.resolve(request(source, **over), fetch=fetch,
                            container_factory=lambda image, network="bridge": FakeContainer(cache, image, network, proof))


ARCHIVE_PATH = "files/acme/a/1.0.0.0-r.zip"


class GeneratedFileValidation(unittest.TestCase):
    GOOD = {"path": ".htaccess", "content": "Deny from all", "sha256": sha(HTACCESS)}

    def test_known_file_is_exactly_thirteen_ascii_bytes(self):
        self.assertEqual(len(HTACCESS), 13)
        self.assertEqual(deps.HTACCESS_SHA256, sha(HTACCESS))
        self.assertEqual(deps.check_generated_files([self.GOOD]), [self.GOOD])
        self.assertEqual(deps.check_generated_files(None), [])

    def test_tampering_is_rejected(self):
        cases = {
            "path": {**self.GOOD, "path": "other"},
            "traversal": {**self.GOOD, "path": "../.htaccess"},
            "nested": {**self.GOOD, "path": "files/.htaccess"},
            "content": {**self.GOOD, "content": "Allow from all", "sha256": sha(b"Allow from all")},
            "newline": {**self.GOOD, "content": "Deny from all\n", "sha256": sha(b"Deny from all\n")},
            "non-ascii": {**self.GOOD, "content": "Deny from allé"},
            "hash": {**self.GOOD, "sha256": "0" * 64},
            "uppercase hash": {**self.GOOD, "sha256": self.GOOD["sha256"].upper()},
            "extra key": {**self.GOOD, "urls": ["https://example.com"]},
            "missing key": {"path": ".htaccess", "content": "Deny from all"},
            "not a dict": "x",
        }
        for name, entry in cases.items():
            with self.subTest(name), self.assertRaises(deps.ResolverError) as ctx:
                deps.check_generated_files([entry])
            self.assertEqual(ctx.exception.code, "invalid-generated-file")

    def test_duplicates_collisions_and_shape(self):
        for value in ([self.GOOD, self.GOOD], "x", {"path": ".htaccess"}, [self.GOOD] * 3):
            with self.subTest(value=value), self.assertRaises(deps.ResolverError):
                deps.check_generated_files(value)
        for artifact in (".htaccess", ".htaccess/x.zip"):
            with self.subTest(artifact=artifact), self.assertRaises(deps.ResolverError):
                deps.check_generated_files([self.GOOD], [artifact])
        deps.check_generated_files([self.GOOD], [ARCHIVE_PATH])


class BindCache(unittest.TestCase):
    ARCHIVE = {"name": "acme/a", "version": "1.0.0", "url": URL_A, "sha256": sha(ARCHIVE_A), "size": len(ARCHIVE_A)}

    def bind(self, cache: dict[str, bytes], accepted=()):
        return deps.bind_cache({p: sha(d) for p, d in cache.items()}, [self.ARCHIVE], list(accepted))

    def test_htaccess_is_explained_as_generated_and_kept(self):
        bound = self.bind({ARCHIVE_PATH: ARCHIVE_A, ".htaccess": HTACCESS})
        self.assertEqual(bound["generated_files"], [{"path": ".htaccess", "content": "Deny from all", "sha256": sha(HTACCESS)}])
        self.assertEqual([a["path"] for a in bound["artifacts"]], [ARCHIVE_PATH])
        self.assertEqual(bound["accepted_unmapped"], [])

    def test_absent_htaccess_is_backward_compatible(self):
        self.assertEqual(self.bind({ARCHIVE_PATH: ARCHIVE_A})["generated_files"], [])

    def test_wrong_content_is_rejected_not_accepted(self):
        for content in (b"Allow from all", HTACCESS + b"\n", b""):
            with self.subTest(content=content), self.assertRaises(deps.ResolverError) as ctx:
                self.bind({ARCHIVE_PATH: ARCHIVE_A, ".htaccess": content})
            self.assertEqual(ctx.exception.code, "generated-file-mismatch")

    def test_it_cannot_be_smuggled_through_accept_unmapped_cache(self):
        with self.assertRaises(deps.ResolverError) as ctx:
            self.bind({ARCHIVE_PATH: ARCHIVE_A, ".htaccess": HTACCESS}, [{"path": ".htaccess", "sha256": sha(HTACCESS)}])
        self.assertEqual(ctx.exception.code, "invalid-request")

    def test_other_unknown_files_still_fail_closed(self):
        for path in ("files/acme/a/other.zip", "repo/x.json", "sub/.htaccess", ".htaccess2"):
            with self.subTest(path=path), self.assertRaises(deps.ResolverError) as ctx:
                self.bind({ARCHIVE_PATH: ARCHIVE_A, path: b"x"})
            self.assertEqual(ctx.exception.code, "unexplained-cache-file")


class ResolveWithGeneratedFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_version_is_three(self):
        self.assertEqual(deps.RESOLVER_VERSION, "3")

    def test_lock_receipt_and_replay_carry_the_file(self):
        result = resolve(self.tmp.name, {ARCHIVE_PATH: ARCHIVE_A, ".htaccess": HTACCESS})
        self.assertEqual(result["verification"], "offline-replay-verified")
        entry = {"path": ".htaccess", "content": "Deny from all", "sha256": sha(HTACCESS)}
        self.assertEqual(result["lock"]["generated_files"], [entry])
        self.assertEqual([a["path"] for a in result["lock"]["artifacts"]], [ARCHIVE_PATH])
        for artifact in result["lock"]["artifacts"]:  # no fake URL for generated content
            self.assertTrue(all(u == URL_A for u in artifact["urls"]))
        evidence = result["evidence"]
        self.assertEqual(evidence["generated_files"], [entry])
        self.assertEqual(evidence["cache_inventory"], [
            {"path": ".htaccess", "sha256": sha(HTACCESS), "kind": "generated"},
            {"path": ARCHIVE_PATH, "sha256": sha(ARCHIVE_A), "kind": "archive"}])
        stage = next(r for r in evidence["receipts"] if r["stage"] == "cache-bound")
        self.assertEqual((stage["artifacts"], stage["generated"]), (1, 1))
        self.assertEqual(result["offline"]["network"], "none")
        self.assertEqual(evidence["offline_replay"]["network_mode"], "none")
        self.assertEqual(evidence["offline_replay"]["argv"][1:], ARGV[1:])

    def test_replay_cache_is_seeded_with_the_exact_file(self):
        seen = {}

        class Spy(FakeContainer):
            def copy_in_tar(self, data, destination):
                super().copy_in_tar(data, destination)
                if destination == deps.CACHE:
                    seen.update(self.cache)

        source = make_source(Path(self.tmp.name) / "src")
        with mock.patch.dict(os.environ, {"MANGO_REMOTE_AUTHOR": "1"}):
            deps.resolve(request(source), fetch=lambda u, d, m: (d.write_bytes(ARCHIVE_A), sha(ARCHIVE_A))[1],
                         container_factory=lambda i, n="bridge": Spy({ARCHIVE_PATH: ARCHIVE_A, ".htaccess": HTACCESS}, i, n, True))
        self.assertEqual(seen, {ARCHIVE_PATH: ARCHIVE_A, ".htaccess": HTACCESS})

    def test_without_htaccess_the_lock_has_no_generated_files_key(self):
        result = resolve(self.tmp.name, {ARCHIVE_PATH: ARCHIVE_A})
        self.assertNotIn("generated_files", result["lock"])
        self.assertEqual(result["evidence"]["generated_files"], [])

    def test_tampered_htaccess_in_the_author_cache_fails(self):
        with self.assertRaises(deps.ResolverError) as ctx:
            resolve(self.tmp.name, {ARCHIVE_PATH: ARCHIVE_A, ".htaccess": b"Allow from all"})
        self.assertEqual(ctx.exception.code, "generated-file-mismatch")

    def test_mock_container_never_reaches_verified(self):
        result = resolve(self.tmp.name, {ARCHIVE_PATH: ARCHIVE_A, ".htaccess": HTACCESS}, proof=False)
        self.assertEqual(result["verification"], "unproven-no-real-offline-replay")
        self.assertTrue(any("NOT verified" in w for w in result["evidence"]["warnings"]))

    def test_signature_changes_with_resolver_version(self):
        source = make_source(Path(self.tmp.name) / "src")
        first = deps.request_signature(request(source))
        with mock.patch.object(deps, "RESOLVER_VERSION", "2"):
            self.assertNotEqual(first, deps.request_signature(request(source)))


class RemoteCommon(unittest.TestCase):
    def test_gate(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(common.ResolverError) as ctx:
            common.require_remote_author()
        self.assertEqual(ctx.exception.code, "gate")
        with mock.patch.dict(os.environ, {"MANGO_REMOTE_AUTHOR": "1"}):
            common.require_remote_author()

    def test_redaction_hashing_hosts(self):
        self.assertEqual(common.redact("see https://u:p@example.com/x?token=1"), "see https://<redacted>@example.com/x?<redacted>")
        self.assertEqual(common.sha256_hex(b"a"), hashlib.sha256(b"a").hexdigest())
        self.assertTrue(common.public_host("api.github.com"))
        for host in ("localhost", "10.0.0.1", "intranet", "x.internal", None, ""):
            self.assertFalse(common.public_host(host), host)

    def test_file_digests_and_atomic_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f"
            path.write_bytes(b"abc")
            digests = common.file_digests(path)
            self.assertEqual(digests["sha256"], hashlib.sha256(b"abc").hexdigest())
            self.assertEqual(digests["sha1"], hashlib.sha1(b"abc").hexdigest())
            common.atomic_json(Path(tmp) / "sub" / "out.json", {"a": 1})
            self.assertEqual(json.loads((Path(tmp) / "sub" / "out.json").read_text()), {"a": 1})

    def test_download_refuses_unsafe_urls_before_any_request(self):
        for url in ("http://example.com/x", "https://example.com/x?token=1", "https://u@example.com/x", "https://localhost/x"):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as tmp, self.assertRaises(common.ResolverError):
                common.download(url, Path(tmp) / "x", 10)

    def test_run_wraps_timeout_and_failure(self):
        with self.assertRaises(common.ResolverError) as ctx:
            common.run([sys.executable, "-c", "import sys; sys.exit(3)"])
        self.assertEqual(ctx.exception.code, "docker")
        self.assertEqual(common.run([sys.executable, "-c", "print(1)"]).stdout.strip(), "1")

    def test_patching_composer_lock_run_is_seen_by_its_container(self):
        calls = []
        with mock.patch.object(deps, "run", lambda *a, **k: calls.append(a) or Done("sha256:" + "b" * 64)):
            self.assertEqual(deps.Container("img").image_id(), "sha256:" + "b" * 64)
        self.assertEqual(len(calls), 1)


class Cli(unittest.TestCase):
    def test_python_dash_m_works_and_is_gated(self):
        env = {**os.environ}
        env.pop("MANGO_REMOTE_AUTHOR", None)
        src = str(Path(deps.__file__).resolve().parents[2])
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        helped = subprocess.run([sys.executable, "-m", "mango.authoring.composer_lock", "--help"], env=env, capture_output=True, text=True)
        self.assertEqual(helped.returncode, 0, helped.stderr)
        self.assertIn("mango-lock-composer", helped.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            blocked = subprocess.run([sys.executable, "-m", "mango.authoring.composer_lock", str(Path(tmp) / "r.json"),
                                      "--output", str(Path(tmp) / "o.json")], env=env, capture_output=True, text=True)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("MANGO_REMOTE_AUTHOR", blocked.stderr)


if __name__ == "__main__":
    unittest.main()
