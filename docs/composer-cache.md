# Composer cache inputs

`dependencies.fetchComposerCache { lock; name ? "composer-cache"; }` turns an
explicit `composer-cache` lock into a read-only directory of pinned archives at the
exact paths Composer used in its cache dir (`COMPOSER_CACHE_DIR`), under `files/`.
It does not bundle `vendor/`, and it never runs Composer or PHP in the Nix host:
the original install is replayed later with the original toolchain.
Status: evaluation-only tests; no real fetch or offline build has been run, and
nothing here is end-to-end qualified.

## Lock

```json
{
  "schema_version": 1,
  "kind": "composer-cache",
  "artifacts": [
    { "path": "files/vendor/pkg/1.2.3.0-abcdef.zip",
      "urls": ["https://api.github.com/repos/vendor/pkg/zipball/abcdef"],
      "sha256": "<64 hex>" }
  ],
  "unmapped_files": []
}
```

Extra keys beyond the above, other kinds or schema versions fail.
Rules, enforced at evaluation:

- `path`: `files/` followed by safe segments (`A-Za-z0-9._+@=,~-`); no `.`/`..`,
  empty segments, absolute paths or shell-special characters. Paths must be unique
  and no path may be a directory prefix of another. Artifacts are regular fetched
  files, so symlinks cannot occur.
- `urls` and `sha256`: checked by the shared `fetchDependency` helper (HTTP(S), no
  userinfo or credential query parameters) plus exactly one 64-hex SHA-256.
- `unmapped_files`: cache files that are not locked archives (for example metadata)
  are **not supported yet**; a non-empty list fails closed. Explicit pinned file
  inputs with exact path and SHA may be added later.

## Replaying the original install

Inside the same prepared toolchain (PHP, extensions, Composer version) recorded when
the lock was authored, with the network off:

1. Copy the fetched cache to a fresh **writable** directory (see
   `prepareCache`); Composer writes into its cache, and `/nix/store` is read-only.
2. Set `COMPOSER_CACHE_DIR` to that copy and use a fresh `HOME`/`COMPOSER_HOME`.
3. Run the original `composer install ...` argv in the original working directory
   with the original `composer.json` and `composer.lock`.
4. `COMPOSER_DISABLE_NETWORK=1` is optional; use it only if authoring also did,
   so the offline replay matches the authoring conditions.

The adapter never substitutes a Composer or PHP version.

## Limits

SHA-256 values prove only that the archive bytes are the ones observed when the lock
was authored. They do not show that the platform, PHP version, extensions or Composer
are right for the build, nor that the install succeeds offline; only the real
replay shows that. Source-only, VCS or path packages, authenticated repositories and
byte-unstable URLs (such as zipball endpoints that change bytes) are outside this
adapter and should be rejected when the lock is authored.
