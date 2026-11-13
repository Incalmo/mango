# Mango 🥭

Mango is a declarative DSL for RL environments, and a drop-in replacement for
Dockerfiles. Declare an environment once: Mango freezes every input it
depends on and builds it offline from then on. Environments run as ordinary
[Harbor](https://github.com/harbor-framework/harbor) tasks.

<div class="grid cards" markdown>

-   **Reproducible**

    ---

    Base images, distro packages and language dependencies are pinned by
    content hash. An environment built next year contains the same files as
    the one built today.

-   **Scalable**

    ---

    Pinned inputs are fetched once and served from your own binary cache.
    Building 1,000 environments does not send 1,000 builds to Docker Hub,
    the Ubuntu archive, Maven Central or npm.

-   **General**

    ---

    Any Linux distro, and any package manager that downloads over HTTP(S).
    25 package managers across 20 languages are verified.

</div>

## From a Dockerfile to Mango

=== "Dockerfile"

    ```dockerfile
    FROM maven:3.9-eclipse-temurin-21
    WORKDIR /app
    COPY . .
    RUN mvn -B package
    ```

=== "Mango"

    ```nix
    main = h.mkLinuxImage {
      name = "app";
      base = h.fetchLinuxBase { image = "docker.io/library/maven"; digest = "sha256:..."; hash = "sha256-..."; };
      inputs = h.dependencies.fetchLockedInputs {
        lock = ./inputs.lock.json;
        extra."mango-proxy" = h.replayProxy;
      };
      context = ./app;
      buildScript = ''
        cp -a /.nix-inputs/context /app && cd /app
        /.nix-inputs/files/mango-proxy replay /.nix-inputs/files/record/app -- mvn -B package
      '';
      config.WorkingDir = "/app";
    };
    ```

The Dockerfile downloads whatever Maven Central serves that day. The Mango
build replays downloads that were recorded once and pinned by hash, with no
network.

## Where to go next

<div class="grid cards" markdown>

-   **[Quick start](quickstart.md)**

    Convert a real Terminal-Bench task, step by step.

-   **[Languages](languages/index.md)**

    A Dockerfile and its Mango equivalent for 20 languages.

-   **[How it works](how-it-works.md)**

    Author online once, build offline forever.

-   **[Record and replay](record-replay.md)**

    Lock any package manager without per-language support.

</div>
