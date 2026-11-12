import hashlib
import unittest

from mango.authoring.rustup_lock import component_lock


def manifest():
    text = 'manifest-version = "2"\ndate = "2026-11-12"\n[profiles]\nminimal = ["rustc", "rust-std", "cargo"]\n[pkg.rust]\nversion = "1.100.0"\n'
    for name in ('rustc', 'rust-std', 'cargo'):
        text += f'[pkg.{name}]\nversion = "1.100.0"\n[pkg.{name}.target.x86_64-unknown-linux-gnu]\navailable = true\nxz_url = "https://static.rust-lang.org/dist/2026-11-12/{name}.tar.xz"\nxz_hash = "' + 'a' * 64 + '"\n'
    text += '[pkg.rust.target.x86_64-unknown-linux-gnu]\navailable = true\ncomponents = [' + ','.join('{pkg="' + name + '",target="x86_64-unknown-linux-gnu"}' for name in ('rustc', 'rust-std', 'cargo')) + ']\n'
    return text.encode()


class RustupLockTests(unittest.TestCase):
    def lock(self, data=None, **kwargs):
        data = manifest() if data is None else data
        return component_lock(data, kwargs.pop('sha256', hashlib.sha256(data).hexdigest()),
                              channel='stable', target='x86_64-unknown-linux-gnu',
                              profile='minimal', manifest_url='https://static.rust-lang.org/dist/2026-11-12/channel-rust-stable.toml', **kwargs)

    def test_exact_profile_and_selector_are_preserved(self):
        lock = self.lock()
        self.assertEqual(lock['toolchain'], 'stable-x86_64-unknown-linux-gnu')
        self.assertEqual([x['name'] for x in lock['components']], ['rustc', 'rust-std', 'cargo'])
        self.assertEqual(lock['rust_version'], '1.100.0')
        self.assertEqual(lock['manifest']['sha256'], hashlib.sha256(manifest()).hexdigest())

    def test_manifest_checksum_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.lock(sha256='b' * 64)

    def test_unavailable_component_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            self.lock(manifest().replace(b'available = true', b'available = false', 1))

    def test_authenticated_component_url_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'public HTTPS'):
            self.lock(manifest().replace(b'https://static.', b'https://user:secret@static.', 1))

    def test_missing_compiler_profile_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'lacks'):
            self.lock(manifest().replace(b'["rustc", "rust-std", "cargo"]', b'["cargo"]'))

    def test_platform_inapplicable_profile_component_is_not_installed(self):
        data = manifest().replace(b'["rustc", "rust-std", "cargo"]', b'["rustc", "rust-std", "cargo", "rust-mingw"]')
        self.assertEqual([x['name'] for x in self.lock(data)['components']], ['rustc', 'rust-std', 'cargo'])
