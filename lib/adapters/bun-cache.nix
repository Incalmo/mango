{ pkgs, bun, source, hash, name ? "bun-dependency-cache"
, projectFiles ? [ "package.json" "bun.lock" ] }:
let
  lib = pkgs.lib;
  valid = path: path != "" && !lib.hasPrefix "/" path
    && builtins.all (p: p != "" && p != "." && p != "..") (lib.splitString "/" path);
  files = assert lib.assertMsg (builtins.all valid projectFiles)
    "Bun projectFiles must be safe relative paths";
    assert lib.assertMsg (builtins.elem "package.json" projectFiles
      && (builtins.elem "bun.lock" projectFiles || builtins.elem "bun.lockb" projectFiles))
      "Bun projectFiles must include package.json and a lockfile";
    projectFiles;
  # A local checkout may contain credentials outside the selected manifests.
  # Filter before coercing its path into /nix/store.
  inputs = if builtins.isPath source then builtins.path {
    name = "bun-project-inputs";
    path = source;
    filter = path: type: type == "directory" || builtins.elem
      (lib.removePrefix ((toString source) + "/") (toString path)) files;
  } else source;
in
# Only downloaded package inputs are retained. node_modules and lifecycle-script
# outputs are deliberately discarded and rebuilt in the offline application.
pkgs.stdenvNoCC.mkDerivation {
  inherit name;
  nativeBuildInputs = [ bun pkgs.cacert pkgs.python3 ];
  dontUnpack = true;
  dontConfigure = true;
  dontFixup = true;
  buildPhase = ''
    mkdir project cache private-home
    ${lib.concatMapStringsSep "\n" (file: ''
      test -f ${lib.escapeShellArg "${inputs}/${file}"}
      mkdir -p "project/$(dirname -- ${lib.escapeShellArg file})"
      cp -- ${lib.escapeShellArg "${inputs}/${file}"} ${lib.escapeShellArg "project/${file}"}
    '') files}
    cd project
    env HOME="$TMPDIR/private-home" BUN_INSTALL_CACHE_DIR="$TMPDIR/cache" \
      CI=true bun install --frozen-lockfile --ignore-scripts
  '';
  installPhase = ''
    python - "$TMPDIR/cache" <<'PYTHON'
import os, pathlib, sys
root = pathlib.Path(sys.argv[1])
for path in root.rglob("*"):
    if path.is_symlink():
        target = path.resolve()
        if not target.is_relative_to(root):
            raise RuntimeError("Bun cache symlink escapes the retained inputs")
        path.unlink()
        path.symlink_to(os.path.relpath(target, path.parent))
PYTHON
    cp -r "$TMPDIR/cache" $out
  '';
  preferLocalBuild = false;
  allowSubstitutes = true;
  outputHash = hash;
  outputHashAlgo = "sha256";
  outputHashMode = "recursive";
}
