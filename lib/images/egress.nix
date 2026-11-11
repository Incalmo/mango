{ pkgs }:
let
  n2c = import ../pins/nix2container.nix { inherit pkgs; };
  base = n2c.pullImage {
    imageName = "docker.io/gogost/gost";
    # 3.2.7-nightly.20260602, linux/amd64
    imageDigest = "sha256:afc0137758ab4ce399d47a299f9abbacbf522b52a17e59cbb4b4e7a1a66e9196";
    sha256 = "sha256-yS7MqosWVBHKYTP339O48lgPmLjjhrweDcamxWkdoL4=";
    arch = "amd64";
  };
  files = pkgs.runCommand "harbor-egress-files" {} ''
    mkdir -p $out/opt/egress-sidecar $out/usr/local/bin
    cp ${./egress}/gost.yaml ${./egress}/allowlist.txt $out/opt/egress-sidecar/
    cp ${./egress}/entrypoint.sh ${./egress}/*rules ${./egress}/private-endpoints $out/opt/egress-sidecar/
    cp ${./egress}/bin/network-policy $out/usr/local/bin/
    chmod +x $out/opt/egress-sidecar/entrypoint.sh $out/opt/egress-sidecar/*rules $out/opt/egress-sidecar/private-endpoints $out/usr/local/bin/network-policy
  '';
in (import ./mk-image.nix { inherit pkgs; }) {
  name = "egress";
  fromImage = base;
  packages = [ pkgs.iproute2 ];
  root = [ files ];
  config = { Entrypoint = [ "/opt/egress-sidecar/entrypoint.sh" ]; Env = [ "PATH=/usr/local/bin:/bin:/sbin:/usr/bin:/usr/sbin" ]; };
}
