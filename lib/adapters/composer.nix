# Composer download-cache inputs. Nothing here runs Composer or builds vendor/:
# the original toolchain replays the original install offline (docs/composer-cache.md).
{ pkgs, fetchDependencyCache }:
let
  lib = pkgs.lib;
  # Archives live under files/ in the cache dir. Conservative characters only, so a
  # path is safe to quote in shell and cannot hide traversal.
  safePath = path: builtins.isString path
    && builtins.match "files(/[A-Za-z0-9._+@=,~-]+)+" path != null
    && builtins.all (part: part != "." && part != "..") (lib.splitString "/" path);
in
{ lock, name ? "composer-cache" }:
let
  document = builtins.fromJSON (builtins.readFile lock);
  artifacts = document.artifacts or null;
in
assert lib.assertMsg ((document.kind or null) == "composer-cache" && (document.schema_version or null) == 1)
  "Composer lock requires kind = \"composer-cache\" and schema_version = 1";
assert lib.assertMsg (builtins.isList artifacts && artifacts != [])
  "Composer lock requires a non-empty artifacts list";
assert lib.assertMsg ((document.unmapped_files or []) == [])
  "Composer unmapped_files are not supported yet: cache files that are not locked archives fail closed";
assert lib.assertMsg (builtins.all (record: builtins.isAttrs record
  && builtins.attrNames record == [ "path" "sha256" "urls" ]
  && safePath record.path && builtins.isList record.urls
  && builtins.isString record.sha256 && builtins.match "[0-9a-f]{64}" record.sha256 != null) artifacts)
  "Composer artifacts must be exactly { path, urls, sha256 } with a safe files/... path and a 64-hex sha256";
# Path uniqueness/overlap and URL checks are shared with every other adapter.
fetchDependencyCache { inherit name artifacts; }
