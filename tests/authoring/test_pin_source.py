"""Git source pins: refs resolve to commits, and the hash is fetchgit's own."""

import contextlib
import io
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
    def setUp(self):
        cleanup = patch("mango.authoring.pin_source._cleanup_probe", return_value=[])
        cleanup.start()
        self.addCleanup(cleanup.stop)

    def test_the_hash_comes_from_the_harness_fetchgit(self):
        with patch(
            "mango.authoring.pin_source.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, "", MISMATCH),
        ) as run:
            value = pin_source.pin(URL, BRANCH)
        self.assertEqual(value, {"repository": URL, "commit": BRANCH, "hash": HASH})
        expression = run.call_args_list[0].args[0][-1]
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

    def test_git_metadata_options_are_bound_to_hash_and_returned(self):
        with patch(
            "mango.authoring.pin_source.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, "", MISMATCH),
        ) as run:
            value = pin_source.pin(URL, BRANCH, deep_clone=True,
                                   fetch_tags=True, fetch_submodules=False)
        expression = run.call_args_list[0].args[0][-1]
        for name, expected in {'leaveDotGit': True, 'deepClone': True,
                               'fetchTags': True, 'fetchSubmodules': False}.items():
            self.assertIs(value[name], expected)
            self.assertIn(f'{name} = {str(expected).lower()};', expression)

    def test_unrecognized_fetchgit_options_are_rejected_before_execution(self):
        with patch("mango.authoring.pin_source.subprocess.run") as run:
            with self.assertRaises(ValueError):
                pin_source.content_hash(URL, BRANCH, {'postFetch': 'bad'})
        run.assert_not_called()


STORE = "/nix/store/"
FAKE = "f" * 32
REAL = "a" * 32
DRV = "d" * 32


class FakeNix:
    """Mocked subprocess.run for the probe and its cleanup; records every call."""

    def __init__(self, build_stderr=MISMATCH, delete_rc=0, valid=True):
        self.build_stderr, self.delete_rc, self.valid = build_stderr, delete_rc, valid
        self.calls = []
        self.name = None

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == "nix-build":
            self.name = argv[-1].split('name = "')[1].split('"')[0]
            return subprocess.CompletedProcess(argv, 1, "", self.build_stderr)
        if argv[0] == "nix-instantiate":
            return subprocess.CompletedProcess(argv, 0, f"{STORE}{DRV}-{self.name}.drv\n", "")
        if argv[:3] == ["nix-store", "--query", "--outputs"]:
            return subprocess.CompletedProcess(argv, 0, f"{STORE}{FAKE}-{self.name}\n", "")
        if argv[:2] == ["nix-store", "--print-fixed-path"]:
            return subprocess.CompletedProcess(argv, 0, f"{STORE}{REAL}-{argv[-1]}\n", "")
        if argv[:2] == ["nix-store", "--check-validity"]:
            return subprocess.CompletedProcess(argv, 0 if self.valid else 1, "", "")
        if argv[:2] == ["nix-store", "--delete"]:
            return subprocess.CompletedProcess(argv, self.delete_rc, "", "still alive")
        raise AssertionError(argv)

    def deleted(self):
        return [c[2] for c in self.calls if c[:2] == ["nix-store", "--delete"]]


class CleanupTests(unittest.TestCase):
    def probe(self, fake):
        err = io.StringIO()
        with patch("mango.authoring.pin_source.subprocess.run", fake), \
                contextlib.redirect_stderr(err):
            try:
                return pin_source.content_hash(URL, BRANCH), err.getvalue()
            except RuntimeError as error:
                return error, err.getvalue()

    def test_exact_probe_outputs_are_deleted_and_hash_is_unchanged(self):
        fake = FakeNix()
        value, err = self.probe(fake)
        self.assertEqual(value, HASH)
        self.assertEqual(err, "")
        self.assertEqual(
            fake.deleted(), [f"{STORE}{FAKE}-{fake.name}", f"{STORE}{REAL}-{fake.name}"]
        )
        fixed = next(c for c in fake.calls if c[1] == "--print-fixed-path")
        self.assertEqual(fixed[2:4], ["--recursive", "sha256"])
        self.assertEqual(len(fixed[4]), 64)
        for call in fake.calls:  # no broad GC / ignore-liveness / store scan
            self.assertNotIn("--gc", call)
            self.assertNotIn("--ignore-liveness", call)
            self.assertNotIn("nix-collect-garbage", call)

    def test_hash_extraction_failure_cleans_only_the_expected_output(self):
        fake = FakeNix(build_stderr="error: unable to access")
        error, _ = self.probe(fake)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("unable to access", str(error))
        self.assertEqual(fake.deleted(), [f"{STORE}{FAKE}-{fake.name}"])

    def test_failed_cleanup_warns_but_keeps_the_hash(self):
        fake = FakeNix(delete_rc=1)
        value, err = self.probe(fake)
        self.assertEqual(value, HASH)
        self.assertIn("warning: probe cleanup", err)
        self.assertIn("still alive", err)
        self.assertIn(fake.name, err)

    def test_absent_outputs_are_not_deleted(self):
        fake = FakeNix(valid=False)
        value, _ = self.probe(fake)
        self.assertEqual(value, HASH)
        self.assertEqual(fake.deleted(), [])

    def test_unregistered_fake_hash_scratch_is_not_mistaken_for_absence(self):
        fake = FakeNix(valid=False)
        with patch("mango.authoring.pin_source._scratch_directory_exists",
                   side_effect=lambda path: path.startswith(STORE + FAKE + "-")):
            value, err = self.probe(fake)
        self.assertEqual(value, HASH)
        self.assertEqual(err, "")
        self.assertEqual(fake.deleted(), [f"{STORE}{FAKE}-{fake.name}"])

    def test_unrelated_output_path_is_never_deleted(self):
        fake = FakeNix()
        def run(argv, **kwargs):
            result = fake(argv, **kwargs)
            if argv[:3] == ["nix-store", "--query", "--outputs"]:
                result.stdout = STORE + FAKE + "-source\n"
            return result
        err = io.StringIO()
        with patch("mango.authoring.pin_source.subprocess.run", run), contextlib.redirect_stderr(err):
            self.assertEqual(pin_source.content_hash(URL, BRANCH), HASH)
        self.assertEqual(fake.deleted(), [f"{STORE}{REAL}-{fake.name}"])
        self.assertIn("refusing to delete unexpected path", err.getvalue())

    def test_concurrent_identical_requests_use_distinct_names(self):
        a, b = FakeNix(), FakeNix()
        self.probe(a)
        self.probe(b)
        self.assertNotEqual(a.name, b.name)
        self.assertFalse(set(a.deleted()) & set(b.deleted()))
        self.assertTrue(all(a.name in p for p in a.deleted()))


if __name__ == "__main__":
    unittest.main()
