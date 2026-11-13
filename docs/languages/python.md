# Python

**Package manager:** pip · **Project files:** `requirements.txt`

=== "Dockerfile"

    ```dockerfile
    FROM python:3.13-slim
    WORKDIR /app
    COPY . .
    RUN pip install --no-cache-dir -r requirements.txt
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

        # FROM python:3.13-slim
        base = h.fetchLinuxBase {
          image = "docker.io/library/python";
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
    
        # RUN pip install --no-cache-dir -r requirements.txt, with no network
        buildScript = ''
          set -eu
          in=/.nix-inputs/files
          cp -a /.nix-inputs/context /app
          cd /app
          $in/mango-proxy replay $in/record/app -- pip install --no-cache-dir -r requirements.txt
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
nix-shell --run 'mango-pin-base docker.io/library/python:3.13-slim --nix'

# 2. Record what the build downloads, then verify it replays offline
nix-shell --run "mango-lock-record \
  --lock $ENV/inputs.lock.json --prefix record/app \
  --base docker.io/library/python@sha256:<digest> \
  --context $ENV/app --workdir /app \
  -- pip install --no-cache-dir -r requirements.txt"
```

Commit `default.nix`, `inputs.lock.json` and the `inputs.lock.d/` directory.

## Build it

```sh
./bin/mango load $ENV
```

!!! note "pip"
    The same recipe works for **uv** and **Poetry**: record the command you run. If you only need wheels and have no build step, [`mango-lock-python`](../authoring-tools.md#lock-python-wheels-from-a-pip-report) pins them from a pip report without running anything.

See [record and replay](../record-replay.md) for what is locked and its limits.
