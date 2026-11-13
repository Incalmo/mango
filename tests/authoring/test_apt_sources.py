import unittest

from mango.authoring.apt_sources import active_sources, mappings


class AptSourceMappingTests(unittest.TestCase):
    def test_deb822_multiple_suites_and_separate_security_origin(self):
        runtime = {
            "/etc/apt/sources.list.d/ubuntu.sources": "# distro\nTypes: deb\nURIs: http://archive.test/ubuntu/\nSuites: noble noble-updates\nComponents: main universe\nSigned-By: /key.gpg\n\nTypes: deb\nURIs: http://security.test/ubuntu/\nSuites: noble-security\nComponents: main universe\nSigned-By: /key.gpg\n"
        }
        result = mappings(
            {
                "sources": [
                    "deb https://snapshot.test/ubuntu/DATE noble main universe",
                    "deb https://snapshot.test/ubuntu/DATE noble-security main universe",
                ]
            },
            runtime,
        )
        self.assertEqual(
            result[0]["runtime_uri"], "http://archive.test/ubuntu/dists/noble/"
        )
        self.assertEqual(
            result[1]["runtime_uri"],
            "http://security.test/ubuntu/dists/noble-security/",
        )

    def test_unreviewed_deb822_options_fail_closed(self):
        runtime = {
            "/etc/apt/custom.sources": "Types: deb\nURIs: http://archive.test/\nSuites: noble\nComponents: main\nEnabled: no\n"
        }
        with self.assertRaises(ValueError):
            mappings({"sources": ["deb https://snapshot.test noble main"]}, runtime)

    def test_repository_replacement_preserves_transaction_boundary(self):
        request = {"sources": ["deb https://old.test stable main"], "stages": [
            {}, {"sources": ["deb https://new.test stable main"], "extra_sources": ["deb https://extras.test extras main"]}]}
        self.assertEqual(active_sources(request, 0)["sources"], request["sources"])
        self.assertEqual(active_sources(request, 1)["sources"], ["deb https://new.test stable main", "deb https://extras.test extras main"])
        with self.assertRaisesRegex(ValueError, "transaction index"):
            active_sources(request, -1)

    def test_same_suite_at_distinct_runtime_origins_is_ambiguous(self):
        with self.assertRaisesRegex(ValueError, "Multiple runtime origins"):
            mappings({"sources": ["deb https://snapshot.test stable main"]}, {
                "/etc/apt/sources.list": "deb https://one.test stable main\ndeb https://two.test stable main\n"})
