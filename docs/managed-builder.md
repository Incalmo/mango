# Managed local builds

`mango load /path/to/environment` builds in a Docker-managed Nix store by
default. The host needs Python 3.11+ for the checkout launcher and local,
rootful Docker on Linux/amd64. It does not need Nix or Harbor installed.
The Python package currently requires Python 3.13+. Image loading requires
Docker's containerd image store to retain OCI digests.

The build container currently requires **privileged Docker containers** to
keep Nix sandboxing enabled. Rootless Docker and remote Docker engines are
not supported by this backend. Organizations that prohibit privileged
containers need a separately provisioned builder; this backend does not
bypass that policy. Run trusted environment sources only.

```sh
./bin/mango load /path/to/environment
./bin/mango compose /path/to/environment
./bin/mango run /path/to/environment --agent oracle
```

`mango.toml` names the [remote caches](remote-cache.md), and the user's
credentials file says how to authenticate to them. Mango authenticates on the
host, starts a temporary read-only proxy per cache, and passes its URL and
the signing key to the container's own Nix process.
Customer tokens and Google credentials are not mounted into the builder.
Host Nix configuration is neither read nor changed. The builder enforces
`sandbox = true`, `sandbox-fallback = false`, and signature verification
uses the configured public keys. An uncached path is downloaded or built.

The environment's existing harness pin remains in `mango.lock`. Mango adds
a builder pin when an older lock lacks it:

```json
{
  "builder": {
    "image": "nixos/nix@sha256:c39fc2f168d84ad70cb07102dd82cc73dfb0915bd609308d1d269030bbfa7a26",
    "platform": "linux/amd64"
  }
}
```

The image supplies Nix; the locked harness supplies the build libraries,
Python dependencies, and Harbor. Nix bootstraps these tools into a persistent
named volume on the first invocation. The first cold build can therefore be
substantially slower than subsequent builds. Mango never resolves a moving
image tag. Changing a builder pin is an explicit lock edit.

Each user and builder digest gets a volume named
`mango-nix-<uid>-<digest-prefix>`, shared across that user's environments.
The volume contains `/nix`, the Nix database, and build receipts. It is a
single-user store, with no host daemon or shared host `nixbld` UIDs.
Build concurrency is bounded to two jobs and four cores per job.
The source is mounted read-only, and the build container has no Docker socket.
VM image builds use KVM when available; the portable VM implementation falls
back to QEMU TCG when KVM cannot initialize. Old locked harnesses retain their
own VM behavior; new default locks select a harness with this fallback.

After building, Mango exports OCI archives and generates the Harbor task
using the pinned harness. Exported files belong to the invoking host user.
The host imports each archive and verifies its image reference before
replacing the workspace's runtime task. A failed build or image import keeps
the previous runtime intact. Temporary archives and containers are removed;
the named Nix volume survives.

Generated tasks are at `../mango-build/<environment-name>/runtime` by default;
`--workspace /path` chooses a different workspace. Paths must be outside
`/nix` and cannot contain commas (Docker mount syntax).

`compose` and `run` use a separate controller container with the pinned
Harbor dependencies and Docker client. It runs as the host UID, drops all
Linux capabilities, mounts the Nix volume read-only, and accesses the host
Docker socket to manage trial containers. It shares host paths so Docker
bind mounts resolve correctly. Common OpenAI, Anthropic, and Google API-key
variables are forwarded to this controller only.

## Cleaning up

Builds keep their results so later builds are fast. To remove what one
environment left behind:

```sh
./bin/mango clean /path/to/environment
```

This removes, for that environment and workspace only:

- the generated workspace (`../mango-build/<environment-name>` or `--workspace`),
  including its Nix garbage-collector roots;
- the `mango.local/*` Docker images it loaded;
- Nix store paths that nothing else keeps alive once those roots are gone, in
  both the host store and the managed builder's volume.

Images also recorded by a sibling workspace, images used by a running
container, and store paths needed by another environment or root are kept.
Trial results under `jobs/` are kept unless `--results` is given. The shared
`mango-nix-*` volume itself is never removed; delete it with
`docker volume rm` to discard the whole managed store. `clean` prints a JSON
summary of what it removed and kept. It works for both builders and does not
need `--builder`.

Supported managed actions are `load`, `prepare`, `compose`, `run`, `build`,
`fetch`, `check`, `validate`, `declaration`, `validate-contract`, and `lock`.
Publishing and offline bundle operations remain available through the host
backend. `check` uses receipts retained in the managed volume.

For developers who deliberately want their existing Nix installation:

```sh
mango load /path/to/environment --builder host
```

Legacy `mango ENV ACTION` invocations retain their host-Nix behavior.

## Verification

The unit suite checks backend selection, lock migration, credential handling,
container mounts, failed-build cleanup, OCI archive contents, and preservation
of an existing runtime on import failure. To exercise real Docker sandboxing
and authenticated signed-cache substitution (including wrong-key rejection):

```sh
nix-shell --run 'MANGO_TEST_MANAGED_BUILDER=1 python -m unittest tests.environment.test_builder -v'
```

This opt-in test creates disposable containers and a temporary local cache;
it requires Docker and the development Nix shell, not cloud credentials.

Cache reuse does not make a recipe's uncached rebuild byte-reproducible.
Package-manager logs and `ldconfig` auxiliary caches can contain varying
contents even when application files match. Use `check` to audit recipes;
the managed builder does not silently remove authored image contents.
