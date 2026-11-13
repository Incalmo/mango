import hashlib
import unittest
from unittest.mock import patch

from mango.authoring.rustup_lock import component_lock, pin_selection, select_toolchain


def manifest():
    text = 'manifest-version = "2"\ndate = "2026-11-12"\n[profiles]\nminimal = ["rustc", "rust-std", "cargo"]\n[pkg.rust]\nversion = "1.100.0"\n'
    for name in ("rustc", "rust-std", "cargo"):
        text += (
            f'[pkg.{name}]\nversion = "1.100.0"\n[pkg.{name}.target.x86_64-unknown-linux-gnu]\navailable = true\nxz_url = "https://static.rust-lang.org/dist/2026-11-12/{name}.tar.xz"\nxz_hash = "'
            + "a" * 64
            + '"\n'
        )
    text += (
        "[pkg.rust.target.x86_64-unknown-linux-gnu]\navailable = true\ncomponents = ["
        + ",".join(
            '{pkg="' + name + '",target="x86_64-unknown-linux-gnu"}'
            for name in ("rustc", "rust-std", "cargo")
        )
        + "]\n"
    )
    return text.encode()


class RustupLockTests(unittest.TestCase):
    def lock(self, data=None, **kwargs):
        data = manifest() if data is None else data
        return component_lock(
            data,
            kwargs.pop("sha256", hashlib.sha256(data).hexdigest()),
            channel="stable",
            target="x86_64-unknown-linux-gnu",
            profile="minimal",
            manifest_url="https://static.rust-lang.org/dist/2026-11-12/channel-rust-stable.toml",
            **kwargs,
        )

    def test_exact_profile_and_selector_are_preserved(self):
        lock = self.lock()
        self.assertEqual(lock["toolchain"], "stable-x86_64-unknown-linux-gnu")
        self.assertEqual(
            [x["name"] for x in lock["components"]], ["rustc", "rust-std", "cargo"]
        )
        self.assertEqual(lock["rust_version"], "1.100.0")
        self.assertEqual(
            lock["manifest"]["sha256"], hashlib.sha256(manifest()).hexdigest()
        )

    def test_manifest_checksum_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.lock(sha256="b" * 64)

    def test_unavailable_component_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.lock(manifest().replace(b"available = true", b"available = false", 1))

    def test_authenticated_component_url_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "public HTTPS"):
            self.lock(
                manifest().replace(
                    b"https://static.", b"https://user:secret@static.", 1
                )
            )

    def test_missing_compiler_profile_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "lacks"):
            self.lock(
                manifest().replace(b'["rustc", "rust-std", "cargo"]', b'["cargo"]')
            )

    def test_platform_inapplicable_profile_component_is_not_installed(self):
        data = manifest().replace(
            b'["rustc", "rust-std", "cargo"]',
            b'["rustc", "rust-std", "cargo", "rust-mingw"]',
        )
        self.assertEqual(
            [x["name"] for x in self.lock(data)["components"]],
            ["rustc", "rust-std", "cargo"],
        )

    def test_explicit_unsupported_component_fails(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.lock(extra_components=["rustfmt"])

    def test_additional_target_gets_its_own_standard_library(self):
        data = manifest().replace(
            b"components = [",
            b'extensions = [{pkg="rust-std",target="wasm32-unknown-unknown"}]\ncomponents = [',
        )
        data += (
            b'[pkg.rust-std.target.wasm32-unknown-unknown]\navailable=true\nxz_url="https://static.rust-lang.org/dist/2026-11-12/std-wasm.tar.xz"\nxz_hash="'
            + b"b" * 64
            + b'"\n'
        )
        lock = self.lock(data, extra_targets=["wasm32-unknown-unknown"])
        self.assertEqual(lock["components"][-1]["target"], "wasm32-unknown-unknown")
        self.assertEqual(lock["components"][-1]["sha256"], "b" * 64)

    def test_explicit_date_must_match_manifest(self):
        data = manifest()
        with self.assertRaisesRegex(ValueError, "date differs"):
            component_lock(
                data,
                hashlib.sha256(data).hexdigest(),
                channel="stable-2026-11-11",
                target="x86_64-unknown-linux-gnu",
                profile="minimal",
                manifest_url="https://static.rust-lang.org/a",
            )


class SelectionTests(unittest.TestCase):
    settings = (
        b'default_toolchain="1.96.0-x86_64-unknown-linux-gnu"\nprofile="minimal"\n'
    )

    def select(self, selector=b'[toolchain]\nchannel="stable"\n', **kwargs):
        return select_toolchain(
            selector,
            kwargs.pop("settings", self.settings),
            base="example/rust@sha256:" + "a" * 64,
            target="x86_64-unknown-linux-gnu",
            installed_toolchains=kwargs.pop(
                "installed", ["1.96.0-x86_64-unknown-linux-gnu"]
            ),
            **kwargs,
        )

    def test_source_channel_does_not_reuse_unrelated_installed_version(self):
        result = self.select()
        self.assertEqual(result["channel"], "stable")
        self.assertFalse(result["provided_by_base"])
        self.assertEqual(result["profile"], "minimal")

    def test_explicit_command_precedes_environment_and_source(self):
        result = self.select(
            command_toolchain="+1.96.0", environment_toolchain="nightly"
        )
        self.assertEqual(result["origin"], "command")
        self.assertTrue(result["provided_by_base"])
        self.assertEqual(
            result["selector_sha256"],
            hashlib.sha256(b'[toolchain]\nchannel="stable"\n').hexdigest(),
        )

    def test_installed_moving_channel_is_not_re_resolved(self):
        selection = self.select(installed=["stable-x86_64-unknown-linux-gnu"])
        with patch(
            "mango.authoring.rustup_lock.pin", side_effect=AssertionError("network")
        ):
            self.assertEqual(pin_selection(selection)["kind"], "base-toolchain")

    def test_default_and_environment_selection(self):
        self.assertEqual(self.select(None)["origin"], "base-default")
        self.assertEqual(self.select(environment_toolchain="beta")["channel"], "beta")

    def test_effective_project_profile_and_extensions(self):
        result = self.select(
            b'[toolchain]\nchannel="nightly-2026-11-12"\nprofile="default"\ncomponents=["rustfmt"]\ntargets=["wasm32-unknown-unknown"]\n'
        )
        self.assertEqual(result["profile"], "default")
        self.assertEqual(result["components"], ["rustfmt"])
        self.assertEqual(result["targets"], ["wasm32-unknown-unknown"])

    def test_ambiguous_directory_override_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Directory overrides"):
            self.select(settings=self.settings + b'[overrides]\n"/project"="beta"\n')

    def test_local_path_toolchain_is_not_silently_substituted(self):
        with self.assertRaisesRegex(ValueError, "selector fields"):
            self.select(b'[toolchain]\npath="/custom"\n')

    def test_installed_toolchain_extensions_need_inventory_review(self):
        with self.assertRaisesRegex(ValueError, "component inventory"):
            self.select(
                b'[toolchain]\nchannel="stable"\ncomponents=["rustfmt"]\n',
                installed=["stable-x86_64-unknown-linux-gnu"],
            )
