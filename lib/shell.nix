# Pinned tooling for running nix2rle: Python with the harness package on its
# path, Harbor, skopeo and the commands below.
let
  pkgs = import (import ./pins/nixpkgs.nix) { system = "x86_64-linux"; config = {}; overlays = []; };
  n2c = import ./pins/nix2container.nix { inherit pkgs; };
  harbor = import ./pins/harbor.nix { inherit pkgs; };
  # toString keeps the checkout's own path: edits apply without re-entering.
  source = toString ../src;
  command = name: module: pkgs.writeShellScriptBin name ''
    export PYTHONPATH=${source}''${PYTHONPATH:+:$PYTHONPATH}
    exec python -m ${module} "$@"
  '';
in pkgs.mkShell {
  packages = [
    (pkgs.python313.withPackages (p: [ p.pyyaml p.pydantic p.zstandard harbor ]))
    n2c.skopeo
    pkgs.ruff
    pkgs.util-linux
    (command "nix2rle" "nix2rle")
    (command "nix2rle-pin-base" "nix2rle.authoring.pin_base")
    (command "nix2rle-apt-lock" "nix2rle.authoring.apt_lock")
  ];
  shellHook = ''
    export PYTHONPATH=${source}''${PYTHONPATH:+:$PYTHONPATH}
  '';
}
