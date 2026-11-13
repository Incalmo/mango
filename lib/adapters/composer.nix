# Composer download-cache inputs. Nothing here runs Composer or builds vendor/:
# the original toolchain replays the original install offline (docs/composer-cache.md).
# Besides locked archives, the cache may carry the manager's own marker file
# (.htaccess = "Deny from all", written by Composer itself); it is inline, hash-checked text.
{ pkgs, fetchDependencyCache }:
let
  lib = pkgs.lib;
  # Archives live under files/ in the cache dir. Conservative characters only, so a
  # path is safe to quote in shell and cannot hide traversal.
  safePath = path: builtins.isString path
    && builtins.match "files(/[A-Za-z0-9._+@=,~-]+)+" path != null
    && builtins.all (part: part != "." && part != "..") (lib.splitString "/" path);
  # Closed set of cache files Composer writes itself, with their exact content.
  knownGenerated = { ".htaccess" = "Deny from all"; };
in
{ lock, name ? "composer-cache" }:
let
  document = builtins.fromJSON (builtins.readFile lock);
  artifacts = document.artifacts or null;
  generated = document.generated_files or [];
in
assert lib.assertMsg ((document.kind or null) == "composer-cache" && (document.schema_version or null) == 1)
  "Composer lock requires kind = \"composer-cache\" and schema_version = 1";
assert lib.assertMsg (builtins.isList artifacts && artifacts != [])
  "Composer lock requires a non-empty artifacts list";
assert lib.assertMsg ((document.unmapped_files or []) == [])
  "Composer unmapped_files are not supported yet: cache files that are not locked archives fail closed";
assert lib.assertMsg (builtins.isList generated && builtins.all (record: builtins.isAttrs record
  && builtins.attrNames record == [ "content" "path" "sha256" ]
  && builtins.isString record.path && knownGenerated ? ${record.path}
  && record.content == knownGenerated.${record.path}
  && builtins.isString record.sha256 && record.sha256 == builtins.hashString "sha256" record.content) generated)
  "Composer generated_files must be exactly { path, content, sha256 } for a known manager file (.htaccess = \"Deny from all\") whose sha256 matches its content";
assert lib.assertMsg (builtins.length (map (record: record.path) generated) == builtins.length (lib.unique (map (record: record.path) generated)))
  "Composer generated_files contain duplicate paths";
assert lib.assertMsg (builtins.all (record: builtins.isAttrs record
  && builtins.attrNames record == [ "path" "sha256" "urls" ]
  && safePath record.path && builtins.isList record.urls
  && builtins.isString record.sha256 && builtins.match "[0-9a-f]{64}" record.sha256 != null) artifacts)
  "Composer artifacts must be exactly { path, urls, sha256 } with a safe files/... path and a 64-hex sha256";
# Path uniqueness/overlap (archives and generated files together) and URL checks are
# shared with every other adapter; the generated files become inline metadata records.
fetchDependencyCache { inherit name artifacts; metadata = generated; }
