{ pkgs }:
let
  lib = pkgs.lib;
  python = pkgs.python313;
  runner = "${builtins.path {
    path = ../../builders;
    name = "linux-image-runner";
    filter = path: type: builtins.elem (baseNameOf path) [ "builders" "rootfs.py" "artifacts.py" ];
  }}/rootfs.py";
in {
  fetchLinuxBase = { image, digest, hash, name ? "linux-base" }:
    assert lib.assertMsg (pkgs.stdenv.hostPlatform.system == "x86_64-linux")
      "fetchLinuxBase currently supports x86_64-linux only";
    assert lib.assertMsg (builtins.match "sha256:[0-9a-f]{64}" digest != null)
      "Linux base images require an immutable SHA-256 digest";
    assert lib.assertMsg (hash != "") "Linux base images require a content hash";
    pkgs.runCommand name {
      nativeBuildInputs = [ pkgs.skopeo pkgs.cacert ];
      impureEnvVars = lib.fetchers.proxyImpureEnvVars;
      outputHashMode = "recursive";
      outputHash = hash;
      outputHashAlgo = null;
      preferLocalBuild = false;
    } ''
      skopeo --insecure-policy copy --retry-times 3 --override-os linux --override-arch amd64 \
        ${lib.escapeShellArg "docker://${image}@${digest}"} dir:$out
    '';

  # The author supplies shell commands and input files. No Dockerfile or package
  # metadata is discovered or interpreted. The original distro's /bin/sh and
  # installers run as root with no network device, either in a QEMU VM
  # (isolation = "vm", needs the `kvm` system feature for practical speed) or in
  # the Nix sandbox itself under a full user-namespace uid range
  # (isolation = "namespace", needs the `uid-range` system feature: Nix's
  # `auto-allocate-uids`; no VM and no KVM). Both run the same rootfs.py build
  # and produce the same layer.
  mkLinuxImage = { name, base, inputs ? pkgs.emptyDirectory, context ? pkgs.emptyDirectory
    , buildScript, buildEnv ? {}, config ? {}, diskSize ? 16384, memSize ? 4096
    , sourceDateEpoch ? 1, exportPaths ? [], cores ? 1, isolation ? "vm" }:
    assert lib.assertMsg (pkgs.stdenv.hostPlatform.system == "x86_64-linux")
      "mkLinuxImage currently supports x86_64-linux only";
    assert lib.assertMsg (builtins.isList exportPaths && builtins.all builtins.isString exportPaths)
      "mkLinuxImage exportPaths must be a list of absolute paths";
    assert lib.assertMsg (builtins.isInt cores && cores >= 1 && cores <= 255)
      "mkLinuxImage cores must be an integer from 1 to 255";
    assert lib.assertMsg (builtins.elem isolation [ "vm" "namespace" ])
      "mkLinuxImage isolation must be \"vm\" or \"namespace\"";
    let
      # Stable component names let script-only edits reuse large byte inventories.
      materialize = source: pkgs.runCommand "linux-build-inputs" {} ''
        cp -rL ${source} $out
      '';
      staged = pkgs.linkFarm "${name}-build-inputs" [
        { name = "files"; path = materialize inputs; }
        { name = "context"; path = materialize context; }
        { name = "build.sh"; path = pkgs.writeText "build.sh" buildScript; }
      ];
      plan = pkgs.writeText "${name}-build-plan.json" (builtins.toJSON {
        requireOffline = true;
        inherit buildEnv sourceDateEpoch exportPaths;
        steps = [{ op = "RUN"; cmd = "/bin/sh /.nix-inputs/build.sh"; }];
      });
      vm = pkgs.vmTools.runInLinuxVM (pkgs.runCommand "${name}-layer" {
        # vmTools otherwise appends a host/NIX_BUILD_CORES-derived -smp.
        enableParallelBuilding = false;
        QEMU_OPTS = "-serial file:xchg/build-console.log"
          + lib.optionalString (cores != 1) " -smp cpus=${toString cores}";
        preVM = pkgs.vmTools.createEmptyImage {
          size = diskSize;
          fullName = "${name}-disk";
          destination = "$TMPDIR/scratch";
        };
        inherit memSize;
        nativeBuildInputs = [ python pkgs.e2fsprogs pkgs.util-linux pkgs.iproute2 ];
      } ''
        mkfs.ext4 -q /dev/${pkgs.vmTools.hd}
        mkdir -p /mnt
        mount /dev/${pkgs.vmTools.hd} /mnt
        python ${runner} build --base ${base} --plan ${plan} \
          --inputs ${staged} --root /mnt/rootfs --out $out
      '');
      # Keep serial output separate from QEMU's monitor, then report it even
      # when vmTools aborts before its postVM hook (for example on build failure).
      vmLayer = lib.overrideDerivation vm (old: {
        args = [ "-c" ''
          ${lib.escapeShellArg old.builder} ${lib.escapeShellArgs old.args}
          status=$?
          if [ -f xchg/build-console.log ]; then
            # Nix treats carriage returns as progress-line replacement. Serial
            # CRLF otherwise erases each message before it reaches the log.
            ${pkgs.coreutils}/bin/tr -d '\r' < xchg/build-console.log
          fi
          exit "$status"
        '' ];
      });
      # The sandbox is the isolation: a private network namespace with only
      # loopback, and uid 0 mapped over a 65536-uid range so installers can
      # chown to their service users.
      namespaceLayer = pkgs.runCommand "${name}-layer" {
        requiredSystemFeatures = [ "uid-range" ];
        nativeBuildInputs = [ python pkgs.util-linux pkgs.iproute2 ];
      } ''
        python ${runner} build --base ${base} --plan ${plan} \
          --inputs ${staged} --root "$TMPDIR/rootfs" --out $out
      '';
      layer = if isolation == "namespace" then namespaceLayer else vmLayer;
      artifacts = pkgs.runCommand "${name}-artifacts.tar" {} ''
        cp ${layer}/artifacts.tar $out
      '';
      imageConfig = pkgs.writeText "${name}-image-config.json" (builtins.toJSON config);
    in pkgs.runCommand "image-${name}" {
      nativeBuildInputs = [ python ];
      passthru = { inherit base layer inputs; inputMounts = staged; } // lib.optionalAttrs (exportPaths != []) { inherit artifacts; };
    } ''
      python ${runner} assemble --base ${base} --layer ${layer} --config ${imageConfig} --out $out
    '';
}
