{ pkgs }:
# Build an image description with nix2container. The result is a small JSON
# file referencing immutable layer blobs. Application derivations can produce
# different bytes at the same input-addressed store path on different builders.
# Hash and retain each layer once; never reconstruct a cached hash from those
# paths during export.
let
  n2c = import ../pins/nix2container.nix { inherit pkgs; };
  lib = pkgs.lib;
in
{ name
  # Packages merged into one tree. With `prefix = null` it is placed at the
  # image root (/bin, /etc, ...). With a prefix such as "/opt/nix-tools" it is
  # only linked there, leaving a base image's own /bin and /etc untouched.
, packages ? [ ]
, prefix ? null
  # Further derivations whose contents are copied to the image root.
, root ? [ ]
  # Directories created in the image: { path; mode ? "0755"; uid ? 0; gid ? 0; }
, dirs ? [ ]
, config ? { }
, fromImage ? null
  # Exact file/directory permission overrides, applied after the generic defaults.
, filePerms ? [ ]
, maxLayers ? 100
}:
let
  tree = pkgs.buildEnv {
    name = "${baseNameOf name}-packages";
    paths = packages;
    ignoreCollisions = true;
  };
  # One real directory tree: nix2container rejects a path that is a symlink in
  # one source (buildEnv links /var whole) and a directory in another.
  rootfs = pkgs.runCommand "${baseNameOf name}-rootfs" { } (
    "mkdir -p $out\n"
    + lib.optionalString (packages != [ ]) (
      if prefix == null then "cp -rsL ${tree}/. $out/\nchmod -R u+w $out\n"
      else "mkdir -p \"$out$(dirname ${prefix})\"\nln -s ${tree} \"$out${prefix}\"\n"
    )
    + lib.concatMapStrings (source: ''
      chmod -R u+w $out
      cp -a --no-preserve=ownership --remove-destination ${source}/. $out/
    '') root
    + "chmod -R u+w $out\n"
    + lib.concatMapStrings (d: "mkdir -p \"$out\"/" + lib.escapeShellArg (lib.removePrefix "/" d.path) + "\n") dirs
  );
  escape = lib.escapeRegex;
  # Restore owner write permission stripped by the store, without making data
  # files executable (symlink modes are ignored). Later entries win, so specific
  # directories override the default.
  perms =
    [{ path = rootfs; regex = "^${escape "${rootfs}"}/.*"; orMode = "0200"; }]
    ++ map (d: {
      path = rootfs;
      regex = "^${escape "${rootfs}"}/${escape (lib.removePrefix "/" d.path)}$";
      mode = d.mode or "0755";
      uid = d.uid or 0;
      gid = d.gid or 0;
    }) (dirs ++ filePerms);
  configFile = pkgs.writeText "config.json" (builtins.toJSON config);
  layer = n2c.buildLayer {
    inherit maxLayers perms;
    copyToRoot = [ rootfs ];
    deps = [ configFile ];
    ignore = configFile;
    # nix2container's name means "retain the tar", not "allow corrupt data".
    # The blob and its digest are then substituted as one Nix output.
    reproducible = false;
  };
in
n2c.buildImage {
  name = "mango/${name}";
  inherit config fromImage;
  created = "1970-01-01T00:00:01Z";
  layers = [ layer ];
}
