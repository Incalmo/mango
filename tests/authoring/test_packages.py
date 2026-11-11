"""Staged package locks: deterministic prefixes, caching and verified downloads."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mango.authoring import packages
from mango.authoring.packages import LockedFile, LockError, LockRequest, PackageStage

BASE = "docker.io/library/ubuntu@sha256:" + "a" * 64


def request(**changes):
    values = {
        "manager": "apt",
        "base": BASE,
        "sources": ("deb https://snapshot.test/ubuntu/20261001T000000Z noble main",),
        "stages": (
            PackageStage(packages=("ca-certificates", "python3")),
            PackageStage(packages=("openssh-client",), target_release="trixie",
                         extra_sources=("deb http://deb.test/debian trixie main",)),
        ),
    }
    return LockRequest(**(values | changes))


def file(name):
    return LockedFile(name=name, url=f"https://snapshot.test/{name}", sha256=hashlib.sha256(name.encode()).hexdigest())


class PackageLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environ = patch.dict(os.environ, {packages.CACHE_ENV: str(self.root / "cache")})
        environ.start()
        self.addCleanup(environ.stop)

    def test_prefixes_are_content_keyed_per_stage(self):
        first, second = request(), request()
        self.assertEqual(first.key, second.key)
        self.assertEqual(first.stage_prefix(1), f"apt/{first.key}/1")
        changed = request(sources=("deb https://snapshot.test/ubuntu/20261101T000000Z noble main",))
        self.assertNotEqual(changed.key, first.key)
        with self.assertRaises(ValueError):
            request(base="docker.io/library/ubuntu:24.04")

    def test_resolution_is_cached_and_offline_mode_refuses_misses(self):
        calls = []

        def resolver(req):
            calls.append(req)
            return [[file("python3.deb")], [file("openssh-client.deb")]]

        with patch.dict(os.environ, {packages.OFFLINE_ENV: "1"}):
            with self.assertRaisesRegex(LockError, "not cached"):
                packages.resolve(request())
        with packages.use_resolver(resolver):
            first = packages.resolve(request())
            second = packages.resolve(request())
        self.assertEqual(len(calls), 1)
        self.assertEqual(first, second)
        with patch.dict(os.environ, {packages.OFFLINE_ENV: "1"}):
            self.assertEqual(packages.resolve(request()), first)

    def test_lock_request_writes_stage_prefixes_and_replaces_them(self):
        lock = self.root / "inputs.lock.json"
        lock.write_text(json.dumps({"schema_version": 1, "artifacts": [
            {"path": "fetch/keep.tar", "urls": ["u"], "sha256": "0" * 64},
            {"path": f"{request().stage_prefix(0)}/stale.deb", "urls": ["u"], "sha256": "1" * 64},
        ]}))
        with packages.use_resolver(lambda req: [[file("python3.deb")], [file("ssh.deb")]]):
            prefixes = packages.lock_request(request(), lock)
        paths = [a["path"] for a in json.loads(lock.read_text())["artifacts"]]
        self.assertEqual(prefixes, [request().stage_prefix(0), request().stage_prefix(1)])
        self.assertIn("fetch/keep.tar", paths)
        self.assertIn(f"{prefixes[0]}/python3.deb", paths)
        self.assertIn(f"{prefixes[1]}/ssh.deb", paths)
        self.assertNotIn(f"{prefixes[0]}/stale.deb", paths)

    def test_apt_script_stages_in_order_with_release_and_extra_sources(self):
        script = packages.apt_script(request())
        self.assertLess(script.index(f"{packages.STAGE_MARKER}0"), script.index("trixie main"))
        self.assertIn("--print-uris -t trixie install openssh-client", script)
        # The first stage is installed so the second resolves against it; the last is not.
        self.assertEqual(script.count("install ca-certificates python3 >&2"), 1)
        self.assertNotIn("install openssh-client >&2", script)
        opkg = packages.opkg_script(request(manager="opkg"))
        self.assertIn(f"echo '{packages.STAGE_MARKER}1'", opkg)

    def test_download_hash_checks_the_archive_index(self):
        payload = b"debian package bytes"
        record = {
            "url": "https://snapshot.test/pool/c/curl_1_amd64.deb",
            "name": "curl_1_amd64.deb",
            "size": str(len(payload)),
            "kind": "SHA512",
            "hash": hashlib.sha512(payload).hexdigest(),
        }
        line = f"'{record['url']}' {record['name']} {record['size']} SHA512:{record['hash']}"
        self.assertEqual(packages.APT_URI.match(line).groupdict(), record)
        (self.root / record["name"]).write_bytes(payload)
        self.assertEqual(packages.download_hash(record, self.root), hashlib.sha256(payload).hexdigest())
        (self.root / record["name"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(LockError, "do not match"):
            packages.download_hash(record, self.root)


if __name__ == "__main__":
    unittest.main()
