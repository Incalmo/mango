# Haskell

**Package manager:** Cabal · **Project files:** `app.cabal`, `src/`

=== "Dockerfile"

    ```dockerfile
    FROM haskell:9.8-slim
    WORKDIR /app
    COPY . .
    RUN cabal update && cabal build
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

        # FROM haskell:9.8-slim
        base = h.fetchLinuxBase {
          image = "docker.io/library/haskell";
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
    
        # RUN cabal update && cabal build, with no network
        buildScript = ''
          set -eu
          in=/.nix-inputs/files
          cp -a /.nix-inputs/context /app
          cd /app
          $in/mango-proxy replay $in/record/app -- sh -c 'cabal update && cabal build'
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
nix-shell --run 'mango-pin-base docker.io/library/haskell:9.8-slim --nix'

# 2. Record what the build downloads, then verify it replays offline
nix-shell --run "mango-lock-record \
  --lock $ENV/inputs.lock.json --prefix record/app \
  --base docker.io/library/haskell@sha256:<digest> \
  --context $ENV/app --workdir /app \
  -- 'cabal update && cabal build'"
```

Commit `default.nix`, `inputs.lock.json` and the `inputs.lock.d/` directory.

## Build it

```sh
./bin/mango load $ENV
```

!!! note "Cabal"
    The Hackage index is large and changes constantly, so it is pinned by URL rather than committed. Keep the environment in your [binary cache](../remote-cache.md) to rebuild it later.

See [record and replay](../record-replay.md) for what is locked and its limits.
