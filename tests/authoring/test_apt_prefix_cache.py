import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mango.authoring import apt_prefix_cache as cache
from mango.authoring.packages import LockError, LockRequest, PackageStage, apt_script


def request(**changes):
    return LockRequest(**({"manager": "apt", "base": "docker.io/library/debian@sha256:" + "a" * 64,
                          "sources": ("deb https://snapshot.test/date trixie main",),
                          "stages": (PackageStage(packages=("ca-certificates",)),
                                     PackageStage(packages=("curl",)))} | changes))


class PrefixCacheTests(unittest.TestCase):
    def test_key_binds_base_repository_timeline_and_exact_order(self):
        original = request()
        key = cache.key_for(cache.descriptor(original, 1))
        changed_tail = request(stages=(original.stages[0], PackageStage(packages=("git",))))
        self.assertEqual(key, cache.key_for(cache.descriptor(changed_tail, 1)))
        for changed in (request(base="docker.io/library/debian@sha256:" + "b" * 64),
                        request(sources=("deb https://snapshot.test/other trixie main",)),
                        request(stages=(PackageStage(packages=("ca-certificates",),
                                                     sources=("deb https://different.test trixie main",)),
                                        original.stages[1]))):
            self.assertNotEqual(key, cache.key_for(cache.descriptor(changed, 1)))

    def test_resumed_script_keeps_repositories_until_explicit_change(self):
        script = apt_script(request(), start=1, initialize=False)
        self.assertNotIn("apt-get -qq -o", script.split("echo")[0])
        self.assertNotIn("rm -f /etc/apt", script)
        self.assertNotIn("ca-certificates", script)
        self.assertIn("@@mango-stage 1", script)
        self.assertIn("--print-uris install curl", script)

    def test_shared_prefix_reuses_exact_image_and_preserves_uri_outputs(self):
        images, containers, calls = {}, {}, []

        def docker(*args, check=True):
            calls.append(args)
            output = ""
            if args[0] == "run":
                name = args[args.index("--name") + 1]
                key = args[args.index("--label") + 1].split("=", 1)[1]
                containers[name] = key
                self.assertRegex(args[-1], r"install (ca-certificates|git) >&2")
                marker = re.search(r"@@mango-stage \d+", args[-1]).group()
                output = marker + "\n"
            elif args[0] == "commit":
                output = "sha256:" + containers[args[-1]]
                images[output] = {"Id": output, "Size": 100,
                                  "Config": {"Labels": {cache.LABEL: containers[args[-1]]}}}
            elif args[:2] == ("image", "inspect"):
                output = json.dumps([images[args[2]]])
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"MANGO_LOCK_CACHE": directory}), \
                patch.object(cache, "docker", side_effect=docker), \
                patch.object(cache.p, "_run_in_base", return_value="@@mango-stage 1\n") as final:
            first = cache.resolve_output(request())
            second = cache.resolve_output(request(stages=(request().stages[0], PackageStage(packages=("git",)))))
            self.assertEqual(first, second)
            self.assertEqual(sum(c[0] == "run" for c in calls), 1)
            prefix_image = "sha256:" + cache.key_for(cache.descriptor(request(), 1))
            self.assertEqual(final.call_args.args[0].base, prefix_image)
            self.assertIn("install git", final.call_args.args[1])
            prepared = request(stages=(request().stages[0], PackageStage(packages=("git",))))
            self.assertEqual(cache.prepare_image(prepared),
                             "sha256:" + cache.key_for(cache.descriptor(prepared, 2)))
            self.assertEqual(sum(c[0] == "run" for c in calls), 2)
            receipt = Path(directory) / "apt-prefixes" / (prefix_image.removeprefix("sha256:") + ".json")
            images[prefix_image]["Config"]["Labels"][cache.LABEL] = "tampered"
            self.assertIsNone(cache.read_receipt(receipt, cache.descriptor(request(), 1)))

    def test_failed_transaction_does_not_publish_snapshot(self):
        calls = []

        def docker(*args, check=True):
            calls.append(args)
            if args[0] == "run":
                raise LockError("transaction failed")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"MANGO_LOCK_CACHE": directory}), \
                patch.object(cache, "docker", side_effect=docker):
            with self.assertRaisesRegex(LockError, "transaction failed"):
                cache.resolve_output(request())
            self.assertFalse(list((Path(directory) / "apt-prefixes").glob("*.json")))
            self.assertFalse(any(c[0] == "commit" for c in calls))
            self.assertTrue(any(c[0] == "rm" for c in calls))


if __name__ == "__main__":
    unittest.main()
