# nix2rle

The [nix2rle](https://github.com/Incalmo/nix2rle) harness builds reproducible learning environments.
Declare packages, containers, and service topology in Nix. Source packages carry
their pins and patches; binary packages carry their download hashes. The harness
builds without network access and generates runtime Compose from the declaration.

This repository contains the harness only. Environment source, dependency locks,
Compose files, graders and solutions belong in separate customer-owned projects.
It contains no bundled environments, private certificates, cache credentials or
cloud infrastructure.

## Requirements

Linux x86_64, Nix with sandboxing enabled, and the `nix-command` and `flakes`
experimental features. Use `sandbox = true` and `sandbox-fallback = false`
in the builder's Nix configuration. Docker is needed only to load or run images;
the loader requires Docker's containerd image store. The native distro builder uses a QEMU VM.

Enter the pinned tooling environment with `nix-shell`. Other architectures are
not currently validated; the supplied egress image is pinned to linux/amd64.

## Library interface

Import `default.nix` with `{ system = "x86_64-linux"; }`, or supply your own
pinned `pkgs`. Flake consumers can call `lib.mkHarness` with the same arguments.
Pin this repository to an immutable revision when distributing an environment.

| Attribute | Purpose |
| --- | --- |
| `mkEnvironment` | Declare containers, services, networks and graders once; generate mappings and Compose |
| `mkContainer` | Declare a pinned base, packages, files and runtime settings |
| `pythonVerifier` | A grader container with the task's tests on a minimal Python userland |
| `mkSourcePackage` | Build explicit source and patches offline |
| `mkBinaryPackage` | Install pinned binary files or archives |
| `dependencies` | Hash-pinned fetchers (`fetchLockedInputs { lock; prefixes ? null; }`), package-manager cache layouts and offline application builds |
| `mkImage` | Build a nix2container image from packages, application outputs and OCI configuration |
| `requireTaskArchive` | Reuse an explicitly supplied original task archive by SHA-256 in the Nix store |
| `fetchLinuxBase` | Fetch a base image pinned by manifest digest and Nix content hash |
| `mkLinuxImage` | Run an authored script inside that distro in a VM without a network device |
| `nix2container` | Pinned upstream image and base-image operations |
| `egressImage` | Harbor-compatible runtime egress sidecar |
| `harbor` | Pinned Harbor CLI used by the runtime adapter |
| `pkgs` | The package set used by the harness |

Start with [declarative environments](docs/declarative-environments.md) and
[containers and packages](docs/containers.md). Lower-level image helpers remain
available for native distro installers and specialized builds.

See [agent authoring](docs/agent-authoring.md): complete native locks where available,
one explicit file for other inputs, and an offline build. The harness does not discover dependencies.

See [dependency adapters](docs/dependency-adapters.md) for optional helpers, lock formats,
supported package managers and limitations.
See [pinned images](docs/pinned-images.md) for an exact upstream image without added layers.
See [Linux images](docs/linux-images.md) to preserve a distro's filesystem and installers
without translating a Dockerfile.
See [authoring tools](docs/authoring-tools.md) to pin a registry base image
(`nix2rle-pin-base`) and to lock the exact package files of apt or opkg installs
(`nix2rle-lock-packages`).
See [pinned-file serving](docs/pinned-files.md) when an offline package manager
needs HTTP URLs for explicitly mapped local inputs. Run
`incalmo-pinned-files --map map.json -- your-command` with immutable pinned files;
native builds explicitly mount the portable script and inputs read-only and use
their own Python. Choose a fixed port when authored metadata or outputs embed URLs.

`mkImage` merges package files and the supplied `root` trees before constructing
layers. Later root trees override earlier files. Data files retain their
non-executable mode; `dirs` permissions apply to the named directories only.

## Running an environment

An environment is a directory whose [declarative](docs/declarative-environments.md)
`default.nix` returns `mkEnvironment` with named containers, services and graders,
beside its Harbor `task/`. The harness generates the runtime Compose
configuration; no manifest or source Compose file is authored.

The `nix2rle` command (on the path inside `nix-shell`) takes that directory.
Run from this checkout, replacing the absolute paths with your own:

```sh
nix-shell --run 'nix2rle /path/to/environment validate'
nix-shell --run 'nix2rle /path/to/environment fetch'
nix-shell --run 'nix2rle /path/to/environment check'
nix-shell --run 'nix2rle /path/to/environment load'
nix-shell --run 'nix2rle /path/to/environment compose'
nix-shell --run 'nix2rle /path/to/environment run --agent nop'
```

Generated state defaults to the environment's sibling `build/<directory-name>/`.
Use the same `--workspace /path/to/state` on every command to override it.
Commands lock that workspace until they finish; parallel builds need separate
workspaces. `validate` checks authored mappings without building or creating state.
`validate-contract` additionally rejects authored Dockerfiles/Compose files and evaluates every
image and application build output. It reports neither a build nor qualification
as passed; use the actual build and runtime checks for those claims.
Source files are not rewritten. `run` reports actual trial rewards as JSON and
fails on missing, incomplete or errored trials; a zero process exit is not a
qualification decision. See [trial results](docs/trial-results.md).
`check` rebuilds retained derivations and checks
output identity; fully pinned inputs alone do not guarantee deterministic outputs.

`compose` requires images already loaded on the current Docker daemon. It prints
JSON containing the main runtime Compose path, separate grader Compose paths,
and argument arrays for starting/stopping each project. Run the returned `up`
command to start the complete runtime with `--no-build --pull never`. Generated
services use digest references and `pull_policy: never`; missing local images or
stale recipe/image mappings fail instead of pulling. Compose files live under
`<workspace>/compose/` and refer to preserved assets under `<workspace>/runtime/`.
Keep that workspace in place while containers use its bind mounts.

The generated topology uses the pinned Harbor overlays, including required egress
routing, authored networks, environment variables and resource limits. Rendering
runs `docker compose config`; it does not start containers or probe the host with
a downloaded image. A host missing required egress kernel capabilities fails
when starting the sidecar. Compose starts each environment at its baseline
network policy. It does not execute solutions, grading, artifact transfer or
phase-specific policy changes. Use the returned `grading.oracle` or `grading.nop`
command for a Harbor trial; merely starting a grader container does not grade
the running Compose project. Harbor creates its own trial environments.

`publish --repository REPO --build-id ID --source-digest DIGEST` builds every
image and pushes it to `REPO/<container>` with skopeo, without a Docker daemon.
It prints a `BuildManifest` carrying the image references and the runtime
declaration; that manifest alone regenerates the Harbor task from the source
tree, with no Nix. Hosted builders use it; see [hosted builds](docs/hosted-builds.md).

`export --destination /new/bundle` copies the supplied environment and harness
into a relocatable directory. Run it with
`nix-shell /new/bundle/shell.nix --run 'nix2rle /new/bundle/environment ...'`. Exports include the supplied source tree: review it
for secrets before distribution. They do not include this checkout's history,
tests or internal development state.

For large original tasks, use [pinned task-source materialization](docs/task-sources.md)
before validating the runtime manifest. Keep the descriptor in recipe history
and supply the original archive through the Nix store or local distribution.

## Binary cache

Dependencies and build outputs are ordinary Nix store objects. Use any standard
Nix binary cache with its public verification key and appropriate read access.
No particular cache endpoint, cloud account or signing key is embedded here.
The `fetch` receipt records the store paths and derivations to retain; publishing
that closure to a cache is a separate operation. Customers need cache read access,
never the publisher's private signing key.

## Repository layout

| Path | Contents |
| --- | --- |
| `default.nix`, `flake.nix`, `shell.nix` | Library entry points and the pinned tooling shell |
| `lib/` | Nix library: `environment.nix`, `container.nix`, `packages.nix` |
| `lib/images/` | Image builders: nix2container, native distro VM, pinned images, egress sidecar |
| `lib/adapters/` | Hash-pinned fetchers and package-manager cache layouts |
| `lib/pins/` | Pinned nixpkgs, nix2container and Harbor |
| `src/nix2rle/` | Python package and `nix2rle` command |
| `src/nix2rle/environment/` | Declaration loading, environment contract, workspace locking |
| `src/nix2rle/outputs/` | Offline retention and rebuild checks, image load and registry push |
| `src/nix2rle/runtime/` | Nix-free task generation and image binding (vendored by hosts) |
| `src/nix2rle/harbor/` | Harbor environment adapter, local Compose, trial verdicts |
| `src/nix2rle/authoring/` | Network-using authoring tools: base pins, apt locks, task sources |
| `builders/` | Standard-library scripts run inside Nix builds |
| `tests/` | Tests by area; `tests/support/` holds fixtures and extra shells |

## Development

```sh
nix-shell --run 'python -m unittest discover -s tests -t . -p "test_*.py" -v'
nix-shell --run 'ruff check src builders tests'
nix-shell --run 'bash tests/support/check-cache-offline-fixture.sh'
nix-shell --run 'RUN_LINUX_VM_TESTS=1 python -m unittest discover -s tests -t . -p "test_linux*.py" -v'
```

Tests create disposable synthetic inputs at runtime; no environment is bundled.
The isolated-store check restores signed cache objects into a fresh store and
rebuilds with an outer network namespace. It requires Linux user namespaces.
It fails if any rebuilt output differs from the original output's NAR hash.

The harness is MIT licensed. Vendored Harbor sidecar files retain their Apache
2.0 license and provenance; see [NOTICE](NOTICE).
