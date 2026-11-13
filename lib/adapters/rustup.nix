{ pkgs }:
{ lockFile }:
let
  lock = builtins.fromJSON (builtins.readFile lockFile);
  valid = lock.schema_version == 1
    && builtins.match "(stable|beta|nightly|[0-9]+\\.[0-9]+\\.[0-9]+)" lock.channel != null
    && builtins.match "[a-zA-Z0-9_-]+" lock.target != null
    && lock.toolchain == "${lock.channel}-${lock.target}"
    && builtins.match "[0-9]{4}-[0-9]{2}-[0-9]{2}" lock.date != null
    && builtins.elem lock.profile [ "minimal" "default" "complete" ]
    && builtins.isList lock.components && lock.components != []
    && builtins.length lock.components == builtins.length (pkgs.lib.unique (map (component: component.name) lock.components))
    && builtins.all (name: builtins.elem name (map (component: component.name) lock.components)) [ "rustc" "rust-std" "cargo" ]
    && builtins.all (component:
      builtins.match "[a-z0-9_-]+" component.name != null
      && builtins.match "[0-9a-f]{64}" component.sha256 != null
      && builtins.match "https://[^?#]+\\.tar\\.xz" component.url != null
    ) lock.components;
  archives = map (component: pkgs.fetchurl {
    url = component.url;
    sha256 = component.sha256;
  }) lock.components;
in assert pkgs.lib.assertMsg valid "Rustup toolchain requires a dated, checksummed component lock";
pkgs.runCommand "rustup-${lock.toolchain}-${lock.date}.tar" {
  nativeBuildInputs = [ pkgs.bash pkgs.coreutils pkgs.findutils pkgs.gnutar pkgs.xz pkgs.gnugrep pkgs.gnused ];
} ''
  mkdir tree unpack
  prefix="$PWD/tree"
  ${pkgs.lib.concatMapStringsSep "\n" (archive: ''
    rm -rf unpack/*
    tar -xJf ${archive} -C unpack --no-same-owner
    installer=$(find unpack -mindepth 2 -maxdepth 2 -name install.sh -type f)
    test "$(printf '%s\n' "$installer" | wc -l)" -eq 1
    test -n "$installer"
    bash "$installer" --prefix="$prefix" --disable-ldconfig
  '') archives}
  test -x tree/bin/rustc
  test -x tree/bin/cargo
  # Keep original installed file modes across the read-only Nix store boundary.
  tar --sort=name --mtime=@1 --owner=0 --group=0 --numeric-owner -cf "$out" -C tree .
''
