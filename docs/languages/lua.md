# Lua

**Package manager:** LuaRocks · **Project files:** `app-1.0-1.rockspec`

=== "Dockerfile"

    ```dockerfile
    FROM nickblah/lua:5.4-luarocks
    WORKDIR /app
    COPY . .
    RUN luarocks install --only-deps app-1.0-1.rockspec
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

        # FROM nickblah/lua:5.4-luarocks
        base = h.fetchLinuxBase {
          image = "docker.io/nickblah/lua";
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
    
        # RUN luarocks install --only-deps app-1.0-1.rockspec, with no network
        buildScript = ''
          set -eu
          in=/.nix-inputs/files
          cp -a /.nix-inputs/context /app
          cd /app
          $in/mango-proxy replay $in/record/app -- luarocks install --only-deps app-1.0-1.rockspec
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
nix-shell --run 'mango-pin-base docker.io/nickblah/lua:5.4-luarocks --nix'

# 2. Record what the build downloads, then verify it replays offline
nix-shell --run "mango-lock-record \
  --lock $ENV/inputs.lock.json --prefix record/app \
  --base docker.io/nickblah/lua@sha256:<digest> \
  --context $ENV/app --workdir /app \
  -- luarocks install --only-deps app-1.0-1.rockspec"
```

Commit `default.nix`, `inputs.lock.json` and the `inputs.lock.d/` directory.

## Build it

```sh
./bin/mango load $ENV
```

!!! note "LuaRocks"
    Rocks that compile C code need the compiler from the base image, exactly as in the Dockerfile.

See [record and replay](../record-replay.md) for what is locked and its limits.
