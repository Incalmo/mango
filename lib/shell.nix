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
    pkgs.jq
    pkgs.yq
    pkgs.gitMinimal
    pkgs.util-linux
    (command "mango" "mango")
    (command "mango-pin-base" "mango.authoring.pin_base")
    (command "mango-pin-source" "mango.authoring.pin_source")
    (command "mango-pin-file" "mango.authoring.pin_file")
    (command "mango-lock-packages" "mango.authoring.packages")
    (command "mango-lock-python" "mango.authoring.python_lock")
    (command "mango-lock-rustup" "mango.authoring.rustup_lock")
    (command "mango-lock-go" "mango.authoring.go_lock")
    (command "mango-prepare-prefix" "mango.authoring.package_state prepare")
    (command "mango-lock-indexes" "mango.authoring.package_state indexes")
    (command "mango-lock-npm" "mango.authoring.npm_lock")
    (command "mango-lock-composer" "mango.authoring.composer_lock")
    (command "mango-prepare-php" "mango.authoring.php_prefix")
  ];
  shellHook = ''
    export PYTHONPATH=${source}''${PYTHONPATH:+:$PYTHONPATH}
  '';
}
