# Package state and reusable authoring prefixes

`mango.authoring.package_state` exposes package resolution, installed authoring
prefixes and retained APT indexes. Run Docker-backed authoring on a suitable
remote machine. These APIs do not build final environment images.

## Declare the complete transaction timeline

`mango-lock-packages --request request.json --lock inputs.lock.json --receipt packages.json`
accepts the complete `LockRequest` JSON model. Existing `--base`, `--source` and
`--stage` flags remain supported. Do not combine those flags with `--request`.

```json
{
  "manager": "apt",
  "base": "docker.io/library/ubuntu@sha256:<64 hex digits>",
  "sources": ["deb https://snapshot.ubuntu.com/ubuntu/20261112T000000Z focal main restricted universe multiverse"],
  "stages": [
    {"packages": ["ca-certificates", "curl"], "install_recommends": null},
    {"packages": ["build-essential"], "update_before": true}
  ]
}
```

The example source list is abbreviated: declare every original suite and
component, including updates, security and backports when enabled. No repository
is inferred or silently added. Stages execute in order against the preceding
installed package state. Unknown model fields are rejected.

| Stage field | Meaning |
| --- | --- |
| `sources` | Replace source lines at this transaction; absent retains them |
| `extra_sources` | Append sources at this transaction |
| `target_release` | Original APT target suite (`-t`) |
| `install_recommends` | `false`: explicit no-recommends; `null`: inherit policy; `true`: explicit recommends |
| `repository_keys` | Hash-pinned public URL/local key inputs with exact target `path`, `sha256`, `dearmor`, `mode` |
| `apt_preferences` | Exact preferences `path` and `content` |
| `operation` | `apt_install`, `dpkg_then_fix`, or update-only `apt_update` |
| `local_debs` | Hash-pinned `.deb` inputs preserving original target paths |
| `dpkg_options` | Supported original dpkg flags; only for conditional repair |
| `clear_lists_before`, `update_before` | Preserve index cleanup/update order |
| `debconf_selections` | Explicit original noninteractive configuration lines |
| `policy_rc_d` | `"deny"` writes the original minimal `#!/bin/sh` / `exit 101` service-start policy and applies `chmod +x` before this transaction |

`dpkg_then_fix` retains the original dpkg-first, repair-on-failure transaction;
it is not flattened into an unconditional APT install. Public key and local-deb
inputs carry `urls` or an absolute author-host `author_source`. Transport paths
are excluded from canonical request identity; file SHA256 remains bound.

Python callers use `resolve_packages(LockRequest(...), Path("inputs.lock.json"))`.
The returned receipt contains the full request, ordered artifact prefixes, exact
archive SHA256 closure, and `canonical_request_sha256`. The ordinary inputs lock
remains the build input.

## Prepare an exact shared toolchain state

```sh
mango-prepare-prefix packages.json --count 2 --output prepared.json
mango-prepare-prefix prepared.json --observe /usr/sbin/policy-rc.d --output observed.json
mango-prepare-prefix prepared.json --release
```

`prepare_prefix(receipt, count=None, retain=True)` installs the selected complete
prefix and checks every selected archive name and SHA256 against the receipt.
A changed repository selection fails before an image binding is returned. The
result includes:

- `prepared_image`: immutable local Docker `id`, original immutable `base`, and
  full canonical prefix `apt_request_sha256`;
- `apt_receipt`: the exact prefix request and selected artifact closure;
- `selection_matches_lock`, transaction count and an optional cache lease.

Dependency resolvers can reuse this binding for Python, Node, Go or other
application toolchains without reinstalling identical distro prerequisites.
It includes only the declared package transactions. Callers still bind original
environment variables, working directory, source files and other setup commands
in the dependency resolver's request.
The cache keys include base, repository timeline, keys, preferences, install
policy and ordered transactions. Its existing image-count/byte bounds still
apply. Set `MANGO_AUTHOR_MIN_FREE_BYTES` to enforce an author-host disk reserve
before starting prefix or index work. A lease is a stopped, explicitly owned container that protects the image
from cache eviction. `release_prefix(prepared_receipt)` verifies ownership and
image identity before removing only that lease. `--no-retain` omits it when the
caller already owns equivalent lifetime protection.

`observe_prefix_files(prepared_receipt, paths)` reads file hashes, numeric modes,
owners and sizes in the exact immutable image with both networking and image
pulls disabled. It reports symlinks, missing paths and nonregular files
separately. An observation binds the base and canonical package-prefix request;
it can prove an original setup write is already satisfied at that boundary.
Do not infer equivalence from base-image observations if intervening package or
setup operations might change the file. Preserve the original write in the
environment recipe even when authoring can reuse identical state.

The prepared image is local authoring infrastructure, not a portable input and
not an environment translation. Builds still reconstruct the original layers
from base pins, package locks and declared source inputs.

## Retain the original APT index state

`mango-lock-indexes index-request.json --directory index-inputs --output indexes.json`
accepts exactly four fields: `package_receipt`, zero-based `transaction`,
`runtime_sources` (original source-file paths to exact text), and `uri_mappings`
(entries with complete `author_uri`/`runtime_uri` suite prefixes).

`capture_indexes(receipt, transaction, mappings, runtime_sources, directory)`
installs preceding transactions, captures the selected update's index bytes,
verifies its package selection against the lock, then performs a real
`--network none` replay against the original runtime repository files. Structured
APT policy must match after only the explicit URI substitutions. The receipt
binds the full author request, update position, source bytes, mapping, archive
SHA256, member paths/modes/owners and successful offline policy comparison.

`mango.authoring.apt_sources.active_sources(request_dict, transaction)` and
`mappings(active_sources, runtime_sources)` derive explicit suite mappings for
reviewed `.list`/`.sources` syntax. They reject ambiguous suites and unsupported
source fields; they do not guess repository policies.

Replay the declared tar at the original update location using the vendored
`restore-apt-indexes.sh ARCHIVE SHA256` helper. Preserve original conditionals
and later list deletion. Index-only updates use `operation: "apt_update"`.
A captured dated snapshot proves that authored snapshot's state; it does not
claim equivalence to unknown historical contents of a mutable repository.
