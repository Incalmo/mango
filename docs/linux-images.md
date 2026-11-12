# Explicit Linux image builds

Use `mkImage` for applications assembled from Nix packages. Use `mkLinuxImage`
when the environment must retain a particular distro, its package database, or
its native installers. Neither path discovers dependencies.

`fetchLinuxBase { image; digest; hash; }` fetches an OCI/Docker image using its
immutable `sha256:…` manifest digest. `image` is the registry/repository reference;
`hash` is the Nix recursive hash of the fetched directory, not the manifest hash.
An author can use `pkgs.lib.fakeHash` once to obtain the expected hash, review it,
then commit the real hash. Retain the resulting store object in the binary cache.
Do not resolve tags during a customer rebuild.

Optional `mirrors = [ "mirror.gcr.io/library/python" ];` supplies alternate
repository names, without tags or digests. The fetcher tries these in order,
then `image`, always with the same `digest` and expected `hash`. Failed copies
are removed before the next attempt. The default empty list retains the direct
fetch behavior. Authors must verify that each mirror serves the pinned manifest;
mirrors never authorize a newer tag or different image content.

For parallel hash discovery, pass a unique `name`, for example
`name = "probe-ubuntu-${builtins.substring 7 12 digest}"`. Otherwise different
fake-hash fetches share the default `linux-base` output path and serialize on
one Nix store lock. After discovery, omit `name` to retain the established cache
path. Naming does not change the fetched bytes or their recursive hash.

`mkLinuxImage` takes these explicit arguments:

Input files and context are materialized as separate immutable store objects.
Context symlinks are preserved. For inputs, structural directories in a link farm
are traversed until an absolute Nix-store reference is found. That referenced
file or directory is copied once, preserving its nested symlinks. Runtime links
such as `/bin/sh` are never followed on the builder. A dependency cache that is
itself a farm should be materialized explicitly before passing it as one payload.
Changing only `buildScript` reuses those objects rather than copying the entire
download inventory into a new cache object. During the VM build, the script,
files and context are mounted individually read-only under `/.nix-inputs`;
their mount points never enter the image layer. The `inputMounts` attribute
exposes the small component map for inspection. This changes native derivations
once when adopting the helper, so rerun the affected qualification checks.

| Argument | Meaning |
| --- | --- |
| `name` | Nix derivation name |
| `base` | Output of `fetchLinuxBase` or a previous `mkLinuxImage` stage |
| `inputs` | Store directory, usually from `dependencies.fetchLockedInputs` |
| `context` | Explicitly selected source/configuration directory |
| `buildScript` | Shell script authored for this distro |
| `buildSteps` | Alternative ordered commands with per-command `env`, `workdir`, and temporary `mounts`; specify exactly one of `buildScript` and `buildSteps` |
| `buildEnv` | Build-only environment variables; defaults to an empty set |
| `config` | Runtime OCI configuration overrides; defaults to an empty set |
| `cores` | Explicit VM CPU count, integer 1–255; default 1. Never inferred from the host |
| `diskSize`, `memSize` | Scratch disk and memory sizes in MiB; defaults 16384 and 4096 |
| `exportPaths` | Absolute paths to export as a separate `.artifacts` tar; defaults to `[]` |
| `sourceDateEpoch` | Timestamp for new layer entries and build `SOURCE_DATE_EPOCH`; defaults to 1 |
| `isolation` | `"vm"` (default): a QEMU VM. `"namespace"`: the Nix sandbox itself, no VM; see below |

The script runs as root using the base's `/bin/sh` in a QEMU Linux VM with no
network device. Loopback is enabled for local service tests; a guard rejects a
VM that has any other interface.

For temporary source mounts, use ordered steps. Mount paths are absolute paths
inside the build root, including staged inputs under `/.nix-inputs`. The kernel
enforces read-only writes through each target; the source retains its original
permissions. Existing target contents reappear after the command, including on
failure. Missing target placeholders are removed. Mounts cannot overlap their
sources, escape the build root through symlinks, or cover runtime/input mounts.

```nix
buildSteps = [
  { command = "mkdir -p /work; cp -R /.nix-inputs/context/. /work/"; }
  {
    command = "./verify-source /observed";
    workdir = "/work";
    mounts = [{ source = "/work"; target = "/observed"; readOnly = true; }];
  }
];
```

Each command starts a fresh shell; persist files or pass explicit `env` values
between steps. This API does not provide network access or dependency discovery.
Set `readOnly = false` explicitly for a writable cache mount. Writes then persist
in its source directory across commands, while the covered target is restored.
Remove temporary cache source directories in a final step to exclude them from
the resulting image.

## Without KVM: `isolation = "namespace"`

A VM build needs the `kvm` system feature for practical speed (on a cloud VM,
nested virtualization). With `isolation = "namespace"` the same build runs in
the Nix sandbox instead: root inside a user namespace mapped over 65536 uids,
so installers can create service users and `chown` to them, in the sandbox's
private network namespace with only loopback. It runs the same `rootfs.py`
build and produces the same layer; a test image built both ways gave a
byte-identical `layer.tar`, including setuid bits, new users and groups, and
whiteouts. `cores`, `diskSize` and `memSize` apply only to the VM.

The building Nix must allow it:

```
experimental-features = auto-allocate-uids cgroups
auto-allocate-uids = true
system-features = uid-range
filter-syscalls = false
sandbox = true
```

`uid-range` derivations need a sandboxed build as root or through the daemon.
`filter-syscalls = false` lets the build set setuid/setgid bits, which the
default seccomp filter refuses; the bits land in the image layer tar, never as
setuid files in the store. Without these settings Nix refuses the build with
"missing system features" instead of degrading.
Files are available read-only at `/.nix-inputs/files` and source at
`/.nix-inputs/context`. The author copies anything needed into writable cache
directories, runs the installers in offline mode, and removes temporary data.
Missing files fail the build. The harness never retries with network enabled.
The VM's serial output is included in the build log on success and failure.
Use `set -eu` and separate required build/test commands onto individual lines.
Shell `set -e` does not abort on an intermediate failure in an `&&` list; a later
successful command can otherwise mask a failed test. Check the script's exit
behavior as well as the final image.

For Debian-family images, explicitly list every required `.deb` and use local
package installation. With apt's `--no-download` option, first copy those files
into its writable archive cache; older apt versions fail when local files only
exist on the input mount. For Alpine, list every required `.apk`, retain signature
verification and use the installer's no-network mode. Package scripts may need
additional files or services; the author must handle those requirements explicitly.
For language dependencies, populate the cache layout expected by the pinned
package-manager version, then invoke its offline build mode. The generic input
format works even when no dedicated package-manager adapter exists.

The output is an OCI layout containing the original base layers and a new layer.
New layer entry timestamps are normalized to `sourceDateEpoch`; base layers are
unchanged. Authors must still handle timestamps embedded inside file contents.
The read-only input mount is excluded from that layer. `buildEnv` is not added to
the runtime configuration. Commands can deliberately copy files or environment
values into the image, so authors must still inspect their output. All Nix inputs
are store-visible: do not put secrets in scripts, input files or `buildEnv`.

Runtime `config` merges into the inherited image configuration. Set `Cmd`,
`Entrypoint`, `User`, `WorkingDir`, `Env` and other fields explicitly when the
environment changes them. Fields such as `Env` replace the entire inherited field.
Build commands always run as root; `config.User` selects the runtime user.

This interface currently supports linux/amd64 on an x86_64 Linux builder. The
base must contain `/bin/sh`. Shell-less images can instead be assembled with
`mkImage`. This is not a Dockerfile interpreter and does not support Windows VMs.
Offline execution establishes dependency completeness for the commands actually
run; it does not establish application correctness or deterministic output.
Qualify the complete Compose topology, verifier and vulnerable/fixed controls,
and check rebuild identity before publishing an environment.

Native installers can generate random or time-dependent state. Check the actual
layer contents: Python bytecode may need a fixed hash seed, local Git mirrors
may retain temporary clone paths, and machine IDs or passwords belong at runtime.
The author handles these explicitly in the recipe; the harness does not discover
or rewrite them.

Do not preserve store-file ownership when copying inputs into the image: store
owners can differ between a daemon build and a restored local store. With GNU
coreutils, use `cp -a --no-preserve=ownership`, then set the intended permissions
and runtime owner explicitly. Store files are also read-only; that is not
necessarily the permission the running service needs.

## Continue a native stage

Pass a previous `mkLinuxImage` result directly as another stage's `base`. The
second offline VM starts from that complete filesystem, including its package
database and installed build tools, without re-running the first stage's script.
It inherits the complete runtime image configuration; only explicitly supplied
`config` fields replace inherited fields. Build scripts still execute as root,
even when the inherited runtime `User` is non-root, and inherit the prior stage's
`Env` and `WorkingDir`.

The output keeps prior layer bytes, descriptors, diff IDs and history, then adds
one new filesystem-difference layer. Nix retains the existing layer store objects
through references instead of copying or flattening them. Whiteouts in earlier
stages remain effective when the next VM reconstructs the filesystem. Numeric
file ownership, permissions and native artifact exports are preserved.

Native stages accept skopeo-directory bases from `fetchLinuxBase` and the
single-image OCI layout produced by `mkLinuxImage`. Ambiguous multi-image indexes
are rejected; no platform/tag selection or registry access occurs during chaining.
Use `exportPaths` when the final image should receive only selected artifacts
rather than inherit the build stage's complete filesystem and tools.

## Explicit native artifacts between stages

Set `exportPaths` to an explicit list of absolute files/directories in the
completed stage filesystem. When nonempty, the resulting image derivation also
exposes `artifacts`, a standalone tar file derivation. For instance, pass
`native.artifacts` as a `native.tar` entry in the final stage's `inputs` directory
and extract it in the authored final-stage script:

```sh
tar -xpf /.nix-inputs/files/native.tar -C /
```

The archive uses root-relative paths and preserves numeric ownership, modes,
hardlinks and symbolic link targets. Member timestamps use `sourceDateEpoch`.
It includes only selected paths and their directory contents; no implicit source
stage base, compiler, configuration or build-input directory transfers to the
final stage. The archive itself is not appended to the image. The final stage
can use a different pinned base; the author must ensure ABI compatibility.

Paths must be normalized absolute paths, cannot name the filesystem root or
build mounts (`/dev`, `/proc`, `/sys`, `/.nix-inputs`), and cannot traverse a
symlink ancestor. Export a canonical path if an ancestor is a symlink. A selected
symlink is preserved without following it: select its target separately when
needed. Devices, sockets and FIFOs are rejected. Directories are recursive;
choose narrow paths to avoid exporting unrelated build files.

An omitted or empty `exportPaths` leaves the original image API unchanged and
provides no `artifacts` attribute. A missing selected file fails the build.

## CPU allocation

Set `cores = 2;` for a native build that needs two virtual CPUs. The author
still chooses the compiler's parallelism (for example `make -j2`); the helper
does not rewrite build commands. CPU allocation is per VM, so limit concurrent
Nix builds separately with `--max-jobs` and account for their combined CPU and
memory demand. Nix `--cores` does not override this explicit VM setting.

This disables vmTools automatic CPU selection, so even the default changes
existing native build derivation identities once; subsequent builds use a stable,
explicit allocation.
