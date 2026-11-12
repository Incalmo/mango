# pnpm dependency cache

`lib/adapters/pnpm.nix` is a thin, fail-closed front end for the pinned Nixpkgs
`fetchPnpmDeps` helper. It is **not yet exported** from `lib/adapters/default.nix`;
integrate with `// import ./pnpm.nix { inherit pkgs; }` (it returns an attribute set
with `fetchPnpmCache`, `fetchPnpmTool`, `preparePnpmStore`). Status: evaluation and
mocked-script tests only. No real fetch or offline build has been run, so nothing
here is qualified or claimed metadata-equivalent to the original build.

## What is delegated, what is added

Delegated to Nixpkgs (`pkgs.fetchPnpmDeps`, `pnpm` argument = your exact pnpm):
the network-only `pnpm install --frozen-lockfile --ignore-scripts --force` into a
private store, removal of `checkedAt` timestamps, JSON key sorting, `tmp`/`projects`
removal, the pnpm 11 SQLite index dump (`fetcherVersion = 4`), permission
normalisation and a reproducible `pnpm-store.tar.zst`. Mango does not touch store
metadata.

Added by Mango, as a non-fixed-output derivation that runs before any network
access: copy only the listed files, then reject (never drop silently) unlisted
manifests/config, unsafe or missing workspace importers/patches, non-registry or
credentialed lock entries, embedded credentials, mismatched `packageManager` /
`devEngines.packageManager`, a too-new lockfile, duplicate/aliased YAML keys, symlinked
inputs, and pnpmfiles.

## API

```nix
let
  pnpm = deps.fetchPnpmTool { version = "10.17.1"; hash = "sha256-…"; };  # optional
in deps.fetchPnpmCache {
  inherit pnpm;
  source = ./source;                       # path, or a derivation such as fetchgit
  projectDir = ".";                        # e.g. "frontend"; files are relative to it
  projectFiles = [ "package.json" "pnpm-lock.yaml" "pnpm-workspace.yaml" ".npmrc"
                   "packages/a/package.json" "patches/x.patch" ];
  hash = lib.fakeHash;                     # run once, copy the "got:" hash
  # optional: expectedPnpmVersion, fetcherVersion, pnpmWorkspaces, pnpmInstallFlags,
  # prePnpmInstall, acknowledgedUnlisted, acceptUnvettedMajor, name
}
```

Output: `pnpm-store.tar.zst` and `.fetcher-version`. `passthru.mangoPnpm` records
pnpm/Node versions, `fetcherVersion`, expected store directory (`v3`/`v10`/`v11`,
derived from the major — verify with `tar tf`), and `passthru.inputs` is the checked
input tree.

Behaviour you must know:

- **No version substitution.** The `pnpm` you pass is the one used; `expectedPnpmVersion`
  and `package.json` `packageManager` (`pnpm@x.y.z[+sha]`) must match it exactly, else
  evaluation/build fails; a non-pnpm `packageManager`/`devEngines.packageManager` also fails and
  an exact `devEngines.packageManager` pnpm version must match. The `+sha` suffix is not
  compared: give `fetchPnpmTool` the hash of that exact registry tarball. `engines.pnpm` and
  `devEngines` version **ranges** are not checked.
- **Supported pnpm:** majors 10 and 11 (what pinned Nixpkgs ships: 10.34.5, 11.22.0, and
  the 10.29.2/10.34.0 variants). Major 9 (store `v3`) needs `acceptUnvettedMajor = true`
  (the helper cleans `v3` but Nixpkgs does not ship or test pnpm 9). Majors ≥ 12 and
  other values **fail**: the helper does not normalise their store directory. Needed
  upstream capability: `fetchPnpmDeps` must remove timestamps, `projects` links and
  non-deterministic index state for that store directory. `fetcherVersion = 3` is
  rejected for pnpm 11. Default `fetcherVersion` is 3 (<11) or 4 (≥11); changing it
  changes the hash.
- **Explicit inventory.** Any unlisted `package.json`, `pnpm-lock.yaml`,
  `pnpm-workspace.yaml`, `.npmrc` or pnpmfile in the source (excluding `node_modules`,
  `.git`) aborts; list it, or add its relative path to `acknowledgedUnlisted` if it is
  genuinely not part of the install (e.g. a fixture). Path sources are filtered before
  import so unlisted files, and credentials in them, never enter the store.
  `projectFiles` entries are relative to `projectDir`; `acknowledgedUnlisted` entries are
  relative to the **source root**, so with `projectDir = "frontend"` the source-root
  `package.json`/`pnpm-lock.yaml` etc. must be acknowledged there. Pnpmfile names
  (`.pnpmfile.cjs`, `pnpmfile.cjs`, `.pnpmfile.mjs`, `pnpmfile.mjs`) are matched by basename at
  any depth, with or without a `projectDir` prefix.
  Every lock importer needs its listed `package.json` (and `pnpm-workspace.yaml`);
  every patch named by the lock, `pnpm.patchedDependencies` or the workspace file must
  be listed. Importer keys, patch paths, `projectFiles`, `projectDir` and
  `acknowledgedUnlisted` must be safe relative paths: no absolute path, empty, `.` or `..`
  segment (a leading `./` is rejected too); listed names are limited to
  `[A-Za-z0-9._@+=,~ %/-]` (no quotes, `$`, backticks, globs or backslashes). Listed files
  must be regular files inside the source (symlinks rejected). Workspace globs themselves are
  not expanded; the lockfile importers are authoritative.
- **Strict YAML.** The lock and every listed `pnpm-workspace.yaml` go through a PyYAML check
  that rejects duplicate map keys (PyYAML/`yq` silently keep the last, which could hide a Git
  entry behind a clean second `packages:`), anchors/aliases and merge keys. Multi-document
  lockfiles are accepted and every document's `lockfileVersion` is checked.
- **Fail closed on sources.** Every `packages` entry in the lock must have an `integrity`
  and no `type`/`directory` resolution; tarball URLs must be `http(s)://` (so `file:` and
  `git+…` fail) and, case-insensitively, free of userinfo and token-like query parameters
  (`token`, `access_token`, `auth`, `password`, `secret`, `key`, `sig`, `signature`,
  `X-Amz-*`, `X-Goog-*`). Every listed `.npmrc` and `pnpm-workspace.yaml` (any depth, not just
  the root) is screened for `_authToken`/`_auth`/`_password`, `tokenHelper`,
  `username`/`email`/`password`/`token` keys, `${…}` interpolation, the same URL userinfo/token
  patterns on any registry or scoped-registry line, and settings that would divert the fetch
  (`store-dir`, `userconfig`, `globalconfig`, `offline`, `prefer-offline`). This is a screen, not
  a secret scanner. Private registries/Git remain unsupported. Config outside the listed
  files (for example `$HOME/.npmrc`) is outside this check.
- **Scripts and flags.** The fetch uses `--ignore-scripts`; flags that weaken or divert it are
  rejected, bare or as `flag=value`: `--no-frozen-lockfile`, `--frozen-lockfile=false`,
  `--no-lockfile`, `--lockfile=false`, `--lockfile-only`, `--fix-lockfile`,
  `--no-ignore-scripts`, `--ignore-scripts=false`, `--offline`, `--prefer-offline`,
  `--registry`, `--config*`, `--store-dir`, `--dir`/`-C`, `--prefix`, `--lockfile-dir`,
  `--workspace-root`/`-w`, `--global`/`-g`, `--pnpmfile`, `--global-pnpmfile`,
  `--no-ignore-pnpmfile`, `--ignore-pnpmfile=false`. Positional arguments and separate flag
  values are not parsed. pnpmfiles execute project code during resolution, so any listed
  pnpmfile, or a `pnpmfile` setting in config, is rejected unless `--ignore-pnpmfile` is in
  `pnpmInstallFlags` (then pass it in the offline build too; a lock `pnpmfileChecksum` may
  then disagree). `prePnpmInstall` is arbitrary shell with network access: caller-reviewed.
- **Registry.** The helper passes `--registry="$NIX_NPM_REGISTRY"`; when unset this is
  empty and relies on pnpm's default. A custom registry in `.npmrc` must be
  credential-free; how it interacts with the helper's flag is unverified.
- **Platform.** `--force` fetches optional dependencies for every platform. Native-addon
  postinstall results, `pnpm rebuild`, and `pnpm deploy` happen only in the offline build.

## `fetchPnpmTool { version, hash, nodejs ? pkgs.nodejs-slim }`

Wraps the pinned Nixpkgs `generic.nix` with the exact `registry.npmjs.org/pnpm/-/pnpm-<version>.tgz`
tarball hash (hash is yours; no version selection). Deviations from the raw tarball, inherited
from Nixpkgs: `dist/reflink.*node` and `dist/vendor` are removed (copy fallback; no vendored
binaries), and the Node is the one you pass (default `nodejs-slim` of pinned Nixpkgs, not
necessarily the original image's Node). The recipe is only exercised upstream for pnpm 10/11;
other versions (9, 12) may fail in `postUnpack`/completions and are unverified.

## Offline consumption

```sh
prepare-pnpm-store "$PNPM_CACHE" "$TMPDIR/pnpm-store"     # preparePnpmStore; destination must not exist
export HOME="$TMPDIR/home"
pnpm config set store-dir "$TMPDIR/pnpm-store"
pnpm config set package-import-method clone-or-copy
pnpm config set manage-package-manager-versions false       # pnpm 9/10
# pnpm 11 instead: export pnpm_config_pm_on_fail=ignore pnpm_config_trust_lockfile=true
pnpm install --offline --frozen-lockfile                    # original scripts run here, and only here
```

Run it in the same `projectDir`, with the same pnpm, `pnpmWorkspaces` filters and
`pnpmInstallFlags` as the fetch. Do not share store formats across pnpm versions. `prepare-pnpm-store` mirrors the pinned
`pnpmConfigHook` restore (zstd untar, make writable, rebuild `v11/index.db` from
`index.db.sql`); the hook also sets `pnpm_config_trust_lockfile=true` for pnpm 11 and
`npm_config_{arch,platform}`, which are left to you. Mango
does not use `pnpmConfigHook`, because it forces `--ignore-scripts` and sets platform
variables; it is acceptable if you want that behaviour. Use `mkOfflineApplication`
for the build; the cache is an immutable store path, the store copy is private/writable.

## Corpus patterns (tm-rle-black, Dockerfile/build.sh scan)

pnpm 9.7.0 (corepack), 10.2.0, 10.11.0, 10.17.1, 10.26.2, 10.30.3, 11.3.0, 11.21.0 and
12.4.1–12.11.2, installed by `npm i -g pnpm@x`. Pnpm 12 therefore **cannot** use this
adapter today. Other patterns seen: `package.json pnpm-lock.yaml pnpm-workspace.yaml .npmrc`
copied together, `frontend/` subdirectory projects (use `projectDir`), `--filter`
installs (use `pnpmWorkspaces`; the lockfile is still the whole workspace),
`--ignore-pnpmfile`, `pnpm fetch`, `pnpm deploy`, `pnpm rebuild <pkgs>` and cache-mount
stores. Git-sourced or `file:` dependencies would be rejected.

## Tests and remote validation

Local (no network, no builds): `nix-shell -p yq jq --run 'nix-shell --run "python -m unittest tests.lib.test_pnpm_cache"'`
(evaluation tests plus the generated input-check script run against fixtures). The script
tests use the **real python `yq` 3.4.3** from `PATH` (the same attribute, `pkgs.yq`, the pinned
helper uses) and fall back to a PyYAML stand-in only when no `yq` is on `PATH`. The strict-YAML
step runs with the shell's PyYAML instead of its own store python. Not covered locally: the
FOD build, a real pnpm, a real registry, `preparePnpmStore` execution.

### Remote smoke (not yet run)

Candidates from `tm-rle-black/web` (exact tool, source pin, patches; the task patches in
all three touch no `package.json`/lock/`.npmrc`, so the cache is keyed on the pinned
checkout, not the patched tree):

| Family | Original manager | Source pin | Project | Notes |
|---|---|---|---|---|
| chatwoot-task-5 (**start here**) | `npm install -g pnpm@10.2.0` (node 24.22.1) | `chatwoot/chatwoot` `b08081b2311a7bbea20469931be9219c81c52726` | root; `package.json` + `pnpm-lock.yaml` only | `pnpm install --frozen-lockfile --ignore-scripts`; 10.2.0 is not a Nixpkgs variant (`fetchPnpmTool`) |
| apache-answer-task-1 | corepack `pnpm@9.7.0` (node 20.18.1) | `apache/answer` `3b9f1370612e690a0b7f230f05e688930db4c6d3` | `projectDir = "ui"` | needs `acceptUnvettedMajor`, store `v3`; original runs `pnpm pre-install`, which is not a plain install |
| keystonejs-task-1 | `npm install -g pnpm@11.21.0` (node 24.11.1) | `keystonejs/keystone` `e49642c9c84e06483c65572b387b275692b30fc1` | workspace, `pnpmWorkspaces = [ "@keystone-6/mono-repo" "@keystone-6/example-auth..." ]` | pnpm 11, fetcherVersion 4; original then runs `pnpm rebuild` |
| teleport-task-1 | `npm install --global pnpm@11.3.0` (node 24.16.0) | `gravitational/teleport` `ddaa46b8f4ee579d43480cd2d3b6a14b18e3ef7d` | workspace | cache-mount store |

Steps for the root smoke (chatwoot, pnpm 10.2.0):
1. Fetch the pinned commit with `fetchgit` (`mango-pin-source` for the hash); confirm in the
   checkout that `package.json` has no `packageManager` other than `pnpm@10.2.0` and that the
   lock has no `git+`/`file:` entries (the adapter rejects either).
2. `pnpm = deps.fetchPnpmTool { version = "10.2.0"; hash = <sri of registry.npmjs.org/pnpm/-/pnpm-10.2.0.tgz>; }`
   (a sha256 SRI of that tarball, obtained remotely, e.g. via `fakeHash`; cross-check the
   registry's sha512 `dist.integrity`); `deps.fetchPnpmCache { inherit pnpm; source = <fetchgit>; projectFiles = [ "package.json" "pnpm-lock.yaml" ]; acknowledgedUnlisted = [ <every other watched file the checkout lists, e.g. .npmrc if present, nested package.json> ]; hash = lib.fakeHash; }`. Evaluation will name any unlisted manifest/config.
3. Build the input derivation first (`.inputs`), then the cache with the fake hash; copy the
   hash; rebuild with `--check` on a second machine.
4. `tar --zstd -tf` shows `./v10/...` and no `tmp`/`projects`; `.fetcher-version` is `3`.
5. In a no-network sandbox on the original image's Node/OS: `prepare-pnpm-store`, then
   `pnpm install --offline --frozen-lockfile --ignore-scripts` with pnpm 10.2.0, followed by the
   task's build; compare `node_modules` and the build output against the original image. Apply
   the task patches only after the install, as the Dockerfile does.
6. Repeat with keystonejs (pnpm 11.21.0, filters, `pnpm rebuild` offline) for the v11/SQLite
   path, and apache-answer `ui/` for the unvetted pnpm 9 path. Negative checks: an unlisted
   manifest, a `projectDir` pnpmfile, a lock with duplicate `packages:` keys (all must fail
   before any network access).

Only step 5 can support an equivalence or qualification claim.
