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

## Lock an apt installation

```sh
nix-shell --run 'nix2rle-apt-lock --lock "$ENV/inputs.lock.json" \
  --prefix debs/target --base docker.io/library/ubuntu@sha256:<digest> \
  --source "deb https://snapshot.ubuntu.com/ubuntu/20261001T000000Z jammy main universe" \
  curl openssh-server'
```

Runs apt inside the pinned base with only the given sources (requires Docker),
selects the files the install adds, downloads each one, checks it against apt's
own index hash and records its URL and SHA-256 under the prefix. Existing
entries under that prefix are replaced; other entries are kept. Use dated
archive snapshots so the URLs keep serving the same bytes.

Install the locked set offline in a `mkLinuxImage` stage:

```sh
sh /.nix-inputs/files/stage-debs.sh /var/cache/apt/archives \
  /.nix-inputs/files/debs/target/*.deb
apt-get install -y --no-download --no-install-recommends /var/cache/apt/archives/*.deb
```

Re-lock after changing the base image, sources or package list. The tool does
not see packages that later build commands install; list them explicitly.
