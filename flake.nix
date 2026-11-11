{
  description = "mango: reproducible learning environment build harness";
  outputs = { self }: {
    lib.mkHarness = import ./default.nix;
    devShells.x86_64-linux.default = import ./lib/shell.nix;
  };
}
