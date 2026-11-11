# Agent authored build inputs

The agent selects the build inputs. When a package manager already supplies a
complete lockfile with immutable versions and content checksums, the harness can
read that lock directly. Otherwise, the agent lists and hashes the files explicitly.
There is no task scanner or package-manager detector in this workflow.

## Two explicit input paths

| Input | Mechanism |
| --- | --- |
| Complete native lock | Recipe explicitly calls its lock reader, such as `fetchCargoCache { lockFile = ./Cargo.lock; }` |
| Incomplete declaration or extra download | Agent records exact files in `inputs.lock.json` and uses `fetchLockedInputs` |

Cargo registry packages already have checksums in `Cargo.lock`. Cargo Git inputs
also require explicit `outputHashes`; a commit alone is not the content hash
required by this reader. Use `cargoSetupHook` with the returned `cargoDeps`, then
build with Cargo's frozen/offline options.

A Maven POM can specify exact versions but normally does not lock every plugin,
parent POM, transitive artifact and downloaded byte. The agent must resolve and
hash that complete set explicitly. Do not infer a complete lock from a filename
or from top-level version declarations.

Both paths produce immutable Nix store inputs and use the same offline build
validation. Native lock readers select only what the supplied lock records;
they must not update the lock or silently resolve newer versions.

A native lock covers its package graph, not every action performed by those
packages. A build plugin can download a separate native generator or compiler.
Pin that executable too, along with its runtime libraries, and point the plugin
at the prepared executable. Test generation inside the offline sandbox and assert
that the expected files exist: wrappers can mistakenly report success when a
native child process terminates on a signal.

## One file per environment

The environment owns `inputs.lock.json` for explicitly pinned inputs. All
services, graders and helpers share it; complete native locks remain separate
and are selected explicitly by the recipe. Each listed file has a destination
path, URL and SHA-256 of its
bytes. Paths are relative to the fetched input directory; prefixes can separate
services or cache layouts. Package names and version constraints are not inputs:
the agent must resolve them into actual files first.

The format is a JSON object with `schema_version: 1` and an `artifacts` array.
Every artifact has these required fields:

| Field | Meaning |
| --- | --- |
| `path` | Unique relative file path in the assembled input directory |
| `urls` | Nonempty list of URLs providing identical bytes |
| `sha256` | SHA-256 of the downloaded bytes, in a Nix-supported format |
| `sha512` | Instead of `sha256`, when upstream publishes only SHA-512 (exactly one of the two) |

An empty array is valid for an environment with no separately fetched files.
Credentials and expiring authenticated URLs must not appear in this file.

## The wrapper

An environment recipe imports the harness and calls:

```nix
inputs = harness.dependencies.fetchLockedInputs {
  lock = ./inputs.lock.json;
};
```

Here `harness` is the imported library attribute set. The result is a directory
in `/nix/store` linking each `path` to its verified download. Downloads and the
assembled directory are normal Nix store objects that can be published to a
standard binary cache. The wrapper rejects invalid paths, duplicate destinations,
missing hashes and hash mismatches. It does not interpret package formats, unpack
archives, run installers or select versions.

To obtain a writable copy, add `harness.dependencies.prepareCache` to the build's
`nativeBuildInputs` and run:

```sh
prepare-dependency-cache "$INPUTS" "$TMPDIR/inputs"
```

`INPUTS` is the store directory passed into the build by its Nix recipe. The
agent writes explicit preparation commands to unpack files and populate the
application's local package caches. The store is never modified.

## The agent's responsibility

1. Resolve every source, package, plugin, compiler download and build asset needed
   by every service, grader and helper. Use package managers during this authoring
   step if useful; retain complete native locks or record the resulting files
   explicitly. Supplement native locks where they omit downloads.
2. Add those files and verified hashes to the shared input file. Include transitive
   dependencies, native build dependencies, repository metadata and lifecycle-script
   downloads where the build needs them.
3. Write preparation and build commands. Specify which archives to unpack, which
   cache paths to populate and which offline flags to use. Retain the required
   package-manager versions and ABI/platform choices in the recipe. Nixpkgs tools
   and base-image inputs must also be pinned and retained in the release closure.
4. Build in a network-disabled sandbox using `mkOfflineApplication` and Nix
   sandboxing, or `mkLinuxImage` for native distro installers in an offline VM
   (see [Linux images](linux-images.md)). Fix a missing input by updating the
   file and preparation commands;
   do not enable network access as a fallback.
5. Check the resulting services, topology, grader and vulnerable/fixed controls.
   A successful compilation is not sufficient to qualify the environment.

The mechanism is deliberately small. A cache for an unfamiliar package manager
can be prepared by the agent's script without adding that ecosystem to the
harness. Existing ecosystem helpers are optional conveniences, not prerequisites.

## Rebuilds

Publish the complete retained closure after qualification, including pinned
sources, tools and base images. A customer restores those store objects and runs
the recorded offline build. A missing cache object or changed download fails;
there is no automatic re-resolution. Credentials and signing keys stay outside
application builds.
