# Tooling for the documentation site (mkdocs.yml):
#   nix-shell docs.nix --run 'mkdocs serve'            # live preview
#   nix-shell docs.nix --run 'mkdocs build --strict'   # writes build/site
let
  pkgs = import (import ./lib/pins/nixpkgs.nix) { system = "x86_64-linux"; config = {}; overlays = []; };
in pkgs.mkShell {
  packages = [ (pkgs.python3.withPackages (p: [ p.mkdocs p.mkdocs-material ])) ];
}
