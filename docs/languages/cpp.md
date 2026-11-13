# C++

**Package manager:** apt (system libraries) · **Project files:** `Makefile`, `src/*.cpp`

C++ projects usually take their dependencies from the distribution. Mango
pins those packages against a dated archive snapshot.

=== "Dockerfile"

    ```dockerfile
    FROM debian:bookworm-slim
    WORKDIR /app
    RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake libfmt-dev
    COPY . .
    RUN make
    ```

    `apt-get update` fetches today's package index, so the compiler and
    libraries change whenever the archive does.

=== "Mango"

    ```nix
    { harness }:
    let
      h = import harness { };

      main = h.mkLinuxImage {
        name = "app";

        # FROM debian:bookworm-slim
        base = h.fetchLinuxBase {
          image = "docker.io/library/debian";
          digest = "sha256:<digest>";   # printed by mango-pin-base
          hash = "sha256-<hash>";
        };

        # The exact .deb files chosen at lock time.
        inputs = h.dependencies.fetchLockedInputs { lock = ./inputs.lock.json; };

        # COPY . .
        context = ./app;

        buildScript = ''
          set -eu
          in=/.nix-inputs/files

          # RUN apt-get update && apt-get install ...
          dpkg -i $in/apt/<key>/0/*.deb   # <key> is printed by mango-lock-packages

          # RUN make
          cp -a /.nix-inputs/context /app
          cd /app
          make
        '';

        # WORKDIR /app
        config.WorkingDir = "/app";
      };
    in
    h.mkEnvironment {
      name = "app";
      task = ./task;
      containers = { inherit main; egress = h.egressImage; };
      services.main.container = "main";
    }
    ```

    The packages are installed from pinned files with no network.

## Lock it

```sh
# 1. Pin the base image
nix-shell --run 'mango-pin-base docker.io/library/debian:bookworm-slim --nix'

# 2. Resolve the packages against a dated snapshot of the archive
nix-shell --run "mango-lock-packages --lock $ENV/inputs.lock.json --manager apt \
  --base docker.io/library/debian@sha256:<digest> \
  --source 'deb https://snapshot.debian.org/archive/debian/20261101T000000Z bookworm main' \
  --source 'deb https://snapshot.debian.org/archive/debian/20261101T000000Z bookworm-updates main' \
  --source 'deb https://snapshot.debian.org/archive/debian-security/20261101T000000Z bookworm-security main' \
  --stage 'build-essential cmake libfmt-dev'"
```

## Build it

```sh
./bin/mango load $ENV
```

!!! note "Conan and vcpkg"
    Package managers that download at build time are locked with [record and replay](../record-replay.md): record `conan install . --build=missing` the same way as the other languages. Conan's recipe downloads are verified; vcpkg has not been tested.

See [authoring tools](../authoring-tools.md#lock-distro-packages) for
Alpine (`apk`), multiple install stages and apt's recommendation policy.
