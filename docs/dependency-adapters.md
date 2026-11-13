# Pinned dependency adapters

The contract is **resolve → fetch → prepare → build → image**. Only resolve and
fetch may contact upstream services. Every downloaded file has a locked content
hash. Fetch outputs and prepared layouts are ordinary Nix store objects,
distributed through a Nix binary cache; no Maven/npm/PyPI proxy is needed.

## API

Import `lib/adapters/default.nix` with the pinned `pkgs`:

```nix
let deps = import (harness + /lib/adapters) { inherit pkgs; }; in
{
  inputs = deps.fetchLockedInputs { lock = ./inputs.lock.json; };
  artifact = deps.fetchDependency {
    name = "demo-1.0.jar";
    urls = [ "https://repo.example/org/example/demo/1.0/demo-1.0.jar" ];
    sha256 = "<reviewed SHA-256>";
  };
}
```

For a package manager without an adapter below, including Maven, Gradle and
NuGet, lock what its build downloads with [record and replay](record-replay.md).

`fetchDependency { name; urls; sha256; }` is the common immutable leaf fetcher;
supply `sha512` instead of `sha256` when upstream publishes only SHA-512.
`fetchDependencyCache { name; artifacts; metadata ? []; }` assembles safe relative
paths from individually fetched artifacts. Metadata records contain `path`,
`content`, and its hexadecimal SHA-256. Duplicate/traversing/overlapping paths and
metadata hash mismatches fail evaluation. URLs with obvious credential fields
or userinfo are rejected. This is not a general secret scanner: reviewed locks
must never contain credentials or signed expiring URLs.

| Adapter | Store output | Offline consumption | Limits |
| --- | --- | --- | --- |
| `fetchCargoCache { lockFile; outputHashes ? {}; }` | Vendored crates selected from `Cargo.lock` using its checksums | Use `cargoSetupHook` with `cargoDeps`; compile with `--frozen --offline` | Git sources require supplemental content hashes; build-script downloads need explicit additional inputs |
| `fetchLockedInputs { lock; prefixes ? null; extra ? {}; }` | Linked files from `inputs.lock.json`: downloads by URL, files committed beside the lock, and `extra` store paths | Read from the input directory, or replay with `mango-proxy` | Lists exact files; resolves nothing |
| `fetchPythonWheelhouse` | Flat directory linking hashed wheels | `pip --no-index --find-links`; `UV_OFFLINE=1` | Select wheels for pinned Python ABI/OS/CPU; source distributions need separately built wheels and build dependencies |
| `fetchNpmCache { source; }` | Per-package Nixpkgs downloads and localized manifests | Use `npmConfigHook` with `npmDeps`; ordinary sandboxed build | Lock v2/v3; registry HTTP(S) with integrity; Git/workspace inputs explicitly rejected for now |
| `fetchBunCache` | Hash-verified whole dependency-cache tree | Copy to private `BUN_INSTALL_CACHE_DIR`; build without network | Explicit `projectFiles` includes all workspace package manifests/config; whole-tree cache, not per-package deduplication |
| `fetchAptCache`, `fetchApkCache` | Linked, flat archives from explicit file locks | Copy/install exact archives with downloads disabled | Not dependency resolvers or rootfs installers; retain signed repository metadata/keys when resolver verification needs them |
| `fetchDependency` | Source tarballs, Git commit archives, tool binaries | Reference the store path directly | Complete URL plus content hash required |

For distro-native builds, use `fetchLinuxBase` and `mkLinuxImage` with an
explicitly authored offline installation script. A package directory alone is
not a complete root filesystem. See [Linux images](linux-images.md).

Other package managers use explicitly authored file inventories or a deliberately
selected native Nixpkgs helper. This API does not discover dependencies or
translate Dockerfiles.

## Immutable store, writable per-build caches

Add `deps.prepareCache` to `nativeBuildInputs`, then:

```sh
prepare-dependency-cache "$STORE_CACHE" "$TMPDIR/cache"
```

The destination must not exist. The helper dereferences immutable links into a
private writable directory. Package managers must never modify `/nix/store` or
share an ambient host HOME. This intentionally copies data at build time; the
shared store layout itself links artifacts to preserve reuse and GC references.

Bun accepts `{ bun; source; hash; projectFiles ? [ "package.json" "bun.lock" ]; }`.
Supply workspace manifests, patches and required local inputs explicitly. Network
fetching uses `--ignore-scripts`; lifecycle scripts run only in the offline build.
Any lifecycle download must be represented as a separate pinned input (such as browser binaries). Config files must not embed tokens.

## Enforcing and proving offline builds

`mkOfflineApplication` rejects fixed-output/impure/no-chroot application recipes
and runs a Linux network-interface check from `preBuild`. Custom `buildPhase` scripts
must invoke `runHook preBuild`. The check is defense in depth: actual enforcement
is Nix `sandbox=true`, `sandbox-fallback=false`; it is not a replacement for those
settings. Pin package-manager/toolchain versions and platform in the recipe.

The existing `mango /path/to/environment check` rebuilds rather than accepting existing
application outputs, disables substitution and remote builders, and checks output
identity. `tests/support/check-isolated-store.sh` provides the stronger proof:
restore to an empty store, remove final outputs there, then rebuild under an outer
network namespace as well as the Nix sandbox. It does not delete host-store paths.

```sh
# The fourth argument is the actual trusted PUBLIC signing key.
nix-shell --run 'bash tests/support/check-isolated-store.sh /path/to/export /tmp/new-store file:///path/to/cache "$CACHE_PUBLIC_KEY"'
```

The cache restores explicit receipt paths only; missing objects fail without an
upstream fallback. The final application is then rebuilt, not substituted. The
current isolated check reports output differences separately from successful
offline compilation: pinning alone does not guarantee bit-for-bit determinism.

## Publication and retention

Publish every path in the frozen `inputs.json` receipt to a standard Nix binary
cache, not only the final runtime images. Retain source, toolchain, package
archives, metadata and derivations as well as outputs. Retain receipts with
environment releases and configure cache retention accordingly.

Private signing keys belong only to trusted publishers. Readers use the public
verification key and cache read access. Do not pass cache credentials or signing
keys into application build sandboxes. Local store paths are not automatically
published.

## Explicit input inventory

The supported authoring workflow uses complete native locks where available,
and an agent-written `inputs.lock.json` for other inputs, described
in [Agent authoring](agent-authoring.md). The harness does not scan task files,
inspect upstream repositories or discover dependencies. Explicit native-lock readers are supported. Other ecosystem
helpers are optional layout utilities; the agent owns completeness and all
package-manager-specific preparation commands.

## pnpm store inputs

`dependencies.fetchPnpmCache`, `dependencies.fetchPnpmTool`, and
`dependencies.preparePnpmStore` support explicit pnpm project inputs and an exact
provided pnpm version. See [pnpm cache inputs](pnpm-cache.md) for the supported
store layouts, workspace inventory, and original-target offline validation steps.
The adapter's evaluation and input checks do not establish that a particular
application builds offline; that requires its real toolchain and build commands.

## Composer cache inputs

`dependencies.fetchComposerCache` lays out pinned Composer dist archives at their
observed cache paths. See [Composer cache inputs](composer-cache.md).
