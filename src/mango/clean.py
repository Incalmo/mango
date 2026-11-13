"""Remove what building one environment left behind.

Only state owned by the selected environment and workspace is touched: its
generated workspace, the Docker images it loaded, and the Nix store paths that
nothing else keeps alive once its garbage-collector roots are gone. Store
paths and images still used by another environment are kept.
"""

import json
import shutil
import subprocess

from mango import builder
from mango.environment.workspace import workspace_lock

RESULTS = "jobs"

# Shared by the host store and the managed builder's volume, so it only uses
# tools present in both: bash, coreutils, findutils, grep and nix-store.
# Arguments: WORKSPACE [ENTRY-TO-KEEP]. Prints the number of deleted paths.
SCRIPT = r"""
set -euo pipefail
work=$1
keep=${2:-}
[ -d "$work" ] || { echo 0; exit 0; }
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT

# Everything this workspace's garbage-collector roots keep alive.
find "$work" -mindepth 1 -name "$keep" -prune -o -type l -print0 \
  | xargs -0 -r readlink \
  | { grep -o '^/nix/store/[^/]*' || true; } | sort -u \
  | while read -r path; do [ ! -e "$path" ] || printf '%s\n' "$path"; done > "$scratch/roots"
: > "$scratch/closure"
if [ -s "$scratch/roots" ]; then
  xargs -r nix-store --query --requisites < "$scratch/roots" | sort -u > "$scratch/closure"
fi

# Copies of store files are read-only; make them removable first.
find "$work" -mindepth 1 -maxdepth 1 ! -name "$keep" -exec chmod -R u+w {} + 2>/dev/null || true
find "$work" -mindepth 1 -maxdepth 1 ! -name "$keep" -exec rm -rf {} +
rmdir "$work" 2>/dev/null || true

# Delete only paths that no other root still needs. Start from the paths the
# remaining roots (and their derivations) do not reach. Nix has the final say:
# it refuses a path that is still alive, for example one held by a running
# process, so drop that path and what it requires, then retry.
valid() { while read -r path; do [ ! -e "$path" ] || printf '%s\n' "$path"; done; }
nix-store --gc --print-roots 2>/dev/null \
  | { grep -o '/nix/store/[^/ ]*$' || true; } | sort -u | valid > "$scratch/live-roots"
xargs -r nix-store --query --requisites < "$scratch/live-roots" | sort -u > "$scratch/live"
xargs -r nix-store --query --deriver < "$scratch/live" 2>/dev/null \
  | { grep '^/nix/store/' || true; } | sort -u | valid > "$scratch/derivers"
xargs -r nix-store --query --requisites < "$scratch/derivers" \
  | sort -u - "$scratch/live" > "$scratch/alive"
comm -23 "$scratch/closure" "$scratch/alive" > "$scratch/delete"
tries=0
while [ -s "$scratch/delete" ]; do
  if xargs -r nix-store --delete < "$scratch/delete" >/dev/null 2> "$scratch/log"; then
    break
  fi
  tries=$((tries + 1))
  { grep -io "cannot delete path '[^']*'" "$scratch/log" || true; } | cut -d"'" -f2 | valid > "$scratch/refused"
  if [ ! -s "$scratch/refused" ] || [ "$tries" -ge 100 ]; then
    echo "mango: some store paths could not be deleted" >&2
    break
  fi
  xargs -r nix-store --query --requisites < "$scratch/refused" | sort -u > "$scratch/skip"
  valid < "$scratch/delete" | comm -23 - "$scratch/skip" > "$scratch/next"
  mv "$scratch/next" "$scratch/delete"
done
count=0
while read -r path; do [ -e "$path" ] || count=$((count + 1)); done < "$scratch/closure"
echo "$count"
"""


def image_tags(workspace):
    """Docker tags of the images recorded in a workspace's ``images.json``."""
    try:
        manifest = json.loads((workspace / "images.json").read_text())
    except (OSError, ValueError):
        return set()
    if not isinstance(manifest, dict):
        return set()
    references = [manifest.get("egress_image")]
    services = manifest.get("services")
    references += list(services.values()) if isinstance(services, dict) else []
    verifiers = manifest.get("verifiers")
    for mapping in verifiers.values() if isinstance(verifiers, dict) else []:
        references += list(mapping.values()) if isinstance(mapping, dict) else []
    tags = set()
    for reference in references:
        name, separator, digest = str(reference).partition("@sha256:")
        # Only images this tool loaded: mango.local/<name>:<digest prefix>.
        if separator and name.startswith("mango.local/") and len(digest) == 64:
            tags.add(f"{name}:{digest[:16]}")
    return tags


def remove_images(workspace):
    """Remove this workspace's images unless a sibling workspace uses them."""
    tags = image_tags(workspace)
    shared = set()
    if workspace.parent.is_dir():
        for sibling in workspace.parent.iterdir():
            if sibling != workspace:
                shared |= image_tags(sibling)
    removed, kept = [], sorted(tags & shared)
    for tag in sorted(tags - shared):
        result = subprocess.run(
            ["docker", "image", "rm", tag],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode == 0:
            removed.append(tag)
        elif "No such image" not in result.stderr:
            # Typically a container from a running trial still uses it.
            kept.append(tag)
    return removed, sorted(kept)


def run_script(prefix, work, keep):
    result = subprocess.run(
        [*prefix, "bash", "-c", SCRIPT, "mango-clean", str(work), keep],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return int(result.stdout.strip().splitlines()[-1])


def clean_managed(root, workspace):
    """Clean this environment's bookkeeping in the managed builder's volume."""
    try:
        pin = json.loads((root / "mango.lock").read_text())["builder"]
        volume = builder.volume_name(pin)
        image, platform = pin["image"], pin["platform"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    inspect = subprocess.run(
        ["docker", "volume", "inspect", volume],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if inspect.returncode != 0:
        return None
    prefix = [
        "docker", "run", "--rm", "--platform", platform,
        "--mount", f"type=volume,source={volume},target=/nix",
        "--env", "NIX_REMOTE=local", "--env", "NIX_CONFIG=build-users-group =",
        image,
    ]
    # Trial results never live in the volume, so nothing is kept there.
    return run_script(prefix, builder.volume_workspace(root, workspace), "")


def clean(root, workspace, *, results=False):
    """Clean one environment's workspace, images and unshared store paths."""
    summary = {
        "workspace": str(workspace),
        "images_removed": [],
        "images_kept": [],
        "store_paths_deleted": 0,
        "managed_store_paths_deleted": 0,
    }
    docker = shutil.which("docker") is not None
    if workspace.exists():
        # A workspace carries its lock file; an earlier clean leaves only results.
        if not workspace.is_dir() or not (
            (workspace / ".harness.lock").is_file()
            or all(entry.name == RESULTS for entry in workspace.iterdir())
        ):
            raise ValueError(f"{workspace} is not a Mango workspace; refusing to delete it")
        with workspace_lock(workspace):
            if docker:
                summary["images_removed"], summary["images_kept"] = remove_images(workspace)
            keep = "" if results else RESULTS
            if shutil.which("nix-store"):
                summary["store_paths_deleted"] = run_script([], workspace, keep)
            else:
                for entry in workspace.iterdir():
                    if entry.name == keep:
                        continue
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
        if workspace.is_dir() and not any(workspace.iterdir()):
            workspace.rmdir()
        parent = workspace.parent
        if parent.name == "mango-build" and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    if docker:
        summary["managed_store_paths_deleted"] = clean_managed(root, workspace) or 0
    if (workspace / RESULTS).exists():
        summary["results_kept"] = str(workspace / RESULTS)
    return summary
