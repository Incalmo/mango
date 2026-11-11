# nix2container builds images as JSON descriptions referencing store paths;
# skopeo streams layers straight from /nix/store, so no image tarball is stored.
{ pkgs }:
let
  source = import ./locked-source.nix "nix2container";
  upstream = import source.outPath { inherit pkgs; };
in upstream.nix2container // {
  inherit source;
  skopeo = upstream.skopeo-nix2container;
}
