# Reuse an exact pinned upstream image

Use `pinnedImage` when a service needs an upstream image unchanged:

```nix
let h = import harness {}; in h.pinnedImage {
  image = "docker.io/library/alpine";
  digest = "sha256:bc21487101ae47172509130ee1092cc273a831968d64b617776d1f69c81e42ab";
  hash = "sha256-VJrEWNzP7YSUs7FXJ6UIbPd7YlPrIGXCxJ2UAbL9iCc=";
}
```

The arguments are the same immutable repository/digest and content hash used by
`fetchLinuxBase`. `hash` is the recursive NAR hash of that helper's skopeo directory
output, **not** a manifest digest or a nix2container `pullImage` hash. The existing
Linux/amd64 platform restriction and pin validation apply. Tags are never resolved:
the remote reference includes the supplied digest.

The result is an OCI-layout directory usable directly in `mkEnvironment.containers`
and by `nix2rle fetch` / `load`. It also exposes `.base`, the original fixed-output
`fetchLinuxBase` directory. No VM, build command, empty layer or image configuration
normalization is involved.

The helper validates SHA-256 digests and sizes of every referenced config/layer
blob and symlinks those bytes into the OCI blob directory. It retains the original
manifest bytes and media type, including Docker schema-2 manifests. Only the
outer `oci-layout` and single-image `index.json` packaging documents are new.
Image history, runtime config, compressed layers and rootfs diff IDs remain exact.
The Nix closure retains the original blobs without duplicating their bytes.

A requested digest may identify a **multi-platform index**. `fetchLinuxBase`
selects its Linux/amd64 manifest, so the loaded image digest is that selected
manifest's digest, not necessarily the requested index digest. The helper does
not recreate the original multi-platform index or package other platforms.

Use `mkContainer` instead when changing files, packages or runtime configuration;
those changes necessarily produce a different image. Native source compilation
and distro installers remain the job of explicit `mkLinuxImage` stages.
