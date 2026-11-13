"""``mango clean`` removes only what one environment's workspace owns."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mango import clean, launcher

MAIN = "mango.local/main@sha256:" + "a" * 64
EGRESS = "mango.local/egress@sha256:" + "b" * 64
GRADER = "mango.local/grader@sha256:" + "c" * 64


def write_workspace(path, services, verifiers=None):
    path.mkdir(parents=True)
    (path / ".harness.lock").write_text("1\n")
    (path / "images.json").write_text(
        json.dumps({"services": services, "verifiers": verifiers or {}, "egress_image": EGRESS})
    )


class CleanTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.root = self.base / "environment"
        self.root.mkdir()
        (self.root / "default.nix").write_text("{ harness }: {}")
        self.workspace = self.base / "mango-build" / "environment"

    def without_tools(self):
        return patch.object(clean.shutil, "which", return_value=None)

    def test_image_tags_cover_services_verifiers_and_egress(self):
        write_workspace(self.workspace, {"main": MAIN}, {"tests": {"main": GRADER}})
        self.assertEqual(
            clean.image_tags(self.workspace),
            {
                "mango.local/main:" + "a" * 16,
                "mango.local/egress:" + "b" * 16,
                "mango.local/grader:" + "c" * 16,
            },
        )

    def test_foreign_and_malformed_references_are_ignored(self):
        write_workspace(
            self.workspace, {"main": "docker.io/library/ubuntu@sha256:" + "d" * 64, "x": "mango.local/x:tag"}
        )
        self.assertEqual(clean.image_tags(self.workspace), {"mango.local/egress:" + "b" * 16})
        (self.workspace / "images.json").write_text("not json")
        self.assertEqual(clean.image_tags(self.workspace), set())

    def test_images_used_by_a_sibling_workspace_are_kept(self):
        write_workspace(self.workspace, {"main": MAIN})
        write_workspace(self.workspace.parent / "other", {})  # shares only egress
        with patch.object(
            clean.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ) as run:
            removed, kept = clean.remove_images(self.workspace)
        self.assertEqual(removed, ["mango.local/main:" + "a" * 16])
        self.assertEqual(kept, ["mango.local/egress:" + "b" * 16])
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [["docker", "image", "rm", "mango.local/main:" + "a" * 16]],
        )

    def test_image_still_in_use_is_reported_as_kept(self):
        write_workspace(self.workspace, {})
        failure = subprocess.CompletedProcess([], 1, "", "conflict: image is being used")
        with patch.object(clean.subprocess, "run", return_value=failure):
            removed, kept = clean.remove_images(self.workspace)
        self.assertEqual((removed, kept), ([], ["mango.local/egress:" + "b" * 16]))

    def test_results_survive_unless_requested(self):
        write_workspace(self.workspace, {})
        (self.workspace / "runtime").mkdir()
        (self.workspace / "jobs").mkdir()
        (self.workspace / "jobs" / "verdict.json").write_text("{}")
        with self.without_tools():
            summary = clean.clean(self.root, self.workspace)
        self.assertEqual(sorted(p.name for p in self.workspace.iterdir()), ["jobs"])
        self.assertEqual(summary["results_kept"], str(self.workspace / "jobs"))
        with self.without_tools():
            summary = clean.clean(self.root, self.workspace, results=True)
        self.assertFalse(self.workspace.parent.exists())
        self.assertNotIn("results_kept", summary)

    def test_refuses_a_directory_that_is_not_a_workspace(self):
        self.workspace.mkdir(parents=True)
        (self.workspace / "thesis.tex").write_text("important")
        with self.without_tools(), self.assertRaises(ValueError):
            clean.clean(self.root, self.workspace)
        self.assertTrue((self.workspace / "thesis.tex").exists())

    def test_missing_workspace_is_a_no_op(self):
        with self.without_tools():
            summary = clean.clean(self.root, self.workspace)
        self.assertFalse(self.workspace.exists())
        self.assertEqual(summary["images_removed"], [])

    def test_launcher_cleans_without_creating_a_lock(self):
        write_workspace(self.workspace, {})
        with self.without_tools(), patch("builtins.print"):
            launcher.main(["clean", str(self.root)])
        self.assertFalse((self.root / "mango.lock").exists())
        self.assertFalse(self.workspace.exists())

    def test_launcher_rejects_unknown_clean_arguments(self):
        write_workspace(self.workspace, {})
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            launcher.main(["clean", str(self.root), "--everything"])
        self.assertTrue(self.workspace.exists())


if __name__ == "__main__":
    unittest.main()
