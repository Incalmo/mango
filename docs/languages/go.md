# Go

**Package manager:** Go modules · **Project files:** `go.mod`, `go.sum`, `*.go`

=== "Dockerfile"

    ```dockerfile
    FROM golang:1.23
    WORKDIR /app
    COPY . .
    RUN go build -o /usr/local/bin/app ./...
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

        # FROM golang:1.23
        base = h.fetchLinuxBase {
          image = "docker.io/library/golang";
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
    
        # RUN go build -o /usr/local/bin/app ./..., with no network
        buildScript = ''
          set -eu
          in=/.nix-inputs/files
          cp -a /.nix-inputs/context /app
          cd /app
          $in/mango-proxy replay $in/record/app -- go build -o /usr/local/bin/app ./...
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
nix-shell --run 'mango-pin-base docker.io/library/golang:1.23 --nix'

# 2. Record what the build downloads, then verify it replays offline
nix-shell --run "mango-lock-record \
  --lock $ENV/inputs.lock.json --prefix record/app \
  --base docker.io/library/golang@sha256:<digest> \
  --context $ENV/app --workdir /app \
  -- go build -o /usr/local/bin/app ./..."
```

Commit `default.nix`, `inputs.lock.json` and the `inputs.lock.d/` directory.

## Build it

```sh
./bin/mango load $ENV
```

!!! note "Go modules"
    With a complete `go.sum`, [`mango-lock-go`](../go-module-locks.md) pins the module graph without running a build and checks every module against its `h1:` sum.

See [record and replay](../record-replay.md) for what is locked and its limits.
