# Keep manager-created npm metadata and ownership inside an explicit archive.
# This does not produce node_modules or run npm/application lifecycle commands.
{ pkgs }:
{ archive, receipt, name ? "npm-cache.tar.gz" }:
let
  document = builtins.fromJSON (builtins.readFile receipt);
  cache = document.cache_archive or {};
  offline = document.offline or {};
  valid = (document.status or null) == "completed"
    && (document.verified_by_replay or false)
    && (offline.verified_by_replay or false)
    && (offline.replay_error or null) == null
    && ((offline.replay or {}).ran or false)
    && (cache.lifecycle_replay_network_disabled or false)
    && (cache.installed_package_selection_equal or false)
    && builtins.match "[0-9a-f]{64}" (cache.sha256 or "") != null
    && (cache.path or "") == "npm/cache/${cache.sha256}.tar.gz";
in
assert pkgs.lib.assertMsg valid "npm cache requires a successful offline lifecycle replay and exact archive identity";
pkgs.runCommand name {
  nativeBuildInputs = [ pkgs.python3 pkgs.coreutils ];
} ''
  python - ${archive} ${pkgs.lib.escapeShellArg cache.sha256} "$out" <<'PY'
import hashlib, pathlib, runpy, shutil, sys
source, expected, output = pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3])
with source.open('rb') as stream:
    if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
        raise ValueError('npm cache archive hash differs from its replay receipt')
pin = runpy.run_path('${../../src/mango/authoring/cache_archive.py}')['pin_cache_archive']
record = pin(source, pathlib.Path('validated-cache'), prefix='npm/cache')
if record['sha256'] != expected:
    raise ValueError('npm cache archive changed during validation')
shutil.copyfile(record['author_source'], output)
PY
''
