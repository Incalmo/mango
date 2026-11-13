{
  description = "mango: reproducible learning environment build harness";
  outputs = { self }: let
    pkgs = import (import ./lib/pins/nixpkgs.nix) { system = "x86_64-linux"; };
    launcher = pkgs.writeShellScriptBin "mango" ''
      export PYTHONPATH=${./.}/src''${PYTHONPATH:+:$PYTHONPATH}
      exec ${pkgs.python3}/bin/python3 -m mango "$@"
    '';
  in {
    lib.mkHarness = import ./default.nix;
    devShells.x86_64-linux.default = import ./lib/shell.nix;
    packages.x86_64-linux.default = launcher;
    packages.x86_64-linux.mango = launcher;
    apps.x86_64-linux.default = { type = "app"; program = "${launcher}/bin/mango"; };
  };
}
