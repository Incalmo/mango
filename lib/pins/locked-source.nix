# Fetch a GitHub input pinned in flake.lock without evaluating the flake.
name:
let
  lock = (builtins.fromJSON (builtins.readFile ./flake.lock)).nodes.${name}.locked;
in {
  outPath = builtins.fetchTarball {
    url = "https://github.com/${lock.owner}/${lock.repo}/archive/${lock.rev}.tar.gz";
    sha256 = lock.narHash;
  };
}
