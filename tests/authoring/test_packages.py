"""Staged package locks: deterministic prefixes, caching and verified downloads."""

import hashlib
import io
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
        self.assertIn("Acquire::ForceHash=SHA256", script)
        self.assertLess(script.index(f"{packages.STAGE_MARKER}0"), script.index("trixie main"))
        self.assertIn("--print-uris -t trixie install openssh-client", script)
        # The first stage is installed so the second resolves against it; the last is not.
        self.assertEqual(script.count("install ca-certificates python3 >&2"), 1)
        self.assertNotIn("install openssh-client >&2", script)
        opkg = packages.opkg_script(request(manager="opkg"))
        self.assertIn(f"echo '{packages.STAGE_MARKER}1'", opkg)

    def test_repository_replacement_occurs_between_transactions(self):
        req = request(stages=(PackageStage(packages=("ca-certificates",)),
            PackageStage(packages=("samba",), sources=("deb https://snapshot.test/older trixie main",)),
            PackageStage(packages=("curl",))))
        script = packages.apt_script(req)
        self.assertLess(script.index("install ca-certificates >&2"), script.index("snapshot.test/older"))
        self.assertLess(script.index("snapshot.test/older"), script.index("install samba"))
        self.assertEqual(script.count("rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*"), 2)
        original = request()
        legacy = original.model_dump(mode="json")
        legacy["stages"] = [{key: stage[key] for key in ("packages", "target_release", "extra_sources")}
                            for stage in legacy["stages"]]
        expected = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()[:16]
        self.assertEqual(original.key, expected)
        self.assertNotEqual(req.key, request(stages=(PackageStage(packages=("ca-certificates",)), PackageStage(packages=("samba",)), PackageStage(packages=("curl",)))).key)

    def test_apt_rejects_missing_hashes_and_incomplete_stage_output(self):
        req = request(stages=(PackageStage(packages=("curl",)),))
        missing_hash = "@@mango-stage 0\n'https://snapshot.test/curl.deb' curl.deb 123 \n"
        with patch.object(packages, "_run_in_base", return_value=missing_hash):
            with self.assertRaisesRegex(LockError, "index hash"):
                packages.default_resolver(req)
        with patch.object(packages, "_run_in_base", return_value=""):
            with self.assertRaisesRegex(LockError, "every requested stage"):
                packages.default_resolver(req)

    def test_old_incomplete_resolver_cache_is_not_reused(self):
        req = request()
        path = packages._cache_path(req)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"request": req.model_dump(mode="json"), "stages": [[], []]}))
        with patch.dict(os.environ, {packages.OFFLINE_ENV: "1"}):
            with self.assertRaisesRegex(LockError, "not cached"):
                packages.resolve(req)

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
        with patch.object(packages.urllib.request, "urlopen", return_value=io.BytesIO(payload)) as download:
            self.assertEqual(packages.download_hash(record, self.root), hashlib.sha256(payload).hexdigest())
            self.assertEqual(packages.download_hash(record, self.root), hashlib.sha256(payload).hexdigest())
            download.assert_called_once()
        target = self.root / "sha512" / record["hash"]
        target.write_bytes(b"tampered")
        with self.assertRaisesRegex(LockError, "do not match"):
            packages.download_hash(record, self.root)
        self.assertFalse(target.exists())

    def test_download_cache_names_do_not_collide_and_bad_download_is_not_published(self):
        first, second = b"first", b"second"
        def record(data):
            return {"url": "https://snapshot.test/same.deb", "name": "same.deb",
                    "size": str(len(data)), "kind": "SHA256",
                    "hash": hashlib.sha256(data).hexdigest()}
        with patch.object(packages.urllib.request, "urlopen",
                          side_effect=[io.BytesIO(first), io.BytesIO(second)]):
            self.assertEqual(packages.download_hash(record(first), self.root), record(first)["hash"])
            self.assertEqual(packages.download_hash(record(second), self.root), record(second)["hash"])
        self.assertEqual(len(list((self.root / "sha256").iterdir())), 2)
        wrong = record(b"expected")
        with patch.object(packages.urllib.request, "urlopen", return_value=io.BytesIO(b"wrong")):
            with self.assertRaisesRegex(LockError, "do not match"):
                packages.download_hash(wrong, self.root)
        self.assertFalse((self.root / "sha256" / wrong["hash"]).exists())
        self.assertEqual(len(list((self.root / "sha256").iterdir())), 2)

    def test_apk_checks_signatures_and_parses_hashed_closure(self):
        req = request(manager="apk", sources=("https://dl.test/v3.21/main",),
                      stages=(PackageStage(packages=("socat",)),))
        script = packages.apk_script(req)
        self.assertIn('apk verify "$file"', script)
        self.assertIn('apk add socat >&2', script)
        self.assertNotIn('--allow-untrusted', script)
        output = f"@@mango-stage 0\n@@mango-apk {'a' * 64} socat-1.apk https://dl.test/socat-1.apk\n"
        with patch.object(packages, '_run_in_base', return_value=output):
            result = packages.default_resolver(req)
        self.assertEqual(result[0][0].name, 'socat-1.apk')
        self.assertEqual(result[0][0].sha256, 'a' * 64)
        self.assertNotIn('--recursive', script)
        self.assertIn('apk add --simulate socat', script)
        with patch.object(packages, '_run_in_base', return_value='@@mango-stage 0\n'):
            self.assertEqual(packages.default_resolver(req), [[]])

    def test_apk_requires_explicit_sources_and_rejects_apt_options(self):
        with self.assertRaisesRegex(LockError, 'repository URLs'):
            packages.apk_script(request(manager='apk', sources=()))
        with self.assertRaisesRegex(LockError, 'apt release'):
            packages.apk_script(request(manager='apk'))


if __name__ == "__main__":
    unittest.main()
