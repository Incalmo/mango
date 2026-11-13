# Scala

**Package manager:** sbt · **Project files:** `build.sbt`, `src/`

=== "Dockerfile"

    ```dockerfile
    FROM sbtscala/scala-sbt:eclipse-temurin-21.0.5_11_1.10.7_3.6.2
    WORKDIR /app
    COPY . .
    RUN sbt -batch package
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

        # FROM sbtscala/scala-sbt:eclipse-temurin-21.0.5_11_1.10.7_3.6.2
        base = h.fetchLinuxBase {
          image = "docker.io/sbtscala/scala-sbt";
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
    
        # RUN sbt -batch package, with no network
        buildScript = ''
          set -eu
          in=/.nix-inputs/files
          cp -a /.nix-inputs/context /app
          cd /app
          $in/mango-proxy replay $in/record/app -- sbt -batch package
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
nix-shell --run 'mango-pin-base docker.io/sbtscala/scala-sbt:eclipse-temurin-21.0.5_11_1.10.7_3.6.2 --nix'

# 2. Record what the build downloads, then verify it replays offline
nix-shell --run "mango-lock-record \
  --lock $ENV/inputs.lock.json --prefix record/app \
  --base docker.io/sbtscala/scala-sbt@sha256:<digest> \
  --context $ENV/app --workdir /app \
  -- sbt -batch package"
```

Commit `default.nix`, `inputs.lock.json` and the `inputs.lock.d/` directory.

## Build it

```sh
./bin/mango load $ENV
```

!!! note "sbt"
    sbt resolves the Scala compiler, its own plugins and your libraries through Coursier; all three are recorded.

See [record and replay](../record-replay.md) for what is locked and its limits.
