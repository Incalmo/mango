{ system ? builtins.currentSystem
, pkgs ? import (import ./lib/pins/nixpkgs.nix) { inherit system; config = {}; overlays = []; }
}:
import ./lib { inherit pkgs; }
