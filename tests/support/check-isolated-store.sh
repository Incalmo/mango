#!/usr/bin/env bash
# Stronger than --offline: no daemon, fresh store, outer network namespace.
# Run inside nix-shell; this checks builds, not a clean-machine Harbor install.
set -euo pipefail
bundle=$(realpath "${1:?offline bundle}")
store_root=${2:?new empty store directory}
[[ ! -e "$store_root" ]] || { echo 'Store directory must not exist' >&2; exit 1; }
mkdir -p "$store_root"
store_root=$(realpath "$store_root")
if [[ -n "${3:-}" ]]; then
  # A standard binary cache replaces the archive transport. No build/evaluation
  # occurs during restore, so a missing object cannot trigger an upstream fetch.
  python - "$bundle"/build/*/inputs.json "$store_root" "$3" "${4:?trusted public key}" <<'RESTORE'
import json, subprocess, sys
from pathlib import Path
paths = json.load(open(sys.argv[1]))['paths']
if not paths or any(not p.startswith('/nix/store/') or '/..' in p for p in paths):
    raise ValueError('Invalid retained store paths')
for offset in range(0, len(paths), 100):
    subprocess.run(['nix', 'copy', '--from', sys.argv[3], '--to', 'local?root=' + sys.argv[2],
                    '--option', 'require-sigs', 'true', '--option', 'trusted-public-keys', sys.argv[4],
                    '--option', 'substituters', '', *paths[offset:offset + 100]], check=True)
RESTORE
else
  nix-store --store "local?root=$store_root" --import < "$bundle/inputs.nar" > "$store_root/imported-paths.txt"
fi
python - "$bundle"/build/*/inputs.json "$store_root" <<'PY'
import json, subprocess, sys
from pathlib import Path
root = Path(sys.argv[2])
store = "local?root=" + str(root)
drvs = json.load(open(sys.argv[1]))['derivations']
(root / "derivations.txt").write_text('\n'.join(drvs) + '\n')
outputs = subprocess.check_output(
    ["nix-store", "--store", store, "--query", "--outputs", *drvs], text=True
).splitlines()
expected = json.loads(subprocess.check_output(
    ["nix", "path-info", "--store", store, "--json", *outputs], text=True
))
if isinstance(expected, list):
    expected = {item["path"]: item for item in expected}
(root / "expected-outputs.json").write_text(json.dumps(expected, indent=2) + '\n')
# Remove final application/image outputs AND their generated reverse dependents.
# Retained source/dependency inputs remain. This is an isolated, newly created
# store: never run this deletion against the host daemon/store.
removed = subprocess.check_output(
    ["nix-store", "--store", store, "--query", "--referrers-closure", *outputs], text=True
).splitlines()
if any(path.endswith(".drv") for path in removed):
    raise RuntimeError("Refusing to remove a retained derivation")
subprocess.run(
    ["nix-store", "--store", store, "--delete", "--ignore-liveness", *removed], check=True
)
if any((root / path.lstrip('/')).exists() for path in outputs):
    raise RuntimeError("Final outputs were not removed")
(root / "removed-outputs.json").write_text(json.dumps(removed, indent=2) + '\n')
PY
# User namespaces already deny setgroups. This switch avoids that redundant
# operation; sandboxing and the outer network namespace both remain enabled.
unshare -Urnm bash -c '
  set -euo pipefail
  while IFS= read -r drv; do
    nix-store --store "local?root=$1" --realise "$drv" --keep-failed \
      --option build-users-group "" --option require-drop-supplementary-groups false \
      --option sandbox true --option sandbox-fallback false \
      --option substitute false --option builders "" --option max-jobs 1
  done < "$1/derivations.txt"
' bash "$store_root"
python - "$store_root" <<'PY'
import json, subprocess, sys
from pathlib import Path
root = Path(sys.argv[1])
expected = json.loads((root / "expected-outputs.json").read_text())
actual = json.loads(subprocess.check_output(
    ["nix", "path-info", "--store", "local?root=" + str(root), "--json", *expected], text=True
))
if isinstance(actual, list):
    actual = {item["path"]: item for item in actual}
records = [
    {"path": path, "expected_nar_hash": old["narHash"],
     "actual_nar_hash": actual[path]["narHash"],
     "equal": old["narHash"] == actual[path]["narHash"]}
    for path, old in expected.items()
]
(root / "fresh-build-verification.json").write_text(json.dumps(records, indent=2) + '\n')
matches = sum(item["equal"] for item in records)
print("Offline rebuild digest matches:", matches, "/", len(records))
if matches != len(records):
    raise SystemExit("Offline rebuild outputs differ; see fresh-build-verification.json")
print("Offline rebuild passed with identical output hashes.")
PY
