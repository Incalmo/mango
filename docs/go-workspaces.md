# Authoring Go workspace and local-module locks

`python -m mango.authoring.go_workspace requests.json --output results.json`
(Python: `mango.authoring.go_workspace.resolve(request, fetch=..., container_factory=...)`)
extends [Go module locks](go-module-locks.md) to projects that use `go.work` and/or
source-local `replace M => ./dir` directives. Run it only on the remote authoring
host with Docker and `MANGO_REMOTE_AUTHOR=1`. It reuses go_lock's pinned base,
public-proxy, size, `h1:` and artifact-hash checks. It does **not** build the
application: the receipt is authoring evidence, not a compilation proof.

```json
{"requests": [{
  "id": "cadence",
  "base": "golang@sha256:<64 hex>",
  "goproxy": "https://proxy.golang.org",
  "original": {"project_dir": "/src/cadence", "run_dir": ".", "goflags": ""},
  "modules": [
    {"dir": ".", "go_mod": "exact text", "go_sum": "exact text"},
    {"dir": "cmd/server", "go_mod": "exact text", "go_sum": null}
  ],
  "workspace": {"mode": "go.work", "dir": ".", "go_work": "exact text", "go_work_sum": null}
}]}
```

* `modules[].dir` is project-relative; every key is required. `go_sum: null` means
  the file is absent; `""` means present and empty. The same applies to
  `go_work_sum`.
* `original.project_dir` is the absolute directory of the original build (recorded,
  never used on the author host); `run_dir` is where Go commands ran (a declared
  module; in workspace mode also a `use` member). `goflags` is the original
  `GOFLAGS`, recorded in the guidance. `-mod=mod`, `-modfile`, `-workfile` and
  `-overlay` are rejected because authoring does not reproduce them.
* `workspace` is `{"mode": "go.work", ...}` or `{"mode": "none"}`. Only `none`
  runs Go with `GOWORK=off`; otherwise `GOWORK` is the staged original `go.work`.
* Optional as in go_lock: `scope`, `gosumdb`, `graph_mod_files`,
  `allow_missing_graph_mods`, `limits`, `platform`.

## Rules

The request's manifests are staged unchanged in one project directory that keeps the
relative layout (never flattened or rewritten). Rejected, each with its own
`reason_code`: absolute (`go-local-absolute`), escaping (`go-local-escape`), missing
(`go-local-missing`, which includes Harbor's `=> ../` with no `go.mod`) and
module-path-mismatched (`go-local-module-mismatch`) local targets; workspace `use`
entries that are undeclared or malformed; duplicate module paths (`module-conflict`);
declared modules nothing references (`module-unreferenced`); unknown modes
(`unsupported-mode`). Cycles (child `=> ../..`) are followed by directory identity.
Fixed-version remote replacements follow go_lock. `go`/`toolchain` directives in every
`go.mod` and in `go.work` are checked against the base's real Go
(`toolchain-newer-than-base`); `GOTOOLCHAIN=local` is always set.

Sum authority mirrors Go: workspace mode merges the `go.sum` of every `use` module with
`go.work.sum`; standalone mode uses only the run directory's `go.sum`. Go's own sum
verification stays on, and artifacts must also match these sums.

Read-only `go mod edit -json` / `go work edit -json` cross-check the resolver's
parsing (`go-parse-mismatch`). `go env` must report the expected `GOWORK`. All
manifest files are hashed before and after Go; any change, or any new
`go.mod`/`go.sum`/`go.work`/`go.work.sum` such as an unexpected `go.work.sum`, fails
(`go-mod-mutated`). `go mod download` entries without cache artifacts (workspace
members, local replacement targets) are accepted only if path and directory match a
declared module and are listed in the receipt; anything else is
`go-output-unsupported`.

## Result

`kind` is `mango.go-workspace-lock-receipt`; `authoring_only` is true and
`application_compilation_proven` false. `lock` and `cache` are go_lock's shapes, so
`fetchLockedInputs` can consume them. `evidence` carries the base/Go version,
hashes of every original manifest (present or absent, including workspace files), the
effective selection (mode, `use`, local/remote replacements, sum authorities,
local modules reported by Go) and artifact hashes. `offline` gives proxy/modcache
environments that keep the original workspace selection and `GOFLAGS`, with
`${MANGO_GO_PROJECT}` / `${MANGO_GO_CACHE}` placeholders instead of author paths.
Consumers must place the byte-identical source tree (and original manifests) at
`${MANGO_GO_PROJECT}`, replay the original commands, and keep absent files absent.

## Limitations

Source patches and source-tree equality are the caller's responsibility. `go mod
download` runs once from `run_dir`; other build contexts, `vendor/`, cgo, `@latest`
queries and unlisted graph modules are out of scope. In standalone mode, local
replacements in non-main modules are demanded as declared manifests even though Go
ignores them there (conservative).
