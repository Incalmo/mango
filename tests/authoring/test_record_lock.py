"""``mango-lock-record`` turns a recording into lock entries without knowing any tool."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from mango.authoring import record_lock


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


JAR, INDEX_PAGE, PACK, BIG = b"jar", b"mutable index", b"git pack", b"x" * 64
BODY = "0" * 64


def recording_index():
    return {
        "version": 1,
        "entries": {
            "GET https://repo.example/a-1.0.jar": {"status": 200, "sha256": sha(JAR), "size": 3, "stable": True},
            "GET https://mirror.example/a-1.0.jar": {"status": 200, "sha256": sha(JAR), "size": 3, "stable": True},
            "GET https://repo.example/index": {"status": 200, "sha256": sha(INDEX_PAGE), "size": 13},
            "GET https://repo.example/huge-index": {"status": 200, "sha256": sha(BIG), "size": 64},
            f"POST https://git.example/repo/git-upload-pack {BODY}": {"status": 200, "sha256": sha(PACK), "size": 8, "stable": True},
            "GET https://repo.example/missing": {"status": 404, "size": 0},
            "GET https://repo.example/probe": {"status": 200, "size": 9, "head_only": True},
        },
    }


class RecordLockTests(unittest.TestCase):
    def test_only_plain_credential_free_gets_are_addressable(self):
        self.assertEqual(record_lock.addressable("GET https://h/p?q=1"), "https://h/p?q=1")
        self.assertEqual(record_lock.addressable("GET http://h"), "http://h")
        for key in (
            "POST https://h/p",
            f"GET https://h/p {BODY}",
            "GET https://user@h/p",
            "GET https://h/p?X-Amz-Signature=abc",
            "GET ftp://h/p",
        ):
            self.assertIsNone(record_lock.addressable(key), key)

    def test_entries_follow_stability_and_size(self):
        artifacts, commit, report = record_lock.lock_entries(recording_index(), "record/app", "inputs.lock.d", commit_limit=32)
        by_path = {a["path"]: a for a in artifacts}
        self.assertEqual(
            by_path[f"record/app/blobs/{sha(JAR)}"],
            {
                "path": f"record/app/blobs/{sha(JAR)}",
                "urls": ["https://mirror.example/a-1.0.jar", "https://repo.example/a-1.0.jar"],
                "sha256": sha(JAR),
            },
        )
        # Short-lived and not addressable by URL: both are committed.
        self.assertEqual(commit, {sha(INDEX_PAGE), sha(PACK)})
        self.assertEqual(by_path[f"record/app/blobs/{sha(PACK)}"]["file"], f"inputs.lock.d/{sha(PACK)}")
        # Too large to commit: pinned by URL anyway, and reported.
        self.assertEqual(by_path[f"record/app/blobs/{sha(BIG)}"]["urls"], ["https://repo.example/huge-index"])
        self.assertEqual(report["unstable_but_too_large_to_commit"], ["GET https://repo.example/huge-index"])
        self.assertEqual(len(artifacts), 4)  # responses without a body add nothing
        self.assertEqual((report["exchanges"], report["pinned_by_url"], report["committed"]), (7, 2, 2))

    def test_bytes_with_a_stable_url_are_not_committed_twice(self):
        index = recording_index()
        index["entries"]["GET https://repo.example/alias"] = {"status": 200, "sha256": sha(JAR), "size": 3}
        _, commit, _ = record_lock.lock_entries(index, "p", "d")
        self.assertNotIn(sha(JAR), commit)

    def test_malformed_recordings_are_rejected(self):
        with self.assertRaises(record_lock.RecordError):
            record_lock.lock_entries({"version": 2, "entries": {}}, "p", "d")
        with self.assertRaises(record_lock.RecordError):
            record_lock.lock_entries({"version": 1, "entries": {"GET https://h/": {"sha256": "../x"}}}, "p", "d")

    def test_write_lock_commits_files_and_replaces_its_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recording = root / "recording"
            (recording / "blobs").mkdir(parents=True)
            for data in (JAR, INDEX_PAGE, PACK, BIG):
                (recording / "blobs" / sha(data)).write_bytes(data)
            (recording / "index.json").write_text(json.dumps(recording_index()))
            lock = root / "inputs.lock.json"
            lock.write_text(json.dumps({"schema_version": 1, "artifacts": [
                {"path": "apt/x.deb", "urls": ["https://d/x.deb"], "sha256": "1" * 64},
                {"path": "record/app/blobs/old", "file": "inputs.lock.d/" + "2" * 64, "sha256": "2" * 64},
            ]}))
            (root / "inputs.lock.d").mkdir()
            (root / "inputs.lock.d" / ("2" * 64)).write_text("stale")
            record_lock.write_lock(lock, "record/app", recording)
            document = json.loads(lock.read_text())
            paths = [a["path"] for a in document["artifacts"]]
            self.assertIn("apt/x.deb", paths)
            self.assertNotIn("record/app/blobs/old", paths)
            self.assertIn("record/app/index.json", paths)
            committed = {p.name for p in (root / "inputs.lock.d").iterdir()}
            index_hash = sha((recording / "index.json").read_bytes())
            self.assertEqual(committed, {sha(INDEX_PAGE), sha(PACK), sha(BIG), index_hash})
            for artifact in document["artifacts"]:
                if "file" in artifact:
                    self.assertEqual(sha((root / artifact["file"]).read_bytes()), artifact["sha256"])

    def test_tampered_recorded_body_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            recording = Path(directory) / "recording"
            (recording / "blobs").mkdir(parents=True)
            (recording / "blobs" / sha(INDEX_PAGE)).write_bytes(b"different")
            (recording / "index.json").write_text(json.dumps({"version": 1, "entries": {
                "GET https://h/index": {"status": 200, "sha256": sha(INDEX_PAGE), "size": 13}}}))
            with self.assertRaises(record_lock.RecordError):
                record_lock.write_lock(Path(directory) / "inputs.lock.json", "p", recording)


if __name__ == "__main__":
    unittest.main()
