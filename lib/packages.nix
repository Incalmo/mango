{ pkgs }:
let
  lib = pkgs.lib;
  adapters = import ./adapters { inherit pkgs; };
  validPath = value: builtins.isString value && value != ""
    && !lib.hasPrefix "/" value
    && builtins.all (part: part != "" && part != "." && part != "..") (lib.splitString "/" value);
in {
  mkSourcePackage = { pname, version, src, patches ? [], build, install, ... }@args:
    let patchedSource = if patches == [] then src else pkgs.applyPatches ({
      name = "${pname}-${version}-source";
      inherit src patches;
    } // lib.optionalAttrs (args ? patchFlags) { inherit (args) patchFlags; });
    in adapters.mkOfflineApplication (
      builtins.removeAttrs args [ "build" "install" "patches" "patchFlags" ] // {
        src = patchedSource;
        dontConfigure = args.dontConfigure or true;
        buildPhase = ''runHook preBuild
          ${build}
          runHook postBuild
        '';
        installPhase = ''runHook preInstall
          ${install}
          runHook postInstall
        '';
        passthru = (args.passthru or {}) // { inherit patchedSource; };
      });

  # Explicit pinned file or archive plus an authored install layout. Nothing is resolved.
  mkBinaryPackage = { pname, version, urls ? null, sha256 ? null, files ? {}, format ? "file"
    , inputs ? pkgs.emptyDirectory, install ? null, ... }@args:
    assert lib.assertMsg (builtins.elem format [ "file" "archive" ])
      "mkBinaryPackage format must be file or archive";
    assert lib.assertMsg (install != null || (files != {} && builtins.all validPath (builtins.attrNames files)))
      "mkBinaryPackage file destinations must be nonempty relative paths";
    assert lib.assertMsg (builtins.all (spec:
      builtins.isAttrs spec && builtins.all (key: builtins.elem key [ "source" "mode" ]) (builtins.attrNames spec)
      && (if format == "file" then !(spec ? source) else validPath (spec.source or ""))
      && builtins.match "[0-7]{3,4}" (spec.mode or "0755") != null
    ) (builtins.attrValues files)) "Invalid binary package file specification";
    assert lib.assertMsg ((urls == null) == (sha256 == null))
      "Binary URLs and hash must be supplied together";
    assert lib.assertMsg (install == null || files == {})
      "Choose either a binary file layout or a custom install command";
    assert lib.assertMsg (install != null || (urls != null && sha256 != null))
      "Binary files require explicit URLs/hash; custom installation requires explicitly pinned inputs";
    adapters.mkOfflineApplication (builtins.removeAttrs args [ "urls" "sha256" "files" "format" "install" ] // {
      inherit pname version inputs;
      src = if urls == null then inputs else adapters.fetchDependency { name = "${pname}-${version}"; inherit urls sha256; };
      dontUnpack = true;
      dontConfigure = true;
      buildPhase = "runHook preBuild; runHook postBuild";
      dontFixup = args.dontFixup or (install == null);
      nativeBuildInputs = (args.nativeBuildInputs or []) ++ [ pkgs.python3 ];
      installPhase = if install != null then ''
        runHook preInstall
        ${install}
        runHook postInstall
      '' else ''
        runHook preInstall
        python - "$src" "$out" ${pkgs.writeText "binary-layout.json" (builtins.toJSON files)} ${format} <<'PY'
import json, pathlib, shutil, sys, tarfile, zipfile
source, output, layout, format = sys.argv[1:]
root = pathlib.Path("unpacked")
if format == "archive":
    root.mkdir()
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            for member in archive.infolist():
                name = pathlib.PurePosixPath(member.filename)
                if name.is_absolute() or ".." in name.parts:
                    raise ValueError("Unsafe binary zip path")
            archive.extractall(root)
    else:
        with tarfile.open(source) as archive:
            archive.extractall(root, filter="data")
for destination, spec in json.load(open(layout)).items():
    origin = pathlib.Path(source) if format == "file" else root / spec["source"]
    if format == "archive" and not origin.resolve().is_relative_to(root.resolve()):
        raise ValueError("Binary archive source escapes root")
    target = pathlib.Path(output) / destination
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(origin, target)
    target.chmod(int(spec.get("mode", "0755"), 8))
PY
        runHook postInstall
      '';
    });
}
