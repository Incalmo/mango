# Author npm dependencies for offline builds

`mango.authoring.npm_lock` resolves public npm registry dependencies with the
exact npm version already installed in a pinned Linux/amd64 base. It records
SHA256-verified tarballs and can preserve the original npm cache, including
registry metadata. Run container and network operations on a remote authoring
host with Docker; Mango image builds consume the resulting inputs offline.

The authoring API supports `npm ci` with an existing version 2/3 lock and
`npm install` with no existing lock. For the latter, npm creates the lock itself;
the receipt includes its exact bytes and hash. The source `package.json` remains
unchanged and must declare its project `name` so npm does not infer it from the
authoring directory. Yarn, pnpm, workspaces, Git/file dependencies, custom registries, and
unreviewed installation flags fail closed.

## Author and verify

Create a request file on the remote host:

```json
{
  "requests": [{
    "id": "frontend",
    "base": "docker.io/library/node@sha256:<exact-manifest-digest>",
    "manager": {"name": "npm", "version": "10.8.2"},
    "command": "install",
    "flags": [],
    "env": {},
    "source_path": "/work/frontend-source",
    "replay": true,
    "capture_cache": true
  }]
}
```

```sh
MANGO_REMOTE_AUTHOR=1 python -m mango.authoring.npm_lock requests.json \
  --output npm-results.json --cache-directory /work/pinned-npm-caches \
  --require-verified
```

The `mango-lock-npm` entry point accepts the same arguments. `--retry-failures`
retries failed checkpoint entries. Request identity and original source hashes
control checkpoint reuse; retained archives must still exist and match their
hash. Alternatively, use the Python API:

```python
from pathlib import Path
from mango.authoring.npm_lock import resolve, require_verified

result = resolve(request, cache_directory=Path("/work/pinned-npm-caches"))
result["status"] = "completed"
require_verified(result)
```

Only `package.json` and the selected lock are copied into the disposable
authoring container. Lifecycle scripts that need other project files require a
separate source-aware workflow. The resolver rejects changed manifests, changed
locks, unsupported package sources, integrity failures, and mismatched installed
package selections.

If dependencies require prior distro packages, supply `prepared_image` with
`id` (an immutable Docker image ID), `base` (the same pinned original base), and
`apt_request_sha256` (the exact prepared package-state request). The resolver
inspects the created container's actual image ID. The prepared image is an
authoring intermediate; it is not an input to the translated environment.
Consumers must bind the receipt to the same package prefix and base.

The public package-state API prepares that prefix from its locked archives and
leases the intermediate image until npm authoring finishes:

```python
from mango.authoring.package_state import prepare_prefix, release_prefix

prepared = prepare_prefix(package_receipt, count=4, retain=True)
try:
    request["prepared_image"] = prepared["prepared_image"]
    request["base"] = prepared["prepared_image"]["base"]
    result = resolve(request, cache_directory=Path("/work/pinned-npm-caches"))
finally:
    release_prefix(prepared)
```

`count` is the number of complete original APT transactions before the npm
command. Preparation verifies the selected archive names and hashes; it does
not reproduce arbitrary earlier shell commands. Bind and review those commands,
source files, environment and working directory independently.

## What the proof covers

The first replay disconnects the container network and reproduces the selected
package graph from individually verified tarballs. With `capture_cache: true`,
a second disconnected install executes the original lifecycle scripts using
the original manager-created cache. Package selection and generated lock bytes
must remain unchanged. Only then does `cache_archive` report
`lifecycle_replay_network_disabled` and `installed_package_selection_equal`.

This does not prove later `npm run build` commands or the resulting application.
Those commands remain in the environment and must pass its offline image build
and runtime evaluation. Receipt `status: completed` alone is insufficient;
use `require_verified` and check the additional cache evidence when consuming a
retained cache.

## Declare the resulting inputs

`lock.artifacts` can be consumed by Mango's existing locked-input fetchers.
`cache_archive` is a regular source artifact with its SHA256, member metadata,
and authoring location. Include that archive as an explicit input, verify its
hash, and extract it into the original npm cache directory before running the
original install with `--offline`. Do not replace the application image with
the prepared authoring image.

The declarative adapter validates a single completed receipt entry and retains
the archive bytes, so its internal permissions survive the Nix store boundary:

```nix
inputs.npmCache = h.dependencies.importNpmCacheArchive {
  archive = ./inputs/npm-cache.tar.gz;
  receipt = ./locks/frontend-receipt.json;
};
```

`frontend-receipt.json` is the selected `results.frontend` object, not the batch
envelope. The adapter checks the offline lifecycle proof, verifies the archive
hash, and independently validates its members. It does not install packages or
declare that the application builds successfully. In a native Linux build,
extract this input with `tar --numeric-owner -xzpf` into the reviewed npm cache
directory and retain the original installation command with `--offline`.

For lockless installs, write the receipt's `generated_lock` bytes next to the
unchanged source manifest. Check both hashes before installation and confirm
the generated lock stays unchanged afterward. Preserve the original npm flags,
environment, working directory, lifecycle behavior, ownership, and cache path.
An npm cache directory alone is not evidence that these conditions match.

`mango.authoring.cache_archive.pin_cache_archive` is the reusable storage layer:
it validates a manager-created gzip tar, rejects paths that escape the archive,
links, special files and duplicate members, enforces size limits, inventories
file hashes/modes/owners, and publishes the archive atomically by SHA256. It does
not infer a dependency lock or certify a package-manager replay.
