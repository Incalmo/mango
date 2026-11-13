# C#

**Package manager:** NuGet · **Project files:** `app.csproj`, `Program.cs`

=== "Dockerfile"

    ```dockerfile
    FROM mcr.microsoft.com/dotnet/sdk:8.0
    WORKDIR /app
    ENV DOTNET_CLI_TELEMETRY_OPTOUT=1
    ENV DOTNET_NOLOGO=1
    COPY . .
    RUN dotnet publish -c Release -o out
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

        # FROM mcr.microsoft.com/dotnet/sdk:8.0
        base = h.fetchLinuxBase {
          image = "mcr.microsoft.com/dotnet/sdk";
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
        buildEnv = { DOTNET_CLI_TELEMETRY_OPTOUT = "1"; DOTNET_NOLOGO = "1"; };

        # RUN dotnet publish -c Release -o out, with no network
        buildScript = ''
          set -eu
          in=/.nix-inputs/files
          cp -a /.nix-inputs/context /app
          cd /app
          $in/mango-proxy replay $in/record/app -- dotnet publish -c Release -o out
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
nix-shell --run 'mango-pin-base mcr.microsoft.com/dotnet/sdk:8.0 --nix'

# 2. Record what the build downloads, then verify it replays offline
nix-shell --run "mango-lock-record \
  --lock $ENV/inputs.lock.json --prefix record/app \
  --base mcr.microsoft.com/dotnet/sdk@sha256:<digest> \
  --env DOTNET_CLI_TELEMETRY_OPTOUT=1 \
  --env DOTNET_NOLOGO=1 \
  --context $ENV/app --workdir /app \
  -- dotnet publish -c Release -o out"
```

Commit `default.nix`, `inputs.lock.json` and the `inputs.lock.d/` directory.

## Build it

```sh
./bin/mango load $ENV
```

!!! note "NuGet"
    Turn telemetry off so the build makes only the requests it needs.

See [record and replay](../record-replay.md) for what is locked and its limits.
