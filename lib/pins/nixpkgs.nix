# fetchTarball's fixed NAR hash allows offline evaluation after NAR import,
# without depending on the recipient's flake/Git metadata cache.
import ./locked-source.nix "nixpkgs"
