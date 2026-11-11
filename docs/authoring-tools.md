# Authoring tools

These tools contact registries and package archives. Run them while authoring;
commit only their output. Builds read the committed pins and locks offline.

## Pin a base image

```sh
nix-shell --run 'nix2rle-pin-base docker.io/library/ubuntu:22.04 --nix'
```

Resolves a tag to its linux/amd64 manifest digest, then fetches that digest
with `fetchLinuxBase` to learn the Nix content hash. The printed
`{ image; digest; hash; }` is accepted by `fetchLinuxBase`, `pinnedImage` and
`mkContainer`'s `base`. An already pinned `image@sha256:...` keeps its digest.

## Lock distro packages

```sh
nix-shell --run 'nix2rle-lock-packages --lock "$ENV/inputs.lock.json" --manager apt \
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

From Python, `nix2rle.authoring.packages.lock_request(LockRequest(...), lock)`
also accepts a per-stage `target_release` (apt `-t`) and `extra_sources`.
Resolutions are cached by key in `NIX2RLE_LOCK_CACHE` (default
`~/.cache/nix2rle/locks`); `NIX2RLE_LOCK_OFFLINE=1` makes a cache miss an error.

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
