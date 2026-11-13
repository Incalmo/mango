# Remote binary caches

Cache access is split between two files with two owners:

- **The environment** names the caches it can be built from, in `mango.toml`
  beside its `default.nix` and `mango.lock`. This file holds no credentials
  and cannot ask for any.
- **The user** says how to authenticate to each cache, in their own
  credentials file. An environment therefore cannot direct a secret to a host
  the user did not choose.

The Mango launcher reads both before resolving the pinned harness and keeps the
caches available throughout `mango load`, `fetch`, `build`, and other commands.
Both command-first and legacy environment-first invocations support them.
Command-first invocations use the [managed builder](managed-builder.md) by
default, so the customer's host Nix daemon does not control cache trust.
The harness revision in `mango.lock` does not need to change to enable cache
access.

## The environment: `mango.toml`

```toml
[[cache]]
url = "https://YOUR-CACHE.example.com"
public_key = "YOUR-CACHE-NAME:YOUR-BASE64-PUBLIC-KEY"
```

Repeat `[[cache]]` for more caches. URLs must be HTTPS origins without
credentials, query strings or paths. The public key is the cache's Nix narinfo
signing key, **not** a token-issuer key. No other settings are accepted.

## The user: `credentials.toml`

Mango reads `~/.config/mango/credentials.toml` (under `$XDG_CONFIG_HOME` when
set), or the file named by `MANGO_CREDENTIALS`. Each table is a cache origin:

```toml
["https://YOUR-INTERNAL-CACHE.run.app"]
auth = "google"

["https://YOUR-PUBLIC-CACHE.run.app"]
auth = "bearer"
token_env = "INCALMO_NIX_CACHE_TOKEN"
```

| `auth` | Request header | Needs |
| --- | --- | --- |
| `google` | `Authorization: Bearer <Google identity token>` | a Google login |
| `bearer` | `Authorization: Bearer <token>` | `token_env` |
| `basic` | `Authorization: Basic`, with an empty user name and the token as password (what Nix sends for a netrc entry with only a password) | `token_env` |

A cache with no entry is read without credentials, which suits public caches.
If such a cache answers 401 or 403, Mango says which entry to add.

The file holds the *name* of an environment variable, never a token. Set the
variable through your credential manager or shell. A missing token for a cache
the environment uses is an error before building. Mango removes every token
variable named in the file from the build's environment. Token expiry and
revocation remain controlled by the cache service.

For `google`, Mango obtains an identity token from the GCE metadata server, an
explicit `GOOGLE_APPLICATION_CREDENTIALS` service-account file when
`google-auth` is available, or `gcloud auth print-identity-token`. For local
use, sign in with `gcloud auth login` first. The identity must have
`roles/run.invoker` on the cache service. Tokens are refreshed during long
builds, and are bound to the cache URL wherever the account type allows. The
lightweight launcher uses only the standard library; Google ADC support is
optional.

## Cache server contract

There is no Mango-specific cache format. Any server that speaks the standard
Nix HTTP binary cache protocol works:

- `GET` and `HEAD` on `/nix-cache-info`, `/<store-hash>.narinfo`, and the NAR
  file each narinfo names, which must be a single file name under `/nar/`.
- Narinfo files signed by the key in `public_key`.
- A NAR request may answer with the bytes, or with a redirect to a
  pre-authorised URL. Mango does not send the credential to the redirect
  target.
- `401`, `403` and `404` mean "not cached"; the path is then built.

For example, a private Cachix cache (this configuration has not yet been
exercised against Cachix itself):

```toml
# mango.toml
[[cache]]
url = "https://YOUR-CACHE.cachix.org"
public_key = "YOUR-CACHE.cachix.org-1:YOUR-BASE64-PUBLIC-KEY"
```

```toml
# credentials.toml
["https://YOUR-CACHE.cachix.org"]
auth = "basic"
token_env = "CACHIX_AUTH_TOKEN"
```

Reading does not publish new build results; publishing remains a separate
builder operation.

## How it works

For each cache, Mango starts a temporary loopback proxy under an unguessable
path. It adds authentication only to cache requests and passes NAR redirects
through to Nix without forwarding the credential. The proxies are stopped when
the invocation exits. Existing Nix configuration is preserved, and cache
settings are added for this invocation. A missing cached path builds normally;
authentication rejections are reported and treated as cache misses.

The environment being built can learn the proxy address from its Nix settings,
so during a build it can read any path its own caches serve to you. It cannot
obtain the credential, and it cannot reach caches it did not declare. Build
trusted environments only.

## Host backend: Nix daemon trust

With `--builder host` or a legacy environment-first invocation, an environment
file cannot grant itself trust in a system Nix daemon. This
integration works with a local store or a daemon that trusts the invoking user.
Otherwise Nix may ignore the dynamic loopback substituter and its public key.
Mango does not modify host Nix configuration or disable signature checking.
The default managed builder configures trust in its own private store instead.
The image-building QEMU VM alone is not a replacement for a host Nix daemon.
Caches configured in the host's own Nix settings keep working with this
backend, with or without `mango.toml`.
