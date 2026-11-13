# Java

**Package manager:** Maven · **Project files:** `pom.xml`, `src/`

=== "Dockerfile"

    ```dockerfile
    FROM maven:3.9-eclipse-temurin-21
    WORKDIR /app
    COPY . .
    RUN mvn -B package
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

        # FROM maven:3.9-eclipse-temurin-21
        base = h.fetchLinuxBase {
          image = "docker.io/library/maven";
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
    
        # RUN mvn -B package, with no network
        buildScript = ''
          set -eu
          in=/.nix-inputs/files
          cp -a /.nix-inputs/context /app
          cd /app
          $in/mango-proxy replay $in/record/app -- mvn -B package
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
nix-shell --run 'mango-pin-base docker.io/library/maven:3.9-eclipse-temurin-21 --nix'

# 2. Record what the build downloads, then verify it replays offline
nix-shell --run "mango-lock-record \
  --lock $ENV/inputs.lock.json --prefix record/app \
  --base docker.io/library/maven@sha256:<digest> \
  --context $ENV/app --workdir /app \
  -- mvn -B package"
```

Commit `default.nix`, `inputs.lock.json` and the `inputs.lock.d/` directory.

## Build it

```sh
./bin/mango load $ENV
```

!!! note "Maven"
    Maven has no lockfile: plugins, parent POMs and transitive artifacts are all resolved at build time. Recording captures exactly what the build asked for. This recipe has also been built end to end as a Mango image.

See [record and replay](../record-replay.md) for what is locked and its limits.
