{ pkgs }:
(import ./images/linux-image.nix { inherit pkgs; }) // (import ./packages.nix { inherit pkgs; }) // {
  inherit pkgs;
  jarNormalizer = ../builders/normalize_jar.py;
  jksTrustStoreNormalizer = ../builders/normalize_jks.py;
  stageDebsScript = ../builders/stage-debs.sh;
  pinnedImage = import ./images/pinned-image.nix { inherit pkgs; };
  pinnedFilesScript = ../builders/pinned_files.py;
  pinnedFiles = pkgs.writeShellApplication {
    name = "incalmo-pinned-files";
    runtimeInputs = [ pkgs.python3 ];
    text = ''exec python3 ${../builders/pinned_files.py} "$@"'';
  };
  mkContainer = import ./container.nix { inherit pkgs; };
  mkEnvironment = import ./environment.nix { inherit pkgs; };
  requireTaskArchive = { sha256 }:
    assert pkgs.lib.assertMsg (builtins.match "[0-9a-f]{64}" sha256 != null)
      "Task archives require a lowercase hexadecimal SHA-256";
    pkgs.runCommand "task-${sha256}.tar.zst" {
      outputHashMode = "flat";
      outputHashAlgo = "sha256";
      outputHash = sha256;
      preferLocalBuild = true;
    } ''
      echo "Missing original task archive task-${sha256}.tar.zst" >&2
      echo "Add the explicitly supplied archive with that basename using nix-store --add-fixed sha256." >&2
      exit 1
    '';
  dependencies = import ./adapters { inherit pkgs; };
  mkImage = import ./images/mk-image.nix { inherit pkgs; };
  nix2container = import ./pins/nix2container.nix { inherit pkgs; };
  egressImage = import ./images/egress.nix { inherit pkgs; };
  harbor = import ./pins/harbor.nix { inherit pkgs; };
}
