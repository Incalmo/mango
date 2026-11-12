"""Filesystem-only cache capture tests with a fake remote container."""

import io
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mango.authoring import npm_lock as deps
from mango.authoring.npm_cache import capture


class Container:
    def __init__(self, member="_cacache/index-v5/entry"):
        self.member = member
        self.commands = []
        self.disconnected = False

    def exec(self, argv, **kwargs):
        self.commands.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, "/root/.npm\n" if argv[:3] == ["npm", "config", "get"] else "", ""
        )

    def disconnect_networks(self):
        self.disconnected = True
        return []

    def copy_out(self, source, destination):
        with tarfile.open(destination, "w:gz") as archive:
            item = tarfile.TarInfo(self.member)
            item.size = 2
            item.mode = 0o600
            archive.addfile(item, io.BytesIO(b"{}"))


class CacheCaptureTests(unittest.TestCase):
    def test_original_metadata_cache_and_lifecycle_replay(self):
        request = {
            "command": "install",
            "_flags": {"argv": []},
            "timeout_seconds": 10,
            "limits": {"max_hidden_lock_bytes": 1000, "max_lock_bytes": 1000},
        }
        container = Container()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                deps, "read_hidden_lock", return_value={"packages": {"vite": "1"}}
            ),
            patch.object(deps, "read_container_bytes", return_value=b"{}"),
        ):
            root = Path(directory)
            result = capture(
                container, request, root, root / "output", {"vite": "1"}, b"{}"
            )
            self.assertTrue(Path(result["author_source"]).is_file())
            self.assertTrue(result["lifecycle_replay_network_disabled"])
        self.assertTrue(container.disconnected)
        install = next(
            command
            for command in container.commands
            if command[:2] == ["npm", "install"]
        )
        self.assertIn(deps.CACHE, install)
        self.assertIn("--offline", install)
        self.assertNotIn("--ignore-scripts", install)

    def test_unsafe_cache_members_are_rejected(self):
        request = {
            "command": "install",
            "_flags": {"argv": []},
            "timeout_seconds": 10,
            "limits": {"max_hidden_lock_bytes": 1000, "max_lock_bytes": 1000},
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(deps, "read_hidden_lock", return_value={"packages": {}}),
            self.assertRaisesRegex(deps.ResolverError, "member"),
        ):
            capture(
                Container("../outside"),
                request,
                Path(directory),
                Path(directory) / "output",
                {},
                None,
            )
