{ pkgs }:
let
  lib = pkgs.lib;
  mkImage = import ./images/mk-image.nix { inherit pkgs; };
  linux = import ./images/linux-image.nix { inherit pkgs; };
  imageReader = builtins.path {
    path = ../builders;
    name = "container-base-reader";
    filter = path: type: builtins.elem (baseNameOf path) [ "builders" "rootfs.py" "artifacts.py" ];
  };
  registryBase = value: builtins.isAttrs value
    && value ? image && value ? digest && value ? hash
    && builtins.isString value.image && builtins.isString value.digest && builtins.isString value.hash
    && builtins.match "sha256:[0-9a-f]{64}" value.digest != null && value.hash != ""
    && builtins.all (key: builtins.elem key [ "image" "digest" "hash" "name" ]) (builtins.attrNames value);
  layoutBase = value: builtins.isAttrs value && builtins.attrNames value == [ "ociLayout" ]
    && (lib.isDerivation value.ociLayout || builtins.isPath value.ociLayout);
  absolute = value: builtins.isString value && lib.hasPrefix "/" value
    && value != "/" && builtins.all (part: part != "" && part != "." && part != "..")
      (lib.splitString "/" (lib.removePrefix "/" value));
  argumentList = value: value == null || (builtins.isList value && builtins.all builtins.isString value);
  validPort = value:
    let matched = builtins.match "([1-9][0-9]{0,4})(/(tcp|udp))?" (toString value);
    in (builtins.isInt value || builtins.isString value) && matched != null
      && lib.toInt (builtins.head matched) <= 65535;
in
{ name, base ? null, packages ? [], files ? {}, directories ? [], environment ? {}
, command ? null, entrypoint ? null, workingDir ? null, user ? null, ports ? [], roots ? [] }:
assert lib.assertMsg (argumentList command && argumentList entrypoint)
  "Container command/entrypoint must be string lists or null";
assert lib.assertMsg (builtins.all builtins.isString (builtins.attrValues environment))
  "Container environment values must be strings";
assert lib.assertMsg (builtins.isList ports && builtins.all validPort ports)
  "Container ports must be 1..65535 with optional /tcp or /udp";
assert lib.assertMsg (builtins.all absolute (builtins.attrNames files))
  "Container file destinations must be normalized absolute paths";
assert lib.assertMsg (builtins.all (spec:
  builtins.isAttrs spec && builtins.length (builtins.filter (key: builtins.hasAttr key spec) [ "source" "text" "symlink" ]) == 1
  && builtins.all (key: builtins.elem key [ "source" "text" "symlink" "mode" ]) (builtins.attrNames spec)
  && (!(spec ? mode) || (builtins.match "[0-7]{3,4}" spec.mode != null && !(spec ? symlink)))
) (builtins.attrValues files)) "Container files require exactly one of source/text/symlink and an optional octal mode";
assert lib.assertMsg (builtins.all (d: absolute d.path) directories)
  "Container directories must use normalized absolute paths";
assert lib.assertMsg (base == null || registryBase base || layoutBase base)
  "Container base must be a pinned { image; digest; hash; } or exactly { ociLayout = imageDerivationOrPath; }";
let
  baseDirectory = if base == null then null
    else if layoutBase base then base.ociLayout
    else linux.fetchLinuxBase base;
  baseImage = if baseDirectory == null then null else pkgs.runCommand "${name}-base.json" {
    nativeBuildInputs = [pkgs.python3];
  } ''
    python - ${baseDirectory} "$out" <<'PYBASE'
import json, sys
sys.path.insert(0, "${imageReader}")
from rootfs import BaseImage
base = BaseImage(sys.argv[1])
if ${if base != null && layoutBase base then "True" else "False"} and not base.oci:
    raise ValueError("ociLayout base must be a single-image OCI layout")
manifest, config = base.manifest, base.config
if config.get("os") != "linux" or config.get("architecture") != "amd64":
    raise ValueError("Container base requires a linux/amd64 image")
media = {
    "application/vnd.docker.image.rootfs.diff.tar.gzip": "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar": "application/vnd.oci.image.layer.v1.tar",
}
layers = []
for layer, diff_id in zip(manifest["layers"], config["rootfs"]["diff_ids"], strict=True):
    layers.append({"digest":layer["digest"], "size":layer["size"], "diff_ids":diff_id,
        "mediatype":media.get(layer["mediaType"], layer["mediaType"]),
        "layer-path":str(base.blob(layer))})
image = {"version":1, "image-config":config.get("config", {}), "layers":layers,
    "arch":config.get("architecture", "amd64"), "created":None}
with open(sys.argv[2], "w") as output: json.dump(image, output, sort_keys=True)
PYBASE
  '';
  # Scratch images declare User=root below, so provide the account it names.
  # Distro bases retain their account databases; explicit files take precedence.
  effectiveFiles = (lib.optionalAttrs (base == null) {
    "/etc/passwd".text = "root:x:0:0:root:/root:/bin/sh\n";
    "/etc/group".text = "root:x:0:\n";
  }) // files;
  layout = pkgs.writeText "${name}-files.json" (builtins.toJSON effectiveFiles);
  declaredRoot = pkgs.runCommand "${name}-files" { nativeBuildInputs = [pkgs.python3]; } ''
    python - ${layout} "$out" <<'PY'
import json, pathlib, shutil, sys
root = pathlib.Path(sys.argv[2]); root.mkdir()
for name, spec in json.load(open(sys.argv[1])).items():
    target = root / name.lstrip("/")
    if any(parent.is_symlink() for parent in target.parents if parent != root):
        raise ValueError("Container destination traverses a symlink: " + name)
    target.parent.mkdir(parents=True, exist_ok=True)
    for parent in target.parents:
        if parent == root: break
        parent.chmod(parent.stat().st_mode | 0o200)
    if target.is_symlink() or target.is_file(): target.unlink()
    if "text" in spec:
        target.write_text(spec["text"])
        target.chmod(int(spec.get("mode", "0644"), 8))
    elif "symlink" in spec:
        target.symlink_to(spec["symlink"])
    else:
        source = pathlib.Path(spec["source"])
        if source.is_dir(): shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
        else: shutil.copy2(source, target, follow_symlinks=False)
PY
  '';
  filePerms = lib.mapAttrsToList (path: spec: {
    inherit path; mode = spec.mode or "0644";
  }) (lib.filterAttrs (_: spec: spec ? mode || spec ? text) effectiveFiles);
  explicitConfig = {
    Env = lib.mapAttrsToList (key: value: "${key}=${value}") environment;
  } // lib.optionalAttrs (command != null) { Cmd = command; }
    // lib.optionalAttrs (entrypoint != null) { Entrypoint = entrypoint; }
    // lib.optionalAttrs (workingDir != null) { WorkingDir = workingDir; }
    // lib.optionalAttrs (user != null) { User = user; };
  assembled = mkImage {
    inherit name packages filePerms;
    config = explicitConfig;
    prefix = if base == null then null else "/opt/nix-tools";
    root = roots ++ [declaredRoot]; dirs = directories;
    fromImage = baseImage;
  };
  settings = pkgs.writeText "${name}-config.json" (builtins.toJSON {
    inherit environment command entrypoint workingDir user ports;
    withPackages = packages != [];
    withBase = base != null;
  });
in pkgs.runCommand "container-${name}.json" {
  nativeBuildInputs = [pkgs.python3];
  passthru = { inherit declaredRoot baseImage; };
} ''
  python - ${assembled} ${if baseImage == null then "-" else baseImage} ${settings} "$out" <<'PY'
import json, sys
image = json.load(open(sys.argv[1]))
config = {} if sys.argv[2] == "-" else json.load(open(sys.argv[2]))["image-config"]
options = json.load(open(sys.argv[3]))
env = dict(item.split("=", 1) for item in config.get("Env", []))
if options["withPackages"]:
    env["PATH"] = "/opt/nix-tools/bin:" + env.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin") if options["withBase"] else "/bin"
if not options["withBase"]:
    config.update(User="root", WorkingDir="/", Cmd=[], Entrypoint=[])
    env.setdefault("PATH", "/bin")
env.update(options["environment"])
config["Env"] = [key + "=" + value for key, value in sorted(env.items())]
for option, field in [("command", "Cmd"), ("entrypoint", "Entrypoint"), ("workingDir", "WorkingDir"), ("user", "User")]:
    if options[option] is not None: config[field] = options[option]
if options["ports"]:
    ports = config.setdefault("ExposedPorts", {})
    for port in options["ports"]:
        value = str(port)
        number, separator, protocol = value.partition("/")
        if not number.isdigit() or not 1 <= int(number) <= 65535 or (separator and protocol not in ("tcp", "udp")):
            raise ValueError("Container port must be 1..65535 with optional /tcp or /udp: " + value)
        ports[str(int(number)) + "/" + (protocol if separator else "tcp")] = {}
image["image-config"] = config
with open(sys.argv[4], "w") as output: json.dump(image, output, sort_keys=True)
PY
''
