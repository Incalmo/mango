{ harness }:
let
  h = import harness { };

  main = h.mkLinuxImage {
    name = "nginx-request-logging";

    # FROM python:3.13-slim-bookworm
    base = h.fetchLinuxBase {
      image = "docker.io/library/python";
      digest = "sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72";
      hash = "sha256-3bGHO8WdxydBabREP5FrDvfckM4U0HYH9riXCvqVrdY=";
    };

    # Every byte the build may read, hash-pinned in inputs.lock.json.
    inputs = h.dependencies.fetchLockedInputs { lock = ./inputs.lock.json; };

    # The RUN lines, executed with no network.
    buildScript = ''
      set -eu
      in=/.nix-inputs/files

      # RUN pip install requests==2.32.4
      pip install --no-index --find-links $in/python/app requests==2.32.4

      # RUN apt-get update && apt-get install -y curl
      dpkg -i $in/apt/4c908884cf820836/0/*.deb
    '';

    # WORKDIR /app
    config.WorkingDir = "/app";
  };
in
h.mkEnvironment {
  name = "nginx-request-logging";
  task = ./task;
  containers = { inherit main; egress = h.egressImage; };
  services.main.container = "main";
}
