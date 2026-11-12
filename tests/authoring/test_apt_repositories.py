import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mango.authoring import package_inputs
from mango.authoring import packages as p


class AptRepositoryTests(unittest.TestCase):
    def key(self, **changes):
        return p.RepositoryKey(**({"path": "/usr/share/keyrings/vendor.gpg",
                                   "sha256": "b" * 64,
                                   "urls": ("https://vendor.test/key.asc",)} | changes))

    def request(self, key=None, priority=600):
        return p.LockRequest(
            manager="apt", base="docker.io/library/ubuntu@sha256:" + "a" * 64,
            sources=("deb https://snapshot.test/date noble main",),
            stages=(p.PackageStage(packages=("gnupg",), install_recommends=None),
                    p.PackageStage(packages=("nodejs",), repository_keys=(key or self.key(),),
                                   extra_sources=("deb [signed-by=/usr/share/keyrings/vendor.gpg] https://vendor.test/node nodistro main",),
                                   apt_preferences=(p.AptPreference(path="/etc/apt/preferences.d/nodejs",
                                      content=f"Package: nodejs\nPin: origin vendor.test\nPin-Priority: {priority}\n"),))),
        )

    def test_key_and_policy_are_installed_after_prerequisites_before_source_refresh(self):
        script = p.apt_script(self.request())
        self.assertLess(script.index("install gnupg >&2"), script.index("gpg --batch"))
        self.assertLess(script.index("gpg --batch"), script.index("signed-by="))
        self.assertLess(script.index("Pin-Priority: 600"), script.index("signed-by="))
        self.assertIn("chmod 0644 /usr/share/keyrings/vendor.gpg", script)
        self.assertIn("Pin: origin vendor.test", script)

    def test_keys_and_pin_policy_bind_cache_but_source_transport_path_does_not(self):
        original = self.request()
        self.assertNotEqual(original.key, self.request(self.key(sha256="c" * 64)).key)
        self.assertNotEqual(original.key, self.request(priority=1001).key)
        self.assertEqual(original.key, self.request(self.key(author_source="/different/key.asc")).key)
        with self.assertRaises(ValueError):
            p.AptPreference(path="/etc/passwd", content="Package: *")

    def test_first_transaction_can_provision_a_binary_key_before_initial_update(self):
        key = self.key(dearmor=False)
        req = self.request().model_copy(update={"stages": (p.PackageStage(
            packages=("nodejs",), repository_keys=(key,)),)})
        script = p.apt_script(req)
        self.assertLess(script.index("cp /mango/author-keys/"), script.index("update >&2"))
        self.assertNotIn("gpg --batch", script)

    def test_repository_key_mounts_verify_declared_bytes(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {p.CACHE_ENV: directory}):
            source = Path(directory) / "key.asc"
            source.write_bytes(b"public key test fixture")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            req = self.request(self.key(sha256=digest, urls=(), author_source=str(source)))
            args = package_inputs.mount_args(req)
            self.assertEqual(args[0], "-v")
            self.assertTrue(args[1].endswith(f":/mango/author-keys/{digest}:ro"))
            cached = package_inputs.materialize_key(req.stages[1].repository_keys[0])
            self.assertEqual(cached.read_bytes(), source.read_bytes())
            cached.write_bytes(b"corrupt")
            with self.assertRaises(p.LockError):
                package_inputs.mount_args(req)
            self.assertFalse(cached.exists())


if __name__ == "__main__":
    unittest.main()
