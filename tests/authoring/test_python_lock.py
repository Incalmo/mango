import copy
import unittest

from mango.authoring.python_lock import report_artifacts


class PythonLockTests(unittest.TestCase):
    def setUp(self):
        self.report = {"version": "1", "install": [{"download_info": {
            "url": "https://files.pythonhosted.org/demo-1-py3-none-any.whl",
            "archive_info": {"hashes": {"sha256": "a" * 64}},
        }}]}

    def test_wheel_is_a_hashed_fetch_input(self):
        artifact, = report_artifacts(self.report, "python/main")
        self.assertEqual(artifact["path"], "python/main/demo-1-py3-none-any.whl")
        self.assertEqual(artifact["sha256"], "a" * 64)

    def test_rejects_unbuildable_or_unshareable_inputs(self):
        for url in ["file:///tmp/demo.whl", "https://host/demo.tar.gz",
                    "https://user:secret@host/demo.whl", "https://host/demo.whl?token=x",
                    "https://host/a%2Fb.whl"]:
            with self.subTest(url=url):
                self.report["install"][0]["download_info"]["url"] = url
                with self.assertRaises(ValueError):
                    report_artifacts(self.report, "python/main")

    def test_rejects_missing_hash_and_conflicting_names(self):
        other = copy.deepcopy(self.report["install"][0])
        other["download_info"]["archive_info"]["hashes"]["sha256"] = "b" * 64
        self.report["install"].append(other)
        with self.assertRaises(ValueError):
            report_artifacts(self.report, "python/main")
        self.report["install"] = [other]
        other["download_info"]["archive_info"] = {}
        with self.assertRaises(ValueError):
            report_artifacts(self.report, "python/main")

    def test_rejects_unsafe_prefix(self):
        for prefix in ["", "/python", "../python", "python/../x", "python//x"]:
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                report_artifacts(self.report, prefix)
