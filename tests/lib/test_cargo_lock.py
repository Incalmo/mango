"""Explicit Cargo.lock consumption, with a real offline compilation."""

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class CargoLockTests(unittest.TestCase):
    def build(self, expression):
        prefix = f"let h = import {ROOT} {{}}; pkgs = h.pkgs; a = h.dependencies; in "
        result = subprocess.run(
            ["nix-build", "--no-out-link", "--expr", prefix + expression],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-15000:])
        return Path(result.stdout.strip().splitlines()[-1])

    def test_cargo_lock_builds_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "Cargo.toml").write_text(
                '[package]\nname="offline-fixture"\nversion="0.1.0"\nedition="2021"\n[dependencies]\nitoa="=1.0.18"\n'
            )
            (root / "Cargo.lock").write_text("""version = 4
[[package]]
name = "offline-fixture"
version = "0.1.0"
dependencies = ["itoa"]
[[package]]
name = "itoa"
version = "1.0.18"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "8f42a60cbdf9a97f5d2305f08a87dc4e09308d1276d28c869c684d7777685682"
""")
            (root / "src/main.rs").write_text(
                'fn main() { println!("{}", itoa::Buffer::new().format(42)); }'
            )
            output = self.build(f"""a.mkOfflineApplication {{ name = "offline-cargo-adapter"; src = {root};
              nativeBuildInputs = [pkgs.cargo pkgs.rustc a.cargoSetupHook];
              cargoDeps = a.fetchCargoCache {{ lockFile = {root}/Cargo.lock; }};
              buildPhase = ''
                runHook preBuild
                export HOME=$TMPDIR/home CARGO_NET_OFFLINE=true
                cargo build --frozen --offline
                test "$(target/debug/offline-fixture)" = 42
              '';
              installPhase = "cp target/debug/offline-fixture $out";
            }}""")
            self.assertTrue(output.is_file())
