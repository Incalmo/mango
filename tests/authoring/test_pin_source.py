"""Git source pins: refs resolve to commits, and the hash is fetchgit's own."""

import subprocess
import unittest
from unittest.mock import patch

from mango.authoring import pin_source

URL = "https://example.test/app.git"
BRANCH = "1" * 40
TAG_OBJECT = "2" * 40
TAG_COMMIT = "3" * 40
HASH = "sha256-VJrEWNzP7YSUs7FXJ6UIbPd7YlPrIGXCxJ2UAbL9iCc="
MISMATCH = (
    "error: hash mismatch in fixed-output derivation:\n"
    "         specified: sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n"
    f"            got:    {HASH}\n"
)


def ls_remote(*lines):
    return subprocess.CompletedProcess(
        [], 0, "".join(f"{c}\t{n}\n" for c, n in lines), ""
    )


class ResolveTests(unittest.TestCase):
    def resolve(self, rev, *lines):
        with patch(
            "mango.authoring.pin_source.subprocess.run", return_value=ls_remote(*lines)
        ) as run:
            commit = pin_source.resolve_commit(URL, rev)
        return commit, run

    def test_a_full_commit_is_not_resolved_again(self):
        with patch("mango.authoring.pin_source.subprocess.run") as run:
            self.assertEqual(pin_source.resolve_commit(URL, BRANCH), BRANCH)
        run.assert_not_called()

    def test_head_and_branches_resolve(self):
        self.assertEqual(self.resolve("HEAD", (BRANCH, "HEAD"))[0], BRANCH)
        commit, run = self.resolve("main", (BRANCH, "refs/heads/main"))
        self.assertEqual(commit, BRANCH)
        self.assertEqual(run.call_args.args[0][:4], ["git", "ls-remote", "--", URL])

    def test_an_annotated_tag_resolves_to_its_commit(self):
        commit, _ = self.resolve(
            "v1", (TAG_OBJECT, "refs/tags/v1"), (TAG_COMMIT, "refs/tags/v1^{}")
        )
        self.assertEqual(commit, TAG_COMMIT)

    def test_a_branch_wins_over_a_tag_of_the_same_name(self):
        commit, _ = self.resolve(
            "v1", (TAG_COMMIT, "refs/tags/v1"), (BRANCH, "refs/heads/v1")
        )
        self.assertEqual(commit, BRANCH)

    def test_an_unknown_ref_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "no branch or tag named 'nope'"):
            self.resolve("nope", (BRANCH, "refs/heads/release/nope"))


class PinTests(unittest.TestCase):
    def test_the_hash_comes_from_the_harness_fetchgit(self):
        with patch(
            "mango.authoring.pin_source.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, "", MISMATCH),
        ) as run:
            value = pin_source.pin(URL, BRANCH)
        self.assertEqual(value, {"repository": URL, "commit": BRANCH, "hash": HASH})
        expression = run.call_args.args[0][-1]
        self.assertIn("h.pkgs.fetchgit", expression)
        self.assertIn(f'rev = "{BRANCH}";', expression)
        self.assertIn("hash = h.pkgs.lib.fakeHash;", expression)
        self.assertIn(f'commit = "{BRANCH}";', pin_source.nix(value))

    def test_a_failed_fetch_is_reported_not_guessed(self):
        with patch(
            "mango.authoring.pin_source.subprocess.run",
            return_value=subprocess.CompletedProcess(
                [], 1, "", "error: unable to access"
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "unable to access"):
                pin_source.pin(URL, BRANCH)


if __name__ == "__main__":
    unittest.main()
