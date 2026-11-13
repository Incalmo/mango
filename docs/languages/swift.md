# Swift

**Package manager:** Swift Package Manager · **Project files:** `Package.swift`, `Package.resolved`, `Sources/`

=== "Dockerfile"

    ```dockerfile
    FROM swift:6.0
    WORKDIR /app
    COPY . .
    RUN swift build -c release
    ```

    Every build asks the registry what is current. Two builds of this file
    a month apart can install different dependencies.

=== "Mango"

    ```nix
    { harness }:
    let
      h = import harness { };

      main = h.mkLinuxImage {
        name = "app";

        # FROM swift:6.0
        base = h.fetchLinuxBase {
          image = "docker.io/library/swift";
          digest = "sha256:<digest>";   # printed by mango-pin-base
          hash = "sha256-<hash>";
        };

        # Every download of the build, recorded once and pinned by hash.
        inputs = h.dependencies.fetchLockedInputs {
          lock = ./inputs.lock.json;
          extra."mango-proxy" = h.replayProxy;
        };

        # COPY . .
        context = ./app;
    
        # RUN swift build -c release, with no network
        buildScript = ''
          set -eu
          in=/.nix-inputs/files
          cp -a /.nix-inputs/context /app
          cd /app
          $in/mango-proxy replay $in/record/app -- swift build -c release
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

    The build replays the recorded downloads to the same command. It has
    no network, so it installs the same bytes every time.

## Lock it

Run once, online, from the Mango checkout. `$ENV` is your environment
directory, holding the project under `app/`.

```sh
# 1. Pin the base image: prints image, digest and hash for default.nix
nix-shell --run 'mango-pin-base docker.io/library/swift:6.0 --nix'

# 2. Record what the build downloads, then verify it replays offline
nix-shell --run "mango-lock-record \
  --lock $ENV/inputs.lock.json --prefix record/app \
  --base docker.io/library/swift@sha256:<digest> \
  --context $ENV/app --workdir /app \
  -- swift build -c release"
```

Commit `default.nix`, `inputs.lock.json` and the `inputs.lock.d/` directory.

## Build it

```sh
./bin/mango load $ENV
```

!!! note "Swift Package Manager"
    SwiftPM clones dependencies with Git over HTTPS. Those exchanges are recorded like any other; because a clone has no URL to fetch from, its packs are committed beside the lock.

See [record and replay](../record-replay.md) for what is locked and its limits.
