{ pkgs }:
{ image, digest, hash }:
let
  base = (import ./linux-image.nix { inherit pkgs; }).fetchLinuxBase {
    inherit image digest hash;
  };
in pkgs.runCommand "pinned-image" {
  nativeBuildInputs = [ pkgs.python3 ];
  passthru = { inherit base; };
} ''
  python3 ${../../builders/pinned_image.py} --base ${base} --out "$out"
''
