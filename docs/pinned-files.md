# Serve explicit pinned files during an offline build

Some package managers require HTTP URLs even when every input is already in the
Nix store. `h.pinnedFiles` serves an authored map of URLs to local files on IPv4
loopback while running one authored command. It neither resolves dependencies nor
contacts an upstream server. It does not create package metadata.

```nix
let
  h = import harness {};
  files = h.dependencies.fetchLockedInputs { lock = ./inputs.lock.json; };
  map = h.pkgs.writeText "registry-map.json" (builtins.toJSON {
    "/@scope/package/-/package-1.0.0.tgz" = {
      file = "${files}/npm/@scope/package/-/package-1.0.0.tgz";
    };
    # Only if required: an explicitly pinned, authored metadata document.
    "/@scope/package" = {
      file = "${files}/metadata/package.json";
      content_type = "application/json";
    };
  });
in h.pkgs.runCommand "application" { nativeBuildInputs = [h.pinnedFiles]; } ''
  incalmo-pinned-files --map ${map} -- sh -c '
    export npm_config_registry="$INCALMO_PINNED_FILES_URL"
    exec your-authored-offline-build-command
  '
''
```

This illustrative fragment leaves the package manager, source, build command,
and output installation to the author. The files must be pinned by the caller's
Nix input graph and immutable during the command. The server validates the map
and file existence; it does not establish download provenance or substitute for
fixed-output hashes and the package manager's own frozen-lock checks.

## Native distro builds

The script uses only Python's standard library (Python 3.9+). `h.pinnedFilesScript`
is the portable script file; `h.pinnedFiles` packages it with Nix's Python for
ordinary Nix derivations. For `mkLinuxImage`, explicitly mount the script and map
as inputs, then invoke the base image's Python:

```nix
inputs = h.pkgs.linkFarm "native-inputs" [
  { name = "files"; path = files; }
  { name = "serve.py"; path = h.pinnedFilesScript; }
  { name = "map.json"; path = h.pkgs.writeText "native-map.json"
      (builtins.toJSON {
        "/package/-/package-1.0.0.tgz".file =
          "/.nix-inputs/files/files/npm/package/-/package-1.0.0.tgz";
      }); }
];
buildScript = ''
  python3 /.nix-inputs/files/serve.py \
    --map /.nix-inputs/files/map.json -- sh -c '
      export npm_config_registry="$INCALMO_PINNED_FILES_URL"
      exec your-authored-offline-build-command
    '
'';
```

The native base must already supply Python, or the author must explicitly install
pinned Python packages. The helper does not inject a host interpreter into the
image. No HTTP endpoint is exposed outside the native VM's loopback interface.
The surrounding native build remains without an external network interface.

## URL and lifecycle contract

- The map is a JSON object: decoded absolute URL path to `{file, content_type?}`.
  `file` must be an existing absolute regular file. Explicit symlinks to pinned
  files are allowed. Directories, implicit index files and directory listings
  are unsupported. Duplicate map keys are rejected.
- Wire paths are percent-decoded **once**, using strict UTF-8. For example,
  `/@scope%2fpackage` selects map key `/@scope/package`. To select a map key
  literally containing `%2f`, request `%252f`. Map keys are never decoded.
- Empty/dot path components, backslashes, controls, absolute-form URLs, malformed
  escapes, fragments and **all query strings** are rejected. Queries are never
  silently discarded. Authors must use stable mapped URLs.
- Only GET and HEAD are supported. Content defaults to `application/octet-stream`;
  map a pinned metadata file with `content_type = "application/json"` when needed.
- The listener is ready before the child starts. An ephemeral port is the default;
  `--port N` requests a fixed port. Use an authored fixed port in an isolated
  native VM when metadata contains absolute URLs or a package manager saves the
  endpoint in its output; random ports would then harm reproducibility. The child receives `INCALMO_PINNED_FILES_URL`
  without a trailing slash. Parallel invocations can use independent default ports.
- A missing request reports `not in pinned file map: METHOD PATH`. Missing,
  invalid, unsupported or failed requests make the wrapper fail even if the child
  exits zero. A child failure retains its exit status. No fallback downloads occur.
- On exit or SIGINT/SIGTERM, the server closes and the child process group is
  terminated. Commands should run their work in the foreground and must not
  detach into new sessions. The helper cleans up remaining children in its group.

Do not bake a registry endpoint, generated `.npmrc`, or package-manager cache into
runtime layers accidentally. Scope settings to the authored build command and
choose explicitly which built artifacts enter the final image.
