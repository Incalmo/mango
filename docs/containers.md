# Declarative containers and packages

`mkContainer` declares files, packages and runtime configuration. `mkSourcePackage`
and `mkBinaryPackage` produce ordinary Nix package derivations. None of these
functions reads a Dockerfile, discovers dependencies or resolves package names.
Use them with the pinned harness and expose the resulting images through
`mkEnvironment` or your explicit runtime manifest.

## Containers

`mkContainer` accepts:

| Field | Meaning |
| --- | --- |
| `name` | Image derivation name |
| `base` | `null` for scratch, `{ image; digest; hash; }` for a pinned registry image, or `{ ociLayout = nativeStage; }` for an authored OCI image output |
| `packages` | Explicit Nix derivations; defaults to `[]` |
| `files` | Absolute destination to `{ source = path; }`, `{ text = string; }`, or `{ symlink = target; }` |
| `directories` | List of `{ path; mode ? "0755"; uid ? 0; gid ? 0; }` |
| `environment` | Runtime environment strings by variable name |
| `command`, `entrypoint` | Argument lists; `null` inherits, `[]` explicitly clears |
| `workingDir`, `user` | Runtime strings; `null` inherits |
| `ports` | Integer or string port, optionally `/tcp` or `/udp`, in range 1–65535 |
| `roots` | Explicit prebuilt root trees as an escape hatch; defaults to `[]` |

Scratch defaults are root user, `/` working directory and `PATH=/bin`. With a
base, omitted command/entrypoint/user/working directory and other OCI config
fields are inherited. Authored environment variables override inherited values.
Declared container ports add OCI `ExposedPorts` metadata; they do not publish
host ports. Host publishing is configured separately in
`mkEnvironment.services.<service>.ports`, using the environment topology API.

Nix packages are linked at `/bin`, `/lib`, etc. in scratch images. With a distro
base they are placed at `/opt/nix-tools` and its `bin` is prepended to the base
PATH, keeping the distro's own filesystem intact. The author may override PATH.
This list accepts Nix packages, not distro package names: `.deb`/`.apk`
installation is not implicit. Native distro installers remain explicit offline
`mkLinuxImage` builds when required for ABI or service behavior.

Text files default to `0644`. A `mode` string on a text/source entry specifies the
exact destination mode in the final image, including read-only modes such as
`0444`. It applies only to that destination, not recursively to a copied tree.
Source directory trees retain their executable bits and symlinks; they are never
recursively flattened to `0644`. Nix store normalization removes write bits, and
the image assembly restores owner-write unless an explicit mode overrides it.
A symlink entry preserves the declared target and does not accept a mode.
Store-path references retain their needed Nix closure inside the image.

File destinations cannot traverse `.`/`..` or symlink ancestors. Declared files
overlay the supplied `roots`. Explicit file permissions are applied after generic
root permissions. Scratch images provide minimal root entries in `/etc/passwd`
and `/etc/group`; explicit `files` entries replace these defaults. Distro images
retain their existing accounts. Selecting another runtime user does not create
an account: declare the required account files explicitly.

Base `digest` is the registry manifest digest; `hash` is the Nix recursive hash
expected by the harness `fetchLinuxBase` implementation (the same descriptor
format). Obtain and retain that exact fetched content. Do not assume a hash from
nix2container `pullImage`, a registry manifest or another puller is interchangeable. No tags are resolved implicitly.

Use `base = { ociLayout = nativeStage; }` to wrap an explicit `mkLinuxImage`
stage with `mkContainer`. This retains the distro filesystem, installed packages
and ABI while runtime configuration and files remain declarative. The layout
must be a single linux/amd64 image; multi-image indexes are rejected. `ociLayout`
accepts an image derivation or an explicit Nix path, and cannot be mixed with
registry descriptor fields. No registry lookup or installer rerun is performed.
Base reading occurs during the image build; evaluating `mkEnvironment.declaration`
does not realize or force its container/native-stage outputs.

Both base forms preserve ordered base layer bytes and diff IDs plus the complete
runtime `config` object before explicit overrides. File/account overrides have
the same semantics as registry-backed containers: inherited users are resolved
against the resulting account files, and selecting `user` does not create one.
The final image is assembled by nix2container. Its top-level OCI creation/history
metadata is generated anew; original top-level author/history metadata is not
round-tripped. This matches existing registry-base behavior and is distinct from
`mkLinuxImage` chaining, which retains the prior native stage's history.

The result is an ordinary nix2container image derivation with `declaredRoot` and
`baseImage` passthru attributes; importing it does not start Docker or build a VM.

## Source packages

`mkSourcePackage { pname; version; src; patches ? []; build; install; ... }`
accepts an explicitly pinned source derivation/path and an ordered patch list.
`build` and `install` are shell commands for the application; the wrapper executes
the corresponding pre/post hooks. Normal derivation arguments are forwarded,
including toolchains, `nativeBuildInputs`, `buildInputs`, `cargoDeps`, environment
variables and `doInstallCheck`/`installCheckPhase`.

Source patches are applied once. The result's `.patchedSource` exposes that same
patched source for image files or a separate source view. Optional `patchFlags`
are passed to that patch step, for example `[ "-p1" "--no-backup-if-mismatch" ]`
to avoid incidental `.orig` files when a hunk applies at an offset. Omitting the
option preserves the underlying patch defaults. The author chooses
setup, hardening, arming and signal patch sets explicitly; no vulnerability
semantics are inferred. Compilation runs as an ordinary offline application
build. The wrapper does not invent install commands or application tests.

## Binary packages

For a pinned file use `mkBinaryPackage { pname; version; urls; sha256; files; }`.
The `files` map has relative output destinations such as `bin/tool`; entries
accept `mode` (default `0755`). Each selected output receives the pinned bytes.
For a tar or zip, set `format = "archive"` and give each entry a `source` path
relative to the archive root. Declare the complete intended layout. Archive path
traversal is rejected. The simple installer leaves binary bytes untouched rather
than applying automatic ELF or shebang transformations.

When an upstream binary distribution has a required installer or several pinned
components, use the explicit escape hatch:
`mkBinaryPackage { pname; version; inputs = lockedDirectory; install = commands; ... }`.
The shell sees `$inputs`; standard build inputs and fixup hooks can be supplied
explicitly (for example `autoPatchelfHook` and its runtime libraries). There is no
implicit download in the installer wrapper. These commands run in an ordinary
isolated derivation; all component files must already be pinned inputs.

List the actual source/native builds in qualification rebuild targets. Rebuilding
only the final image description is not proof that the application recompiles.

The native-base integration test runs a small offline VM build and loads its
wrapped image into Docker, checking inherited settings and explicit file/account
overrides. It requires the containerd image store used by the harness loader:

```sh
nix-shell --run 'RUN_LINUX_VM_TESTS=1 python -m unittest discover -s tests -t . -p test_container_native_base.py -v'
```

The test uses uniquely named local images and removes its own tag afterward.

When `nix2rle load` serializes a nix2container image, it restores the complete
declared runtime configuration after transport conversion, including Docker
health checks and stop signals. It recomputes the OCI config and manifest
digests and sizes, leaving serialized architecture, OS, ordered layers,
rootfs diff IDs and generated history unchanged. This applies to both pinned
registry bases and native OCI bases.

For native Debian/Ubuntu builds, `h.stageDebsScript` stages an explicitly supplied
set of pinned `.deb` files under apt's canonical archive names. Run it using the
base image's `sh` and `dpkg-deb`; it requires standard `cp`, `cmp`, `mv`, `sed`,
and `mktemp`. It does not resolve, download, or install packages.

```sh
sh /.nix-inputs/files/stage-debs.sh /var/cache/apt/archives \
  /.nix-inputs/files/debs/*.deb
apt-get --no-download --no-install-recommends -y install /var/cache/apt/archives/*.deb
```

Include `{ name = "stage-debs.sh"; path = h.stageDebsScript; }` in the native
stage's explicit inputs. Epochs become `%3a` and `~` is retained in filenames;
arbitrary download names are ignored. Conflicting bytes for one canonical name
fail before publishing the supplied set. Use a build-private destination with
one writer. Package selection, manual/automatic marks, installation order and
cleanup remain the environment author's responsibility.

Test with `nix-shell tests/support/deb-shell.nix --run 'python -m unittest discover -s tests -t . -p test_stage_debs.py -v'`.

For JVM builds that need deterministic JAR packaging, `h.jarNormalizer` exposes
an optional Python 3.8+ standard-library script:

```sh
python normalize-jar.py input.jar output.jar --epoch 315532800
```

Supply it as an explicit build input and run it after compilation. It sorts ZIP
entries, uses fixed compression, and preserves entry contents, comments and
permission attributes. Compiled `.class` timestamps are one ZIP clock tick
(two seconds) newer than other entries: Clojure's loader uses a strict comparison
when choosing ahead-of-time classes instead of recompiling source. Equal timestamps
can change application behavior even when every entry's contents are unchanged.

The script removes timestamp-only extra fields and regenerated ZIP64 metadata;
it rejects duplicate names, malformed extra fields and unsupported mixed Unix
metadata. Output replacement is atomic, including when input equals output.
This changes archive bytes and requires a new archive hash. Use the same pinned
Python/zlib toolchain for reproducibility and verify application startup afterward;
the helper does not repair nondeterministic entry contents or resolve dependencies.
