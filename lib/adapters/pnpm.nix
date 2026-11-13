{ pkgs }:
# pnpm dependency cache, built on the pinned Nixpkgs `fetchPnpmDeps` helper.
#
# The helper owns everything format-specific: `pnpm install --frozen-lockfile
# --ignore-scripts --force` into a private store, removal of timestamps and
# project symlinks, JSON key sorting, the SQLite dump (pnpm 11), permission
# normalisation and the reproducible tarball. This file adds only (a) a
# fail-closed, explicit input inventory in front of it and (b) metadata/helper
# scripts for the offline side. It never edits store metadata itself.
let
  lib = pkgs.lib;

  # Config/manifest files whose silent omission would change what pnpm installs.
  # A pnpmfile executes project code during resolution; matched by basename at any depth.
  pnpmfileNames = [ ".pnpmfile.cjs" "pnpmfile.cjs" ".pnpmfile.mjs" "pnpmfile.mjs" ];
  watchedNames = [ "package.json" "pnpm-lock.yaml" "pnpm-workspace.yaml" ".npmrc" ] ++ pnpmfileNames;
  skippedDirs = [ "node_modules" ".git" ];

  # Safe relative path: no empty/`.`/`..` segment and only a conservative character set (no
  # backslash, control, quote, `$`, backtick or glob characters), so names are inert in the
  # generated check script.
  validRel = path:
    builtins.isString path && path != "" && !lib.hasPrefix "/" path
    && builtins.match "[A-Za-z0-9._@+=,~ %/-]+" path != null
    && builtins.all (p: p != "" && p != "." && p != "..") (lib.splitString "/" path);

  # Store-layout directory the pinned helper normalises, by pnpm major.
  # Majors outside this table are rejected: the helper would not remove
  # timestamps/projects there and the hash would not be reproducible.
  layouts = { "9" = "v3"; "10" = "v10"; "11" = "v11"; };
  vettedMajors = [ "10" "11" ]; # the variants pinned Nixpkgs itself ships

  # Registry-tarball and credential screens, shared by the lockfile check.
  # Case-insensitive (`i` flag at the call sites). Userinfo, or a credential-like query parameter.
  credentialRegex = "://[^/?#[:space:]]*@|[?&;](token|access_token|auth|authorization|password|passwd|secret|key|signature|sig|X-Amz-[A-Za-z-]*|X-Goog-[A-Za-z-]*)=";

  # Weakening/diverting flags; each matches exactly or as `flag=value`.
  forbiddenFlags = [
    "--no-frozen-lockfile" "--frozen-lockfile=false" "--no-prefer-frozen-lockfile" "--prefer-frozen-lockfile=false"
    "--fix-lockfile" "--lockfile-only" "--no-lockfile" "--lockfile=false" "--resolution-only"
    "--no-ignore-scripts" "--ignore-scripts=false"
    "--registry" "--offline" "--prefer-offline" "--no-ignore-pnpmfile" "--ignore-pnpmfile=false"
    "--pnpmfile" "--global-pnpmfile" "--global" "-g"
    "--dir" "-C" "--prefix" "--lockfile-dir" "--workspace-root" "-w" "--store-dir"
    "--config"
  ];

  # Strict YAML screen. PyYAML (behind `yq`) silently keeps the last of duplicate map keys, so a
  # second `packages:`/`resolution:` could hide an entry from the lock screen while another YAML
  # reader sees the first. Fail on duplicate keys, aliases/anchors and merge keys in every document.
  yamlStrict = ''
    import sys
    import yaml

    def walk(node, path):
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key, value in node.value:
                if not isinstance(key, yaml.ScalarNode):
                    raise SystemExit(f"non-scalar YAML key at {path or '<root>'}")
                if key.tag == "tag:yaml.org,2002:merge":
                    raise SystemExit(f"YAML merge key at {path or '<root>'}")
                if key.value in seen:
                    raise SystemExit(f"duplicate YAML key {key.value!r} at {path or '<root>'}")
                seen.add(key.value)
                walk(value, f"{path}/{key.value}")
        elif isinstance(node, yaml.SequenceNode):
            for i, item in enumerate(node.value):
                walk(item, f"{path}[{i}]")

    for name in sys.argv[1:]:
        try:
            with open(name, encoding="utf-8") as handle:
                text = handle.read()
            for event in yaml.parse(text, Loader=yaml.SafeLoader):
                if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                    raise SystemExit(f"{name}: YAML anchors/aliases are not allowed")
            docs = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
        except (yaml.YAMLError, UnicodeDecodeError) as error:
            raise SystemExit(f"{name}: {error}")
        for doc in docs:
            try:
                walk(doc, "")
            except SystemExit as error:
                raise SystemExit(f"{name}: {error}")
  '';
  yamlPython = pkgs.python3.withPackages (ps: [ ps.pyyaml ]);

  fetchPnpmCache =
    { pnpm
    , source
    , hash
    , projectFiles
    , projectDir ? "."
    , name ? "pnpm-dependency-cache"
    , fetcherVersion ? null
    , expectedPnpmVersion ? null
    , pnpmWorkspaces ? [ ]
    , pnpmInstallFlags ? [ ]
    , prePnpmInstall ? ""
    , acknowledgedUnlisted ? [ ]
    , acceptUnvettedMajor ? false
    }:
    let
      version = pnpm.version or (throw "fetchPnpmCache: `pnpm` must be a pnpm derivation with a version");
      major = lib.versions.major version;
      layout = layouts.${major} or (throw ''
        fetchPnpmCache: pnpm ${version} (major ${major}) is unsupported. Pinned Nixpkgs fetchPnpmDeps only
        normalises store directories v3, v10 and v11. Needed capability: fetchPnpmDeps must remove timestamps,
        `projects` symlinks and non-deterministic index state for this pnpm's store directory. Not guessing a recipe.'');
      effectiveFetcher = if fetcherVersion != null then fetcherVersion
        else if lib.versionAtLeast version "11" then 4 else 3;
      prefix = if projectDir == "." then "" else projectDir + "/";
      rel = file: prefix + file;
      inputsName = "pnpm-project-inputs";
      ignorePnpmfile = builtins.elem "--ignore-pnpmfile" pnpmInstallFlags;
      badFlag = builtins.filter (f: builtins.any (b: f == b || lib.hasPrefix (b + "=") f
        || lib.hasPrefix "--config." f || lib.hasPrefix "--store-dir" f
        || (lib.hasPrefix "-C" f && !lib.hasPrefix "--" f)) forbiddenFlags)
        pnpmInstallFlags;

      listed = builtins.map (f: rel f) projectFiles;
      baseIn = names: builtins.filter (f: builtins.elem (builtins.baseNameOf f) names) listed;
      configFiles = baseIn [ ".npmrc" "pnpm-workspace.yaml" ];
      yamlFiles = baseIn [ "pnpm-workspace.yaml" ];
      isListed = relPath: builtins.elem relPath listed;
      isAcknowledged = relPath: builtins.elem relPath acknowledgedUnlisted;

      # Path sources are filtered before entering the store, so unrelated files
      # (possibly credentials) are never copied. Unlisted manifests/config
      # abort evaluation instead of being dropped silently.
      imported = if builtins.isPath source then builtins.path {
        name = inputsName + "-src";
        path = source;
        filter = path: type:
          let
            relPath = lib.removePrefix ((toString source) + "/") (toString path);
            base = builtins.baseNameOf path;
          in
          if type == "directory" then !(builtins.elem base skippedDirs)
          else if isListed relPath then true
          else if builtins.elem base watchedNames && !(isAcknowledged relPath) then
            throw "fetchPnpmCache: ${relPath} exists in the source but is not in projectFiles; list it, or add it to acknowledgedUnlisted if it is not part of the install"
          else false;
      } else source;

      projectManifest = rel "package.json";
      checked = assert lib.assertMsg (builtins.all validRel (projectFiles ++ acknowledgedUnlisted))
          "fetchPnpmCache: projectFiles must be safe relative paths";
        assert lib.assertMsg (projectDir == "." || validRel projectDir)
          "fetchPnpmCache: projectDir must be \".\" or a safe relative path";
        assert lib.assertMsg (builtins.elem "package.json" projectFiles && builtins.elem "pnpm-lock.yaml" projectFiles)
          "fetchPnpmCache: projectFiles (relative to projectDir) must include package.json and pnpm-lock.yaml";
        assert lib.assertMsg (builtins.length projectFiles == builtins.length (lib.unique projectFiles))
          "fetchPnpmCache: projectFiles contains duplicates";
        assert lib.assertMsg (builtins.match "sha256-[A-Za-z0-9+/]{43}=" hash != null)
          "fetchPnpmCache: hash must be an SRI sha256 (use lib.fakeHash to discover it)";
        assert lib.assertMsg (expectedPnpmVersion == null || expectedPnpmVersion == version)
          "fetchPnpmCache: pnpm is ${version} but ${toString expectedPnpmVersion} was required; a different pnpm is never substituted";
        assert lib.assertMsg (builtins.elem major vettedMajors || acceptUnvettedMajor)
          "fetchPnpmCache: pnpm ${version} is not a variant pinned Nixpkgs ships (10, 11). Layout ${layout} is handled by the helper but unvetted here; set acceptUnvettedMajor = true to accept that";
        assert lib.assertMsg (pnpm ? nodejs-slim)
          "fetchPnpmCache: pnpm must expose passthru.nodejs-slim (use fetchPnpmTool or a Nixpkgs pnpm)";
        assert lib.assertMsg (badFlag == [ ])
          "fetchPnpmCache: pnpmInstallFlags must not weaken the frozen, script-free, network-only fetch: ${toString badFlag}";
        assert lib.assertMsg (!(lib.versionAtLeast version "11" && effectiveFetcher == 3))
          "fetchPnpmCache: fetcherVersion 3 is not supported by the helper for pnpm 11; use 4";
        assert lib.assertMsg (builtins.all (f: !(builtins.elem (builtins.baseNameOf f) pnpmfileNames) || ignorePnpmfile) projectFiles)
          "fetchPnpmCache: a pnpmfile (at any depth) would execute project code during the network fetch; omit it and pass --ignore-pnpmfile in pnpmInstallFlags (and in the offline build)";
        true;

      pnpmInputs = assert checked; pkgs.runCommand inputsName {
        nativeBuildInputs = [ pkgs.jq pkgs.yq ];
        passthru = { inherit projectFiles projectDir; };
      } ''
        set -euo pipefail
        src=${lib.escapeShellArg "${imported}"}
        dir=${lib.escapeShellArg projectDir}
        fail() { echo "fetchPnpmCache: $*" >&2; exit 1; }
        mkdir "$out"

        # Relative paths from the lock/manifests must stay inside the project: no absolute
        # path, empty, `.` or `..` segment, backslash or newline.
        safe_rel() {
          case "$1" in
            ""|/*|*$'\n'*|*\\*|.|..|./*|../*|*/.|*/..|*/./*|*/../*|*//*|*/) return 1 ;;
          esac
        }
        real_src=$(realpath -e -- "$src")

        ${lib.concatMapStringsSep "\n" (f: ''
          test -f "$src"/${lib.escapeShellArg f} || fail "listed input ${f} is missing from the source"
          test ! -L "$src"/${lib.escapeShellArg f} || fail "listed input ${f} is a symlink"
          case "$(realpath -e -- "$src"/${lib.escapeShellArg f})" in
            "$real_src"/*) ;;
            *) fail "listed input ${f} resolves outside the source" ;;
          esac
          mkdir -p "$out/$(dirname -- ${lib.escapeShellArg f})"
          cp -- "$src"/${lib.escapeShellArg f} "$out"/${lib.escapeShellArg f}
        '') listed}

        # Derivation (non-path) sources are not filtered by Nix: detect omissions here.
        ${lib.optionalString (!builtins.isPath source) ''
          while IFS= read -r f; do
            f=''${f#./}
            case "$f" in
              ${lib.concatMapStringsSep "|" lib.escapeShellArg (listed ++ acknowledgedUnlisted)}) ;;
              *) fail "$f exists in the source but is not in projectFiles/acknowledgedUnlisted" ;;
            esac
          done < <(cd "$src" && find . \( -name node_modules -o -name .git \) -prune -o \( -type f -o -type l \) \( ${
            lib.concatMapStringsSep " -o " (n: "-name ${lib.escapeShellArg n}") watchedNames
          } \) -print)
        ''}

        lock="$out/${prefix}pnpm-lock.yaml"
        base="$out/${prefix}"

        # YAML that PyYAML (behind yq) and pnpm could read differently is rejected up front.
        ${yamlPython}/bin/python3 -I -c ${lib.escapeShellArg yamlStrict} "$lock" ${lib.concatMapStringsSep " " (f: ''"$out"/${lib.escapeShellArg f}'') yamlFiles} \
          || fail "unsafe YAML (duplicate keys, aliases or anchors) in a listed lock/workspace file"

        # packageManager and devEngines.packageManager must not disagree with the pnpm we were given.
        pm=$(jq -r '.packageManager // ""' "$base/package.json")
        case "$pm" in
          pnpm@*) want=''${pm#pnpm@}; want=''${want%%+*}
            [ "$want" = ${lib.escapeShellArg version} ] || fail "package.json packageManager is $pm but pnpm ${version} was supplied" ;;
          "") ;;
          *) fail "package.json packageManager is $pm, not pnpm" ;;
        esac
        jq -r '[.devEngines.packageManager? | if type == "array" then .[] else . end | select(type == "object")]
               | .[] | "\(.name // "")\t\(.version // "")"' "$base/package.json" \
          | while IFS=$'\t' read -r dename deversion; do
              [ "$dename" = pnpm ] || fail "package.json devEngines.packageManager is '$dename', not pnpm"
              case "$deversion" in
                [0-9]*.[0-9]*.[0-9]*)
                  case "$deversion" in
                    *[!0-9.]*) ;; # a range or prerelease: not compared (documented)
                    *) [ "$deversion" = ${lib.escapeShellArg version} ] \
                         || fail "package.json devEngines.packageManager is pnpm@$deversion but pnpm ${version} was supplied" ;;
                  esac ;;
              esac
            done

        lockvers=$(yq -r '.lockfileVersion // empty' "$lock")
        [ -n "$lockvers" ] || fail "pnpm-lock.yaml has no lockfileVersion"
        while IFS= read -r lockver; do
          case "$lockver" in
            [0-9]*) ;;
            *) fail "pnpm-lock.yaml lockfileVersion '$lockver' is not numeric" ;;
          esac
          if [ "''${lockver%%.*}" -gt ${major} ]; then
            fail "lockfileVersion $lockver is newer than pnpm ${version} can read"
          fi
        done <<< "$lockvers"

        # Every workspace importer needs its manifest and a workspace file.
        yq -r '(.importers // {}) | keys[]' "$lock" | while IFS= read -r imp; do
          [ "$imp" = . ] && continue
          safe_rel "$imp" || fail "workspace importer '$imp' is not a safe relative path"
          test -f "$base/$imp/package.json" || fail "workspace importer $imp has no listed package.json"
          test -f "$base/pnpm-workspace.yaml" || fail "lockfile has importer $imp but pnpm-workspace.yaml is not listed"
        done

        # Patches referenced by the lock, package.json or workspace file must be safe and listed.
        {
          yq -r '(.patchedDependencies // {}) | .[] | .path? // empty' "$lock"
          jq -r '(.pnpm.patchedDependencies // {}) | .[]' "$base/package.json"
          if [ -f "$base/pnpm-workspace.yaml" ]; then
            yq -r '(.patchedDependencies // {}) | .[]' "$base/pnpm-workspace.yaml"
          fi
        } | while IFS= read -r patch; do
          safe_rel "$patch" || fail "patch '$patch' is not a safe relative path"
          test -f "$base/$patch" || fail "patch $patch is referenced but not listed in projectFiles"
        done

        # Only hashed http(s) tarballs; fail closed on Git, directory, file: and credentialed URLs.
        bad=$(yq -r '(.packages // {}) | to_entries[]
          | select((.value.resolution.integrity? // null) == null
              or (.value.resolution.type? // null) != null
              or (.value.resolution.directory? // null) != null
              or (((.value.resolution.tarball? // "") | (. != "" and (test("^https?://") | not))))
              or (((.value.resolution.tarball? // "") | test("${credentialRegex}"; "i"))))
          | .key' "$lock" | sed -n '1,20p')
        if [ -n "$bad" ]; then
          echo "$bad" >&2
          fail "lockfile entries above are not credential-free hashed registry tarballs (Git/directory/file/private sources are unsupported)"
        fi

        # Config files at any depth: no credentials, interpolation, URL userinfo/tokens, or
        # settings that redirect the store or other config.
        ${lib.concatMapStringsSep "\n" (f: ''
          rc="$out"/${lib.escapeShellArg f}
          if grep -Eiq '_authtoken|_auth([^a-z]|$)|_password|token-?helper|^[[:space:]]*["'"'"']?(username|email|password|token)["'"'"']?[[:space:]]*[=:]|\$\{' "$rc" \
             || grep -Eiq ${lib.escapeShellArg credentialRegex} "$rc"; then
            fail "${f} appears to contain credentials, URL userinfo/tokens or environment interpolation"
          fi
          if grep -Eiq '^[[:space:]]*["'"'"']?(store-?dir|userconfig|globalconfig|offline|prefer-?offline)["'"'"']?[[:space:]]*[=:]' "$rc"; then
            fail "${f} redirects the store/config or forces offline mode, which would make the fetch non-hermetic"
          fi
          ${lib.optionalString (!ignorePnpmfile) ''
            if grep -Eiq 'pnpmfile' "$rc"; then
              fail "${f} configures a pnpmfile, which would run during the fetch; use --ignore-pnpmfile"
            fi
          ''}
        '') configFiles}
      '';

      deps = pkgs.fetchPnpmDeps ({
        inherit hash pnpm pnpmWorkspaces pnpmInstallFlags prePnpmInstall;
        pname = name;
        fetcherVersion = effectiveFetcher;
        src = pnpmInputs;
        # The helper runs in the unpacked store path named after the inputs.
      } // lib.optionalAttrs (projectDir != ".") {
        sourceRoot = "${inputsName}/${projectDir}";
      });
    in deps.overrideAttrs (old: {
      passthru = (old.passthru or { }) // {
        inputs = pnpmInputs;
        mangoPnpm = {
          pnpmVersion = version;
          pnpmMajor = major;
          nodejsVersion = pnpm.nodejs-slim.version or "unknown";
          fetcherVersion = effectiveFetcher;
          expectedStoreLayout = layout;
          vettedMajor = builtins.elem major vettedMajors;
          inherit projectDir projectFiles pnpmWorkspaces pnpmInstallFlags;
          helper = "pkgs.fetchPnpmDeps";
          storeArchive = "pnpm-store.tar.zst";
          metadataEquivalence = "not claimed; requires a real offline build of the original target";
        };
      };
    });

  # Exact npm release tarball for a pinned pnpm, packaged by the Nixpkgs recipe.
  fetchPnpmTool = { version, hash, nodejs ? pkgs.nodejs-slim }:
    assert lib.assertMsg (builtins.match "[0-9]+\\.[0-9]+\\.[0-9]+" version != null)
      "fetchPnpmTool: version must be an exact release such as 10.17.1";
    assert lib.assertMsg (builtins.match "sha256-[A-Za-z0-9+/]{43}=" hash != null)
      "fetchPnpmTool: hash must be an SRI sha256 of the registry tarball";
    pkgs.callPackage (pkgs.path + "/pkgs/development/tools/pnpm/generic.nix") {
      inherit version hash;
      nodejs = null;
      nodejs-slim = nodejs;
    };

  # Fresh writable store copy for the offline build of the original target.
  preparePnpmStore = pkgs.writeShellApplication {
    name = "prepare-pnpm-store";
    runtimeInputs = [ pkgs.coreutils pkgs.gnutar pkgs.zstd pkgs.sqlite ];
    text = ''
      if [ "$#" -ne 2 ]; then
        echo "usage: prepare-pnpm-store PNPM_CACHE NEW_STORE_DIRECTORY" >&2
        exit 2
      fi
      if [ -e "$2" ] || [ -L "$2" ]; then
        echo "pnpm store destination already exists: $2" >&2
        exit 2
      fi
      if [ ! -f "$1/pnpm-store.tar.zst" ] || [ ! -f "$1/.fetcher-version" ]; then
        echo "not a fetchPnpmCache output: $1" >&2
        exit 2
      fi
      mkdir -p -- "$2"
      tar --zstd -xf "$1/pnpm-store.tar.zst" -C "$2"
      chmod -R u+w -- "$2"
      # fetcherVersion 4 stores the pnpm 11 index as an SQL dump (as pnpmConfigHook does).
      if [ -f "$2/v11/index.db.sql" ]; then
        sqlite3 "$2/v11/index.db" < "$2/v11/index.db.sql"
        rm "$2/v11/index.db.sql"
      fi
    '';
  };
in {
  inherit fetchPnpmCache fetchPnpmTool preparePnpmStore;
}
