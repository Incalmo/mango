{ pkgs }:
let
  lib = pkgs.lib;
  validPath = path:
    builtins.isString path && path != "" && !lib.hasPrefix "/" path
    && builtins.all (part: part != "" && part != "." && part != "..")
      (lib.splitString "/" path);
  validatePaths = records:
    let paths = map (record: record.path) records;
    in assert lib.assertMsg (builtins.all validPath paths)
      "dependency cache paths must be relative without . or ..";
    assert lib.assertMsg (builtins.length paths == builtins.length (lib.unique paths))
      "dependency cache contains duplicate paths";
    assert lib.assertMsg (builtins.all (a: builtins.all (b:
      a == b || !lib.hasPrefix (a + "/") b) paths) paths)
      "dependency cache contains overlapping file and directory paths";
    records;
  # Exactly one content hash: sha256, or sha512 for upstreams that publish only
  # SHA-512 (hexadecimal, base32 or SRI, as fetchurl accepts).
  fetchDependency = { name, urls, sha256 ? null, sha512 ? null }:
    assert lib.assertMsg (urls != []
      && builtins.length (builtins.filter (h: h != null && h != "") [ sha256 sha512 ]) == 1)
      "dependencies require explicit URLs and exactly one content hash (sha256 or sha512)";
    assert lib.assertMsg (builtins.all (url:
      builtins.match "https?://[^/@]+(/.*)?" url != null
      && builtins.match ".*[?&](token|access_token|password|signature|X-Amz-Signature|X-Goog-Signature)=.*" url == null) urls)
      "dependency URLs must be HTTP(S), without userinfo or credential query parameters";
    pkgs.fetchurl ({
      inherit urls;
      name = lib.strings.sanitizeDerivationName name;
      # Prefer our published substitute to contacting the upstream again.
      preferLocalBuild = false;
    } // (if sha256 != null then { inherit sha256; } else { inherit sha512; }));
  # Hash attributes of a lock record, whichever of sha256/sha512 it carries.
  recordHash = record: { sha256 = record.sha256 or null; sha512 = record.sha512 or null; };
  # A record names its bytes by URL, or by `file`: a path relative to `root`
  # for bytes committed beside the lock because no URL keeps serving them.
  # `extra` adds already-built store paths, such as tools, by destination name.
  fetchDependencyCache = { name, artifacts, metadata ? [], root ? null, extra ? {} }:
    let
      extras = lib.mapAttrsToList (path: _: { inherit path; }) extra;
      records = validatePaths (artifacts ++ metadata ++ extras);
      local = record:
        assert lib.assertMsg (root != null && validPath record.file && !(record ? urls))
          "a committed dependency file needs a lock directory and a relative path, and no URLs";
        builtins.path {
          path = root + "/${record.file}";
          name = lib.strings.sanitizeDerivationName (builtins.baseNameOf record.path);
          sha256 = record.sha256;
          recursive = false;
        };
      downloads = map (record: {
        name = record.path;
        path = if record ? file then local record else fetchDependency ({
          name = builtins.baseNameOf record.path;
          inherit (record) urls;
        } // recordHash record);
      }) artifacts;
      generated = map (record: {
        name = record.path;
        path = assert lib.assertMsg
          (builtins.hashString "sha256" record.content == record.sha256)
          "dependency metadata content hash mismatch";
          pkgs.writeText (lib.strings.sanitizeDerivationName (builtins.baseNameOf record.path)) record.content;
      }) metadata;
      added = lib.mapAttrsToList (name: path: { inherit name path; }) extra;
    in builtins.deepSeq records (pkgs.linkFarm name (downloads ++ generated ++ added));
in {
  inherit fetchDependency fetchDependencyCache;
  fetchRustupToolchain = import ./rustup.nix { inherit pkgs; };
  # Explicitly selected native-lock reader: Cargo registry checksums are reused.
  # Git entries need supplemental NAR hashes; never fetch unhashed Git in eval.
  fetchCargoCache = { lockFile, outputHashes ? {}, extraRegistries ? {} }:
    pkgs.rustPlatform.importCargoLock {
      inherit lockFile outputHashes extraRegistries;
      allowBuiltinFetchGit = false;
    };
  cargoSetupHook = pkgs.rustPlatform.cargoSetupHook;
  # The authoring agent supplies the complete inventory. No discovery, package
  # resolution, lockfile translation or command inspection takes place here.
  # `prefixes` selects the entries one image needs (a path equal to a prefix or
  # under it), so an image's inputs never change when another image's do.
  fetchLockedInputs = { lock, name ? "environment-inputs", prefixes ? null, extra ? {} }:
    let
      document = builtins.fromJSON (builtins.readFile lock);
      wanted = artifact: prefixes == null || builtins.any
        (prefix: artifact.path == prefix || lib.hasPrefix (prefix + "/") artifact.path)
        prefixes;
    in assert lib.assertMsg ((document.schema_version or null) == 1)
      "inputs lock requires schema_version = 1";
      assert lib.assertMsg (builtins.isList (document.artifacts or null))
        "inputs lock requires an explicit artifacts list";
      assert lib.assertMsg (prefixes == null || builtins.all builtins.isString prefixes)
        "fetchLockedInputs prefixes must be a list of path strings";
      fetchDependencyCache {
        inherit name extra;
        root = builtins.dirOf lock;
        artifacts = builtins.filter wanted document.artifacts;
      };

  fetchPythonWheelhouse = { artifacts, name ? "python-wheelhouse" }:
    assert lib.assertMsg (builtins.all (record:
      lib.hasSuffix ".whl" record.path && !lib.hasInfix "/" record.path) artifacts)
      "wheelhouse requires flat wheel filenames; build source distributions separately";
    fetchDependencyCache { inherit name artifacts; };
  # Reuse Nixpkgs' per-package npm fetcher and offline setup hook. Git and
  # local/workspace dependencies require explicit future support, not fallback.
  fetchNpmCache = { source }:
    let
      lock = builtins.fromJSON (builtins.readFile (source + "/package-lock.json"));
      package = builtins.fromJSON (builtins.readFile (source + "/package.json"));
      valid = name: module: name == "" || (!(module.link or false) && (
        (module.inBundle or false) || (module ? resolved && module ? integrity
          && builtins.match "(sha256|sha512)-[A-Za-z0-9+/]+={0,2}" module.integrity != null
          && builtins.match "https?://[^/@]+(/.*)?" module.resolved != null
          && builtins.match ".*[?&](token|access_token|password|signature|X-Amz-Signature|X-Goog-Signature)=.*" module.resolved == null)));
      checked = lib.mapAttrsToList valid (lock.packages or {});
      options = builtins.mapAttrs (_: _: { preferLocalBuild = false; }) (lock.packages or {});
    in assert lib.assertMsg (builtins.elem (lock.lockfileVersion or 0) [ 2 3 ])
      "npm requires a version 2 or 3 package-lock.json";
      assert lib.assertMsg (builtins.all (value: value) checked)
      "npm dependencies require credential-free HTTP(S) URLs and integrity hashes; Git/workspace dependencies are not supported yet";
      pkgs.importNpmLock { inherit package; packageLock = lock; fetcherOpts = options; };
  npmConfigHook = pkgs.importNpmLock.npmConfigHook;
  importNpmCacheArchive = import ./npm-cache-archive.nix { inherit pkgs; };
  fetchAptCache = { artifacts, name ? "apt-archives" }:
    assert lib.assertMsg (builtins.all (r: lib.hasSuffix ".deb" r.path && !lib.hasInfix "/" r.path) artifacts)
      "apt cache requires flat .deb filenames";
    fetchDependencyCache { inherit name artifacts; };
  fetchApkCache = { artifacts, name ? "apk-archives" }:
    assert lib.assertMsg (builtins.all (r: lib.hasSuffix ".apk" r.path && !lib.hasInfix "/" r.path) artifacts)
      "apk cache requires flat .apk filenames";
    fetchDependencyCache { inherit name artifacts; };
  fetchComposerCache = import ./composer.nix { inherit pkgs fetchDependencyCache; };
  fetchBunCache = args: import ./bun-cache.nix ({ inherit pkgs; } // args);
  mkOfflineApplication = args:
    assert lib.assertMsg (!(args ? outputHash) && !(args.__noChroot or false)
      && !(args.__impure or false)) "Application builds must be ordinary sandboxed derivations";
    pkgs.stdenvNoCC.mkDerivation (args // {
      preBuild = ''
        # Fail even if a caller accidentally disables the Nix sandbox.
        ${pkgs.python3}/bin/python - <<'ISOLATION'
import socket, sys
if sys.platform != "linux" or any(name != "lo" for _, name in socket.if_nameindex()):
    raise SystemExit("Application build has a network interface; enable sandbox=true and sandbox-fallback=false")
ISOLATION
      '' + (args.preBuild or "");
    });
  prepareCache = pkgs.writeShellApplication {
    name = "prepare-dependency-cache";
    runtimeInputs = [ pkgs.coreutils ];
    text = ''
      if [ "$#" -ne 2 ]; then
        echo "usage: prepare-dependency-cache STORE_CACHE NEW_DIRECTORY" >&2
        exit 2
      fi
      if [ -e "$2" ] || [ -L "$2" ]; then
        echo "dependency cache destination already exists: $2" >&2
        exit 2
      fi
      mkdir -p -- "$2"
      cp -rL -- "$1/." "$2/"
      chmod -R u+w -- "$2"
    '';
  };
} // import ./pnpm.nix { inherit pkgs; }
