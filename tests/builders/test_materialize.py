"""Transport references expand once; application and context symlinks survive."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "mango_materialize", Path(__file__).resolve().parents[2] / "builders/materialize.py"
)
materialize = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(materialize)


class MaterializeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = self.root / "store"
        self.store.mkdir()
        self.farm = self.root / "farm"
        self.farm.mkdir()

    def test_nested_farm_paths_expand_file_and_directory_payloads_once(self):
        file = self.store / "hash-wheel"
        file.write_text("wheel bytes")
        source = self.store / "hash-source"
        source.mkdir()
        (source / "AGENTS.md").write_text("documentation")
        (source / "CLAUDE.md").symlink_to("AGENTS.md")
        (source / "runtime").symlink_to("/usr/bin/not-a-build-input")
        (source / "nested-store-link").symlink_to(file)
        (self.farm / "cache/download/module/@v").mkdir(parents=True)
        (self.farm / "cache/download/module/@v/item.zip").symlink_to(file)
        (self.farm / "source").symlink_to(source)
        output = self.root / "output"
        materialize.inputs(self.farm, output, store=self.store)
        self.assertEqual(
            (output / "cache/download/module/@v/item.zip").read_text(), "wheel bytes"
        )
        self.assertFalse((output / "cache/download/module/@v/item.zip").is_symlink())
        self.assertEqual((output / "source/CLAUDE.md").readlink(), Path("AGENTS.md"))
        self.assertEqual(
            (output / "source/runtime").readlink(), Path("/usr/bin/not-a-build-input")
        )
        self.assertEqual((output / "source/nested-store-link").readlink(), file)

    def test_context_preserves_all_symlinks_including_store_links(self):
        file = self.store / "hash-file"
        file.write_text("bytes")
        (self.farm / "store-link").symlink_to(file)
        (self.farm / "dangling").symlink_to("missing")
        output = self.root / "output"
        materialize.payload(self.farm, output)
        self.assertEqual((output / "store-link").readlink(), file)
        self.assertEqual((output / "dangling").readlink(), Path("missing"))

    def test_store_output_aliases_resolve_but_runtime_output_links_do_not(self):
        file = self.store / "hash-file"
        file.write_text("bytes")
        alias = self.store / "hash-alias"
        alias.symlink_to(file)
        runtime = self.store / "hash-runtime"
        runtime.symlink_to("/bin/sh")
        (self.farm / "file").symlink_to(alias)
        (self.farm / "runtime").symlink_to(runtime)
        output = self.root / "output"
        materialize.inputs(self.farm, output, store=self.store)
        self.assertEqual((output / "file").read_text(), "bytes")
        self.assertEqual((output / "runtime").readlink(), Path("/bin/sh"))

    def test_non_store_farm_link_is_not_followed(self):
        (self.farm / "runtime").symlink_to("/bin/sh")
        output = self.root / "output"
        materialize.inputs(self.farm, output, store=self.store)
        self.assertEqual((output / "runtime").readlink(), Path("/bin/sh"))

    def test_intermediate_store_path_cannot_escape_to_runtime_filesystem(self):
        escape = self.store / "hash-escape"
        escape.symlink_to(self.root)
        (self.farm / "bad").symlink_to(escape / "outside")
        with self.assertRaisesRegex(ValueError, "outside the Nix store"):
            materialize.inputs(self.farm, self.root / "output", store=self.store)
