# Explicit pinned task sources

Keep large original task assets out of recipe history. A recipe may track a
`task-source.json` descriptor while ignoring its materialized `task/` directory.
The harness accepts a locally supplied original `.tar.zst` archive; it does not
fetch a URL, resolve dependencies, discover installers or upload anything.

The descriptor has exactly these fields:

- `schema_version`: `2`.
- `archive_sha256`: lowercase SHA-256 of the original compressed archive bytes.
- `registered_digest`: `sha256:` followed by the registered canonical task hash.
- `canonical`: a nonempty list of rules `{ "header": LINE, "exclude": [GLOB, ...] }`
  describing the registry's canonical manifest format.

```json
{
  "schema_version": 2,
  "archive_sha256": "<64 hex>",
  "registered_digest": "sha256:<64 hex>",
  "canonical": [
    { "header": "example-manifest-v2", "exclude": ["metadata.json", "notes/*"] }
  ]
}
```

Copy the archive hash and registered digest from the verified registration or
download receipt, and the rules from the registry's packaging specification. Do not use
signed download URLs or credentials in source descriptors. Keep provenance and
harness/recipe revisions in the separate qualification records.

From the harness checkout, materialize before running `nix2rle validate`:

```sh
nix-shell --run 'python -m nix2rle.authoring.task_source materialize --archive "$ARCHIVE" --descriptor "$ENVIRONMENT/task-source.json" --out "$ENVIRONMENT/task"'
nix-shell --run 'python -m nix2rle.authoring.task_source verify --descriptor "$ENVIRONMENT/task-source.json" --task "$ENVIRONMENT/task"'
```

The first command snapshots and hashes the supplied archive, extracts into a
private sibling temporary directory, verifies its canonical digest, and publishes
with an atomic no-replace rename. An existing destination (including an empty
directory or symlink) is an error. Concurrent destination creation is also an
error. Failure removes temporary files and leaves the destination unpublished.
This command currently requires Linux with `renameat2` support.

File bytes, ordinary permission bits, mtimes and relative symlinks are preserved. Unsafe
paths, duplicate entries, writes through symlink ancestors, escaping/absolute
symlinks, hardlinks, devices, FIFOs and special permission bits are rejected.
Registered task archives normalize ownership and permissions; arbitrary rootfs
archives are not this format. The command never overwrites or edits the original
archive or an existing task tree.

Canonical verification hashes every file and symlink in the extracted snapshot
as sorted lines `<kind> <mode> <size> <sha256> <path>` (regular files keep only
the owner-executable bit, as `755` or `644`; symlinks hash their target). Each
rule's digest is the SHA-256 of its header line followed by the lines of every
path that matches none of its `exclude` globs (`fnmatch` against the
task-relative path), each line newline-terminated. Verification passes when any
rule reproduces `registered_digest`; list several rules for a registry whose
format changed over time. The harness applies no ignore rules of its own.
It is not an authoring packager. The archive hash binds all original bytes;
canonical verification alone does not bind metadata excluded by the registered
format. Preserve the original archive for full-byte identity review.

## Reuse through the Nix store

`h.requireTaskArchive { sha256 = archiveHash; }` names a fixed-output store file
`task-<archiveHash>.tar.zst`. Add the explicitly supplied archive with that basename:

```sh
# NAME must be task-<the authored archive_sha256>.tar.zst.
cp -- "$ARCHIVE" "$STAGING/$NAME"
nix-store --add-fixed sha256 "$STAGING/$NAME"
```

The helper returns the same store object, or restores it from a configured binary
cache. If missing, it fails with instructions to add the original file. Its
result can be passed as the materializer's `--archive` argument. There are no
embedded URLs, credentials or implicit remote fetches in the helper.

Identical archive hashes and names reuse one Nix store object. Different archives
containing some identical installers are still separate archive objects; this
interface does not automatically deduplicate bytes inside them. Authors can
explicitly pin reusable installers separately as normal Nix inputs, without
changing the immutable original task archive or scanning it for dependencies.

A customer bundle must include the original archive plus descriptor, or the
materialized task tree. The current `nix2rle export` copies a materialized task;
materialize it first. A bare descriptor is not a distributable environment.
Allow disk space for the original archive, temporary archive snapshot and
extracted tree during materialization. Qualification must still compare the
exact registered task and run all runtime gates.
