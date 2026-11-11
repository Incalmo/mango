let
  h = import ../../. {};
in h.pkgs.mkShell {
  inputsFrom = [ (import ../../shell.nix) ];
  packages = [ h.pkgs.dpkg ];
}
