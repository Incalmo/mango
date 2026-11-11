let
  pkgs = import (import ../../lib/pins/nixpkgs.nix) { config = {}; overlays = []; };
in pkgs.mkShell { packages = [ pkgs.python3 pkgs.jdk11 pkgs.ruff ]; }
