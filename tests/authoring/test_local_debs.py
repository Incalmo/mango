import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mango.authoring import package_inputs
from mango.authoring import packages as p


class LocalDebTests(unittest.TestCase):
    def request(self, stage):
        return p.LockRequest(manager="apt", base="docker.io/library/ubuntu@sha256:" + "a" * 64,
                             sources=("deb https://snapshot.test/date focal main",), stages=(stage,))

    def item(self, **changes):
        return p.LocalDeb(**({"path": "/tmp/service.deb", "sha256": "b" * 64,
                             "urls": ("https://release.test/service.deb",)} | changes))

    def test_source_locations_do_not_change_the_content_key(self):
        a = self.request(p.PackageStage(packages=("/tmp/service.deb",), local_debs=(self.item(),)))
        b = self.request(p.PackageStage(packages=("/tmp/service.deb",),
                                       local_debs=(self.item(author_source="/other/verified.deb"),)))
        self.assertEqual(a.key, b.key)
        changed = self.request(p.PackageStage(packages=("/tmp/service.deb",),
                                             local_debs=(self.item(sha256="c" * 64),)))
        self.assertNotEqual(a.key, changed.key)
        with self.assertRaises(ValueError):
            p.PackageStage(packages=("curl",), install_recomendations=True)

    def test_recommendations_inherit_or_override_explicitly(self):
        default = self.request(p.PackageStage(packages=("curl",)))
        inherited = self.request(p.PackageStage(packages=("curl",), install_recommends=None))
        enabled = self.request(p.PackageStage(packages=("curl",), install_recommends=True))
        self.assertNotIn("install_recommends", default.cache_document()["stages"][0])
        self.assertIsNone(inherited.cache_document()["stages"][0]["install_recommends"])
        self.assertNotEqual(default.key, inherited.key)
        self.assertNotIn("--no-install-recommends", p.apt_script(inherited))
        self.assertIn("--install-recommends", p.apt_script(enabled))

    def test_dpkg_branch_preserves_original_force_options_and_empty_indexes(self):
        req = self.request(p.PackageStage(operation="dpkg_then_fix", local_debs=(self.item(),),
                                         dpkg_options=("--force-confold", "--force-downgrade"),
                                         clear_lists_before=True, install_recommends=None))
        script = p.apt_script(req, install_last=True)
        self.assertLess(script.index("rm -rf /var/lib/apt/lists/*"), script.index("if dpkg"))
        self.assertIn("if dpkg -i --force-confold --force-downgrade /tmp/service.deb >&2; then :; else", script)
        self.assertIn("--print-uris -f install", script)
        self.assertIn("'^Remv '", script)
        self.assertTrue(script.endswith("fi"))
        self.assertNotIn("--no-install-recommends", script)

    def test_debconf_seed_is_applied_before_the_original_transaction(self):
        plain = self.request(p.PackageStage(packages=("couchdb",)))
        seeded = self.request(p.PackageStage(packages=("couchdb",),
                                            debconf_selections=("couchdb couchdb/mode select none",)))
        self.assertNotEqual(plain.key, seeded.key)
        script = p.apt_script(seeded)
        self.assertIn("printf '%s\\n' 'couchdb couchdb/mode select none' | debconf-set-selections", script)
        self.assertLess(script.index("debconf-set-selections"), script.index("install couchdb"))
        with self.assertRaises(ValueError):
            p.PackageStage(packages=("couchdb",), debconf_selections=("first\nsecond",))

    def test_local_uri_is_not_a_download_and_metadata_is_bound(self):
        req = self.request(p.PackageStage(packages=("/tmp/service.deb",), local_debs=(self.item(),)))
        output = ("@@mango-stage 0\n'file:/tmp/service.deb' service.deb 123 \n"
                  f"@@mango-local-deb\t{'b' * 64}\t/tmp/service.deb\tservice\t1.2\tamd64\n")
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {p.CACHE_ENV: directory}), \
                patch.object(p, "_run_in_base", return_value=output):
            self.assertEqual(p.default_resolver(req), [[]])
            metadata = p.local_deb_metadata(req)[0][0]
            self.assertEqual(metadata["package"], "service")
            self.assertEqual(metadata["version"], "1.2")
            with self.assertRaisesRegex(p.LockError, "undeclared"):
                p.filter_local_deb_uris(req, output.replace("file:/tmp/service.deb", "file:/tmp/other.deb"))

    def test_local_source_hash_verification_and_deduplicated_mounts(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {p.CACHE_ENV: directory}):
            source = Path(directory) / "source.deb"
            source.write_bytes(b"explicit Debian bytes")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            item = self.item(sha256=digest, urls=(), author_source=str(source))
            req = self.request(p.PackageStage(packages=(item.path,), local_debs=(item,)))
            mounts = package_inputs.mount_args(req)
            self.assertEqual(len(mounts), 2)
            self.assertTrue(mounts[1].endswith(f":/mango/author-debs/{digest}.deb:ro"))
            target = package_inputs.materialize(item)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            target.write_bytes(b"tampered")
            with self.assertRaisesRegex(p.LockError, "SHA256"):
                package_inputs.materialize(item)
            wrong = self.item(sha256="e" * 64, urls=(), author_source=str(source))
            with self.assertRaisesRegex(p.LockError, "SHA256"):
                package_inputs.materialize(wrong)
            self.assertFalse((Path(directory) / "local-debs" / ("e" * 64 + ".deb")).exists())


if __name__ == "__main__":
    unittest.main()
