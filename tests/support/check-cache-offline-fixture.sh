#!/usr/bin/env bash
# Empty store -> signed cache restore -> remove final output -> offline rebuild.
# Run inside nix-shell. No cloud resources and no host-store deletion.
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
trap 'chmod -R u+w "$work"; rm -rf "$work"' EXIT
umask 077
mkdir -p "$work/bundle/mango-build/fixture"
cat > "$work/fixture.nix" <<NIX
let pkgs = import (import $repo/lib/pins/nixpkgs.nix) { system = "x86_64-linux"; };
in builtins.derivation {
  name = "offline-cache-fixture";
  system = "x86_64-linux";
  builder = "\${pkgs.bash}/bin/bash";
  args = [ "-euc" "printf 'offline cache build\\n' > \$out" ];
}
NIX
drv=$(nix-instantiate "$work/fixture.nix")
nix-store --realise "$drv" > /dev/null
PYTHONPATH="$repo/src" python -m mango.outputs.offline freeze "$work/bundle/mango-build/fixture/inputs.json" "$drv"
nix-store --generate-binary-cache-key fixture "$work/key" "$work/key.pub"
python - "$work" <<'PY'
import json, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
paths = json.loads((root / 'bundle/mango-build/fixture/inputs.json').read_text())['paths']
cache = (root / 'cache').as_uri() + '?secret-key=' + str(root / 'key')
for offset in range(0, len(paths), 100):
    subprocess.run(['nix', 'copy', '--to', cache, *paths[offset:offset+100]], check=True)
PY
bash "$repo/tests/support/check-isolated-store.sh" "$work/bundle" "$work/isolated" \
  "file://$work/cache" "$(cat "$work/key.pub")"
