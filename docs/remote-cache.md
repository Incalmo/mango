# Remote binary caches

Put `mango.toml` beside an environment's `default.nix` and `mango.lock`.
The Mango launcher reads it before resolving the pinned harness and keeps the
cache available throughout `mango load`, `fetch`, `build`, and other commands.
Both command-first and legacy environment-first invocations support it.
Use an updated launcher; older launchers do not read this file. The harness
revision in `mango.lock` does not need to change to enable cache access.

For Incalmo employees and builders, use the **internal** read service:

```toml
[cache]
url = "https://YOUR-INTERNAL-CACHE.run.app"
public_key = "YOUR-CACHE-NAME:YOUR-BASE64-PUBLIC-KEY"
auth = "google"
```

Mango obtains an identity token from the GCE metadata server, an explicit
`GOOGLE_APPLICATION_CREDENTIALS` service-account file when `google-auth` is
available, or `gcloud auth print-identity-token`. For local use, sign in with
`gcloud auth login` first. The identity must have `roles/run.invoker` on the
cache service. Google tokens are refreshed during long builds. The lightweight
launcher uses only the standard library; Google ADC support is optional.

For customers, use the **public** read service and an issued customer token:

```toml
[cache]
url = "https://YOUR-PUBLIC-CACHE.run.app"
public_key = "YOUR-CACHE-NAME:YOUR-BASE64-PUBLIC-KEY"
auth = "token"
token_env = "INCALMO_NIX_CACHE_TOKEN"
```

Set that environment variable through your local credential manager or shell,
then run `mango load /path/to/environment`. Do not put a token into `mango.toml`
or `mango.lock`. Mango removes the token variable from the child build process's
environment. A missing token is an error before building. Token expiry and
revocation remain controlled by the cache service.

URLs must be HTTPS origins without credentials, query strings or paths. The
public key is the cache's Nix narinfo signing key, **not** the customer-token
issuer key. For `Incalmo/infra/nix-cache`, Terraform outputs `internal_url`,
`public_url`, and `public_key` provide these values. Reading does not publish
new build results; publishing remains a separate builder operation.

Mango starts a temporary loopback proxy under an unguessable path. It adds
authentication only to cache requests and passes signed NAR redirects through
to Nix without forwarding the authentication token. The proxy is stopped when
the invocation exits. Existing Nix configuration is preserved, and cache
settings are added for this invocation. A missing cached path builds normally;
authentication rejections are reported and treated as cache misses.

## Nix daemon trust

An environment file cannot grant itself trust in a system Nix daemon. This
integration works with a local store or a daemon that trusts the invoking user.
Otherwise Nix may ignore the dynamic loopback substituter and its public key.
Mango does not modify host Nix configuration or disable signature checking.
A managed builder must configure that trust itself. The image-building QEMU
VM is not a replacement for the host Nix daemon.
