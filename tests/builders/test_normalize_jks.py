import hashlib
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "builders"))
from normalize_jks import normalize, normalize_file


def seal(body, password="changeit"):
    return (
        body
        + hashlib.sha1(
            password.encode("utf-16-be") + b"Mighty Aphrodite" + body
        ).digest()
    )


def utf(raw):
    return struct.pack(">H", len(raw)) + raw


def store(timestamp=987654321, version=2, alias=b"alias", tag=2, cert=b"opaque cert"):
    return seal(
        struct.pack(">Iiii", 0xFEEDFEED, version, 1, tag)
        + utf(alias)
        + struct.pack(">q", timestamp)
        + (utf(b"X.509") if version == 2 else b"")
        + struct.pack(">i", len(cert))
        + cert
    )


class ParserTests(unittest.TestCase):
    def test_preservation_versions_and_idempotence(self):
        for version in (1, 2):
            # Modified UTF-8 NUL and surrogate pair, with exact bytes retained.
            original = store(
                version=version, alias=b"a\xc0\x80\xed\xa0\xbd\xed\xb8\x80"
            )
            normalized, count = normalize(original, 42)
            self.assertEqual(count, 1)
            self.assertEqual(
                normalized, store(42000, version, b"a\xc0\x80\xed\xa0\xbd\xed\xb8\x80")
            )
            self.assertEqual(normalize(normalized, 42)[0], normalized)

    def test_rejects_malformed_without_mutation(self):
        body = store()[:-20]
        invalid = [
            store(tag=1),
            store(tag=9),
            store(version=3),
            store(cert=b""),
            store(alias=b"\x00"),
            store(alias=b"\xc1\x81"),
            store(alias=b"\xff"),
            seal(body + b"extra"),
            seal(body[:-1]),
            store()[:-1],
            seal(b"PKCS" + body[4:]),
            seal(body[:8] + struct.pack(">i", -1) + body[12:]),
            store()[:-1] + bytes([store()[-1] ^ 1]),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trust"
            for value in invalid:
                path.write_bytes(value)
                with self.assertRaises(ValueError):
                    normalize_file(path, 0)
                self.assertEqual(path.read_bytes(), value)
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["trust"])
        with self.assertRaises(ValueError):
            normalize(store(), 0, "incorrect")
        for epoch in (-1, 2**63, True, 1.5):
            with self.assertRaises(ValueError):
                normalize(store(), epoch)

    def test_duplicate_alias_and_all_truncations(self):
        body = store()[:-20]
        duplicate = body[:8] + struct.pack(">i", 2) + body[12:] + body[12:]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            normalize(seal(duplicate), 0)
        for length in range(len(body)):
            with self.assertRaises(ValueError):
                normalize(seal(body[:length]), 0)
        custom = seal(body, "páss😀")
        output, count = normalize(custom, 0, "páss😀")
        self.assertEqual(count, 1)
        self.assertEqual(output, seal(store(0)[:-20], "páss😀"))

    def test_permissions_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trust"
            path.write_bytes(store())
            path.chmod(0o640)
            normalize_file(path, 0)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            link = path.with_name("link")
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                normalize_file(link, 0)


@unittest.skipUnless(shutil.which("keytool"), "run in tests/support/jks-shell.nix")
class KeytoolTests(unittest.TestCase):
    def test_real_java_stores(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def keytool(*args):
                return subprocess.check_output(
                    ["keytool", *map(str, args), "-storepass", "changeit"],
                    stderr=subprocess.STDOUT,
                )

            private = root / "private.jks"
            cert = root / "cert.der"
            keytool(
                "-genkeypair",
                "-alias",
                "temporary",
                "-keyalg",
                "RSA",
                "-keysize",
                "2048",
                "-dname",
                "CN=Normalizer integration test",
                "-validity",
                "1",
                "-keypass",
                "changeit",
                "-storetype",
                "JKS",
                "-keystore",
                private,
            )
            keytool(
                "-exportcert",
                "-alias",
                "temporary",
                "-keystore",
                private,
                "-file",
                cert,
            )
            with self.assertRaisesRegex(ValueError, "private-key"):
                normalize(private.read_bytes(), 0)
            stores = []
            for index in range(2):
                path = root / (str(index) + ".jks")
                for alias in ("first", "café"):
                    keytool(
                        "-importcert",
                        "-noprompt",
                        "-alias",
                        alias,
                        "-file",
                        cert,
                        "-storetype",
                        "JKS",
                        "-keystore",
                        path,
                    )
                # Explicitly establish different valid entry timestamps, independent of clock speed.
                data, count = normalize(path.read_bytes(), 100 + index)
                self.assertEqual(count, 2)
                path.write_bytes(data)
                before = keytool("-list", "-rfc", "-keystore", path)
                normalize_file(path, 0)
                after = keytool("-list", "-rfc", "-keystore", path)
                for alias in ("first", "café"):
                    output = root / (str(index) + alias + ".der")
                    keytool(
                        "-exportcert",
                        "-alias",
                        alias,
                        "-keystore",
                        path,
                        "-file",
                        output,
                    )
                    self.assertEqual(output.read_bytes(), cert.read_bytes())
                # RFC certificate blocks and aliases survive Java's own reader.
                for output in (before, after):
                    self.assertEqual(output.count(b"BEGIN CERTIFICATE"), 2)
                    self.assertIn(b"Alias name: first", output)
                    self.assertIn("Alias name: café".encode(), output)
                stores.append(path.read_bytes())
            self.assertEqual(stores[0], stores[1])


if __name__ == "__main__":
    unittest.main()
