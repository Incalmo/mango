# Composer cache inputs

`dependencies.fetchComposerCache { lock; name ? "composer-cache"; }` turns an
explicit `composer-cache` lock into a read-only directory of pinned archives at the
exact paths Composer used in its cache dir (`COMPOSER_CACHE_DIR`), under `files/`,
plus the few cache files Composer writes itself (see "Manager-generated files").
It does not bundle `vendor/`, and it never runs Composer or PHP in the Nix host:
the original install is replayed later with the original toolchain.

Status: the Nix adapter is covered by evaluation-only tests and the authoring
helpers by mocked tests. A real offline replay that includes the generated
`.htaccess` has **not** been run yet, so nothing here is end-to-end qualified.

## Authoring tools

Both run on the remote authoring host only (`MANGO_REMOTE_AUTHOR=1`, Docker there);
neither is needed to build an environment.

| Module | Command | Purpose |
| --- | --- | --- |
| `mango.authoring.php_prefix` | `python -m mango.authoring.php_prefix prepare\|extract-composer` (`mango-prepare-php`) | Prepare the PHP toolchain: apt state, then the original post-apt `docker-php-ext-*`/`a2enmod` commands with `--network none`, committed to an authoring-only image with a verified receipt. `extract-composer` copies the PHAR out of the digest-pinned composer image without running it. |
| `mango.authoring.composer_lock` | `python -m mango.authoring.composer_lock REQUESTS --output RESULTS` (`mango-lock-composer`) | Run the original `composer install` (download-only, plugins and scripts off) in that prepared image, bind the cache to the locked archives by bytes, then replay the original command in a `--network none` container. |
| `mango.authoring.remote_common` | (library) | Shared gate, redaction, hashing, bounded https download and subprocess helpers. |

The prepared image is an **authoring-only cache**. The final image still runs the
original Dockerfile commands (the same apt install, extension build and
`composer install`); nothing from the prefix image or from `vendor/` is shipped.
`prepare` refuses to run without `MANGO_REMOTE_AUTHOR=1`, and the Composer resolver
runs only inside containers with the pinned prepared image ID, 1.5 CPUs, 4 GB memory,
no capabilities, no mounts and a fresh `HOME`/`COMPOSER_HOME`/cache. Archive and cache
sizes, archive counts and timeouts are bounded (`limits`); every stage writes a receipt
with image IDs, PHP/Composer versions, the `php -m` module hash and the cache inventory.
`status: completed` is not verification: only `verification: offline-replay-verified`,
produced by a real Docker `--network none` replay, counts, and mocks can never reach it.

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
  "generated_files": [
    { "path": ".htaccess", "content": "Deny from all", "sha256": "<64 hex>" }
  ],
  "unmapped_files": []
}
```

`generated_files` is optional (locks without it are unchanged). Extra keys beyond the
above, other kinds or schema versions fail. Rules, enforced at evaluation:

- `path`: `files/` followed by safe segments (`A-Za-z0-9._+@=,~-`); no `.`/`..`,
  empty segments, absolute paths or shell-special characters. Paths must be unique
  and no path may be a directory prefix of another. Artifacts are regular fetched
  files, so symlinks cannot occur. Artifact URLs are only ever real archive URLs
  under `files/...`.
- `urls` and `sha256`: checked by the shared `fetchDependency` helper (HTTP(S), no
  userinfo or credential query parameters) plus exactly one 64-hex SHA-256.
- `unmapped_files`: other cache files that are not locked archives are **not
  supported**; a non-empty list fails closed.

## Manager-generated files

Composer itself writes `.htaccess` (`Deny from all`) into its cache, home and data
directories (`Composer/Factory.php`, checked in the pinned 2.8.12 PHAR). Because it is
part of the cache the author run produced, the offline replay must see exactly the
same file, so it is locked rather than ignored:

- the closed set is a single path, `.htaccess`, with exactly the 13 ASCII bytes
  `Deny from all` (no newline) and its SHA-256. Any other path, content, hash, extra
  key, duplicate, or collision with an archive path is rejected, by the authoring
  helper (`generated-file-mismatch` / `invalid-generated-file`) and again by the Nix
  adapter. It cannot be listed as an unmapped file.
- the content is inline in the lock; there is no URL for it. The adapter emits it as
  a metadata file next to the fetched `files/` tree.
- the author phase captures it from the cache produced by the `--download-only
  --no-plugins --no-scripts` run; the replay seeds both the verified archives and this
  exact file, and the receipt's cache inventory lists it with kind `generated`.
- a cache without `.htaccess` simply has no `generated_files`.

## Replaying the original install

Inside the same prepared toolchain (PHP, extensions, Composer version) recorded when
the lock was authored, with the network off:

1. Copy the fetched cache, including `.htaccess`, to a fresh **writable** directory;
   Composer writes into its cache, and `/nix/store` is read-only. Re-verify the exact
   file set and every SHA-256.
2. Set `COMPOSER_CACHE_DIR` to that copy and use a fresh `HOME`/`COMPOSER_HOME`.
3. Run the original `composer install ...` argv in the original working directory
   with the original `composer.json` and `composer.lock`. Mango's translated build
   keeps the original `RUN` text and only prepends guards.
4. No extra Composer variables are needed: the replay container has no network
   (`--network none`), which is what the helper verified.

The adapter never substitutes a Composer or PHP version.

## Supported subset and special cases

- `composer install` of a current `composer.lock`, `--prefer-dist` archives of type
  zip/tar over https, no credentials, bounded sizes. `--no-dev` and the autoload flags
  are honoured; `update`, `require`, `create-project`, source installs, `-d`,
  `--no-cache` and positional packages are refused.
- Dev versions are accepted only as an immutable GitHub dist: a lower-case 40-hex
  commit where the lock's source and dist references agree, the repository is declared
  in `composer.json` as an exact public `https://github.com/<owner>/<repo>` VCS
  repository, and the dist URL is the `api.github.com` zipball of that same repo and
  commit. The VCS repository is never cloned; only the locked archive is used.
- GitHub zipball bytes are not guaranteed stable. They are compared once, at
  authoring, with the bytes Composer cached; a difference fails with
  `unstable-dist-bytes`.

## Limits

SHA-256 values prove only that the archive bytes are the ones observed when the lock
was authored. They do not show that the platform, PHP version, extensions or Composer
are right for the build, nor that the install succeeds offline; only the real
replay shows that. Unsupported and rejected: source-only, path, artifact and custom
or private repositories, authenticated repositories, credentials in config, other
VCS hosts, mutable dev versions, plugins that need the network, extra
manager-generated or unexplained cache files, and byte-unstable URLs. Composer plugins
and scripts run only in the offline replay. Unauthenticated GitHub API rate limits may
stop large locks (`download-failed`).
