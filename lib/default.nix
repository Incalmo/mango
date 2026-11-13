{ pkgs }:
(import ./images/linux-image.nix { inherit pkgs; }) // (import ./packages.nix { inherit pkgs; }) // {
  inherit pkgs;
  jarNormalizer = ../builders/normalize_jar.py;
  jksTrustStoreNormalizer = ../builders/normalize_jks.py;
  stageDebsScript = ../builders/stage-debs.sh;
  installDebsScript = ../builders/install-debs.sh;
  restoreAptIndexesScript = ../builders/restore-apt-indexes.sh;
  pinnedImage = import ./images/pinned-image.nix { inherit pkgs; };
  pinnedFilesScript = ../builders/pinned_files.py;
  pinnedFiles = pkgs.writeShellApplication {
    name = "mango-pinned-files";
    runtimeInputs = [ pkgs.python3 ];
    text = ''exec python3 ${../builders/pinned_files.py} "$@"'';
  };
  mkContainer = import ./container.nix { inherit pkgs; };
  mkEnvironment = import ./environment.nix { inherit pkgs; };
  # A grader image: the task's tests/ at /tests (test.sh executable) on a
  # minimal Python userland, kept running for the verifier to exec into.
  pythonVerifier = { name ? "verifier", tests, packages ? [ ], python ? pkgs.python3 }:
    import ./container.nix { inherit pkgs; } {
      inherit name;
      packages = [ python pkgs.bashInteractive pkgs.coreutils ] ++ packages;
      files = {
        "/tests".source = tests;
      } // pkgs.lib.optionalAttrs (builtins.pathExists (tests + "/test.sh")) {
        "/tests/test.sh" = { source = tests + "/test.sh"; mode = "0755"; };
      };
      directories = [ { path = "/tmp"; mode = "1777"; } { path = "/logs"; } ];
      environment.HOME = "/tmp";
      workingDir = "/tests";
      command = [ "sleep" "infinity" ];
    };
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
