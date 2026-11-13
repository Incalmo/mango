# Authoring Go module locks

`mango-lock-go requests.json --output results.json` resolves dependencies using
the Go executable in an exact digest-pinned base image. Run it on a remote
authoring host with Docker and `MANGO_REMOTE_AUTHOR=1`. It does not build the
application. The Python entry point is `mango.authoring.go_lock.resolve`.

```json
{
  "requests": [{
    "id": "application-modules",
    "base": "golang@sha256:<64 hexadecimal characters>",
    "goproxy": "https://proxy.golang.org",
    "modules": [{"dir": ".", "go_mod": "exact go.mod text", "go_sum": "exact go.sum text"}],
    "scope": "main"
  }]
}
```

Apply the original source patches before taking module-file bytes. The resolver
rejects changed manifests, local or unversioned replacements, workspaces,
unsupported toolchain requirements, and private/insecure proxy settings.
Fixed-version module-to-module replacements are supported: the replacement's
actual module path and version determine the proxy artifacts. Both original
module files remain unchanged. Do not translate a replacement by rewriting
`go.mod` or by choosing a newer dependency.

Go runs with `GOTOOLCHAIN=local` to prevent a hidden toolchain download.
This is an authoring constraint, not permission to change the application's
original build flags. The resolver compares module zip and mod-file Go `h1:`
checksums with the original sums and Go's results, then records SHA256 hashes
for the exact public proxy bytes. Info metadata may differ only in JSON field
ordering, with duplicate keys rejected. Downloads have configurable size
limits; credentials and local proxy endpoints are rejected.

Consume the result's locked inputs as a file-backed Go proxy. Use a fresh
module cache, disable network, and replay the original module download/build
commands. Compare `go.mod` and `go.sum` before and after. Preserve original
`-mod`, build tags, linker flags, Cgo settings and source transformations.
The authoring receipt is not an offline build proof. Queries such as `@latest`,
unlisted graph modules, system libraries and source generators need additional
explicit inputs or adapters.

CLI output is checkpointed per request and binds the full request hash.
`--retry-failures` retries failed requests; completed unchanged requests are
reused. Never reuse a lock merely because two tasks use the same repository:
compare the patched module bytes, base toolchain, scope and proxy policy.
