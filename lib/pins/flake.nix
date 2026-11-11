{
  # Lock file only: nixpkgs.nix and nix2container.nix read flake.lock so the
  # harness can be evaluated without flakes. `nix flake update` refreshes both.
  description = "Pinned inputs for the Nix Harbor harness";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.nix2container = {
    url = "github:nlewo/nix2container";
    flake = false;
  };
  outputs = { self, ... }: { };
}
