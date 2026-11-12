# Pinned tooling for running mango: Python with the harness package on its
# path, Harbor, skopeo, git and the commands below.
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
    pkgs.gitMinimal
    pkgs.util-linux
    (command "mango" "mango")
    (command "mango-pin-base" "mango.authoring.pin_base")
    (command "mango-pin-source" "mango.authoring.pin_source")
    (command "mango-lock-packages" "mango.authoring.packages")
  ];
  shellHook = ''
    export PYTHONPATH=${source}''${PYTHONPATH:+:$PYTHONPATH}
  '';
}
