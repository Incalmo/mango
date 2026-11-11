# Hosted builds

A hosted service receives the environment source tree, builds it once,
and runs many trials from the result. The split keeps Nix on builders only:

1. **Builder.** With the source snapshot extracted at `ENV`:

   ```sh
   nix-shell "$HARNESS/shell.nix" --run "mango '$ENV' \
     --workspace '$WORK' publish --repository '$REPO/$BUILD' \
     --build-id '$BUILD' --source-digest '$DIGEST' --output manifest.json"
   ```

   This evaluates and realises every image and build output, then pushes each
   image to `$REPO/$BUILD/<container>` with skopeo. Manifest bytes are pushed
   unchanged (`--preserve-digests`), and the registry's digest must equal the
   layout's. No Docker daemon or containerd store is involved. Registry
   credentials come from skopeo's usual configuration or `--authfile`.

   Run `build` first (same arguments, no publish options) to realise every
   output without pushing; `publish` then only pushes, so a registry admission
   slot never covers a build. Unlike `fetch`, `build` records no offline receipt.

   Failures exit with a classified status and print a final stderr line
   `{"error": "<stage>", "message": "..."}`:

   | Status | Stage | Meaning |
   | --- | --- | --- |
   | 10 | `evaluation` | Declaration or derivation evaluation failed; permanent |
   | 11 | `build` | An offline build failed; permanent for these inputs |
   | 12 | `input-download` | A pinned input failed to transfer (not a hash mismatch); retryable |
   | 13 | `push` | Registry push failed; retryable |

   Registry credentials: pass `--authfile`, or rely on skopeo's default lookup
   (`$XDG_RUNTIME_DIR/containers/auth.json`, then `~/.docker/config.json`,
   whose `credHelpers` entries such as `docker-credential-gcr` are honoured).
   `nix-shell` keeps `HOME`, so the builder's Docker config applies.

2. **Manifest.** The printed `BuildManifest` (recipe version 3) maps services,
   each separate grader variant and the egress sidecar to `repo@sha256:` image
   references. Its `source_digest` is the caller-supplied snapshot digest, and
   its `runtime` field is the evaluated `RuntimeDeclaration`: the task
   directory, every Compose document, environment files and executable files.

3. **Worker.** Given the same source tree and the manifest, a worker needs only
   Python and Docker:

   ```python
   from mango.runtime.task import generate_task
   from mango.runtime.materialize import materialize_runtime

   generate_task(source, manifest.runtime, task)   # copy task/, write Compose
   materialize_runtime(task, manifest, digest)       # bind digest images
   ```

   The generated `task` is an ordinary image-only Harbor task. Use the
   prebuilt-image environment adapter so Harbor pulls instead of building.

`src/mango/runtime/` (`task.py`, `materialize.py`, `manifest.py`) has no Nix
or Harbor dependency: a hosted worker installs or vendors only that package.
Keep vendored copies byte-compatible apart from their import lines.

Native `mkLinuxImage` stages run in a QEMU VM. Builders need `/dev/kvm`
(on GCE, nested virtualization) for practical build times, Nix sandboxing,
and a persistent store or binary cache shared across builds.
