# Authoring tools

These tools contact registries and package archives. Run them while authoring;
commit only their output. Builds read the committed pins and locks offline.

## Pin a base image

```sh
nix-shell --run 'mango-pin-base docker.io/library/ubuntu:22.04 --nix'
```

Resolves a tag to its linux/amd64 manifest digest, then fetches that digest
with `fetchLinuxBase` to learn the Nix content hash. The printed
`{ image; digest; hash; }` is accepted by `fetchLinuxBase`, `pinnedImage` and
`mkContainer`'s `base`. An already pinned `image@sha256:...` keeps its digest.

## Pin a file download

For an explicitly selected download, `mango-pin-file URL --path sources/file.tar.gz`
streams its bytes and prints one `{path, urls, sha256}` entry for
`inputs.lock.json`. It does not execute or extract the download. Public HTTP(S)
URLs are supported; credentials and query strings are rejected. The hash pins
the bytes fetched at authoring time; it is not a verification of an upstream
release signature. Preserve any signature checks required by the original build.

## Pin a Git source

```sh
nix-shell --run 'mango-pin-source https://github.com/owner/app.git --rev v1.2.3'
```

Resolves a branch, tag or `HEAD` (the default) to its full commit, then fetches
that commit with the harness's own `pkgs.fetchgit` to learn the Nix content
hash. The printed `{ repository; commit; hash; }` (JSON, or Nix with `--nix`)
is what `pkgs.fetchgit { url = repository; rev = commit; inherit hash; }` checks,
with `fetchgit`'s defaults, submodules included. A full commit is not resolved
again. Do not hash a checkout with `nix hash path`: it includes `.git` and
skips submodules, so the build rejects it.

If build scripts require Git metadata, pass `--leave-dot-git`; use
`--deep-clone` for history and `--fetch-tags` for tags. Both imply retaining
`.git`. `--no-submodules` disables submodule fetching. Nondefault requests
return `leaveDotGit`, `deepClone`, `fetchTags`, and `fetchSubmodules` alongside
the pin; pass those exact attributes to `pkgs.fetchgit` too. The content hash
is calculated with those options, so changing them requires a new pin. This
preserves the requested fetchgit checkout semantics; it does not promise the
same refs, shallow boundaries, or Git configuration as an arbitrary original
clone script. Compare any history-dependent builds before qualification.

## Lock Python wheels from a pip report

On an authoring host with the target Python interpreter and platform, resolve
the complete wheel closure without installing it:

```sh
python -m pip install --dry-run --ignore-installed \
  --report report.json -r requirements.txt
mango-lock-python report.json --lock inputs.lock.json --prefix python/app
```

The report converter records every wheel URL and SHA-256 for
`dependencies.fetchLockedInputs`. Select the `python/app` prefix and install
with `pip --no-index --find-links` pointing at that directory. Preserve the
original requirements and constraints for the offline install. The report must
come from the target platform; this converter does not establish ABI compatibility
or prove that application builds have all their inputs. Source distributions,
VCS/editable inputs, missing hashes and URLs carrying credentials or queries
are rejected; author those source builds separately. An empty report has no
dependencies and does not establish closure completeness.
Preserve the original resolver's options: adding `--only-binary` can select an
older wheel where the original install would have chosen a newer source archive.
Also omit `--ignore-installed` when the original transaction relies on packages
already present in the target interpreter.

## Lock distro packages

```sh
nix-shell --run 'mango-lock-packages --lock "$ENV/inputs.lock.json" --manager apt \
  --base docker.io/library/ubuntu@sha256:<digest> \
  --source "deb https://snapshot.ubuntu.com/ubuntu/20261001T000000Z noble main universe" \
  --stage "ca-certificates python3" --stage "build-essential"'
```

Runs the package manager (apt, or opkg with `--manager opkg`) inside the pinned
base with only the given sources (requires Docker). Each `--stage` is one
transaction, resolved after the earlier stages are installed, so a later stage
sees what the earlier ones left behind. Every selected file is downloaded and,
for apt, checked against the archive's own index hash before its URL and SHA-256
are recorded under `<manager>/<key>/<stage>`. The key is derived from the base,
sources and stages, so changing any of them selects a new prefix. Entries under
the request's prefixes are replaced; other entries are kept. The command prints
the prefixes. Use dated archive snapshots so the URLs keep serving the same bytes.

From Python, `mango.authoring.packages.lock_request(LockRequest(...), lock)`
also accepts a per-stage `target_release` (apt `-t`) and `extra_sources`.
Resolutions are cached by key in `MANGO_LOCK_CACHE` (default
`~/.cache/mango/locks`); `MANGO_LOCK_OFFLINE=1` makes a cache miss an error.

For large remote authoring batches, `MANGO_APT_PREFIX_CACHE=1` also caches Docker
snapshots after intermediate apt transactions. Requests reuse a snapshot only
when the immutable base, initial repositories, every preceding transaction and
repository change match exactly. Receipts bind the actual Docker image ID and
retain the original package URI/hash records. Final environment builds still use
the regular file locks; these snapshots are only an authoring optimization.
Only cache-labeled images are evicted, with defaults of 32 images and 64 GiB
(shared image layers are counted conservatively). Configure these through
`MANGO_APT_PREFIX_MAX_IMAGES` and `MANGO_APT_PREFIX_MAX_BYTES`. Active containers
and prefix locks protect in-use snapshots, so temporary usage can exceed these
limits while requests finish. Leave this option unset for ordinary resolution.

`PackageStage.install_recommends` preserves apt's recommendation policy: the
default `False` emits `--no-install-recommends`, `None` inherits the image's apt
configuration, and `True` emits `--install-recommends`. Do not use the default
when translating an original command that omitted the flag.

Local Debian files can be declared with `local_debs=[LocalDeb(path="/tmp/app.deb",
sha256="...", urls=("https://example.org/app.deb",))]`. For an existing source
asset, use `author_source="/absolute/remote/source/app.deb"` instead of URLs.
The authoring host verifies the bytes, mounts a content-addressed copy read-only,
and copies it to the declared path at that transaction. The original target path
and SHA256 are part of the cache key; the author's source-file location is not.
List local paths in `packages` for ordinary apt installs. Selected repository
dependencies appear in the normal lock; the supplied root files remain explicit
source/locked inputs. `local_deb_metadata(request)` returns their package names,
versions and architectures after resolution.

For original `dpkg -i ... || apt-get -f install -y` transactions, use
`operation="dpkg_then_fix"`, empty `packages`, the declared `local_debs`, and
explicit `dpkg_options` such as `("--force-confold", "--force-downgrade")`.
The resolver preserves the conditional repair branch and rejects repair plans
that remove packages. `clear_lists_before=True` preserves a preceding removal
of apt indexes; `update_before=True` explicitly refreshes them before a later
transaction. The offline build must preserve the same dpkg/repair conditional;
`install-debs.sh --repair` supports its locked repair branch. All request models
reject unknown fields, so an older authoring host cannot silently ignore new
transaction semantics.

When an earlier source instruction pre-seeds a package installer, carry its
literal lines in `debconf_selections=("couchdb couchdb/mode select none",)` on the
next transaction. They are applied with `debconf-set-selections` before the
install and are part of the prefix-cache identity. Preserve the same seed in the
offline build; choosing a default installer mode can change service behavior or
make package configuration fail.

Install each stage offline, in order, in a `mkLinuxImage` stage:

```sh
sh /.nix-inputs/files/stage-debs.sh /var/cache/apt/archives \
  /.nix-inputs/files/apt/<key>/0/*.deb
apt-get install -y --no-download --no-install-recommends /var/cache/apt/archives/*.deb
```

Select only an image's own entries with
`h.dependencies.fetchLockedInputs { lock = ./inputs.lock.json; prefixes = [ "apt/<key>" ]; }`.
Re-lock after changing the base image, sources or packages. The tool does not see
packages that later build commands install; list them explicitly.

For Alpine, use `--manager apk` and explicit `--source` repository URLs.
The resolver simulates each install inside the pinned base and fetches the
exact selected package versions (retaining already-installed dependencies),
verifies each APK signature with that base's trusted keys, and records SHA-256
hashes under `apk/<key>/<stage>`. Native builds can install the selected
archives with `apk add --no-network`; signature checking remains enabled.
Alpine stage requests do not accept apt-specific release or extra-source fields.
Archive availability is separate from integrity: retain these inputs in a
binary cache when upstream repositories rotate package versions.

A Python `PackageStage` may also specify `sources=(...)` to **replace** all apt
repositories immediately before that transaction. Omit it (`None`) to retain
the previous stage's repositories; `extra_sources` still appends after a
replacement. This preserves builds that first install bootstrap tools from the
base's selected repositories, then switch to an older snapshot for the service.
The chosen sources persist into later stages. Replacements are included in the
lock key; requests without replacements keep their existing keys and caches.

APT repository state can also be declared explicitly. `PackageStage.repository_keys`
accepts hash-pinned public key inputs with a target path, optional ASCII dearmor
(default true), and numeric mode (default 0644). `apt_preferences` accepts explicit
APT preference file paths and contents. Keys and preferences are installed before
that stage's repositories are refreshed; prerequisite tools belong in an earlier
transaction. Both fields participate in the dependency cache identity.

`operation="apt_update"` represents an index refresh without installing packages.
The authoring API `mango.authoring.apt_indexes.capture` captures repository indexes
before an installation, checking the selected archive names and SHA256 values
against the supplied lock for the whole preceding transaction sequence. Its
regular tar input retains file bytes, modes, and numeric owners. Explicit
suite-level author/runtime URI mappings rename APT's index filenames while
preserving signed release metadata. Replay belongs at the original update's shell
position, including its conditional branches. A snapshot replay describes the
chosen authoring repository state; it does not establish equivalence to historical
contents of an unpinned mutable repository.
