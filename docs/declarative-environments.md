# Declare the environment once

A customer environment can supply only `default.nix`, container recipes, and its
Harbor task assets. It does not need a hand-maintained `manifest.json` or runtime
Docker Compose file. The environment expression accepts `{ harness }` and returns
`h.mkEnvironment`, where `h = import harness {}`.

Optional `mango.toml` beside `default.nix` selects an authenticated remote binary
cache for the launcher. See [Remote binary caches](remote-cache.md) for Google
IAM and customer-token configuration, including Nix daemon trust requirements.
`mango load` uses the [managed Docker builder](managed-builder.md) by default;
`--builder host` selects an existing host Nix installation.

`mkEnvironment` accepts:

- `name`: descriptive environment name.
- `task`: explicit local task directory, typically `./task`; it contains
  `task.toml`, instructions, tests, solutions and runtime assets.
- `containers`: named image derivations, including `egress`. Each application
  container can be defined in a separate Nix file returning `h.mkContainer`.
- `services`: runtime service names mapped to Compose-style attributes. Each
  service declares `container = "<container output name>"`; `main` is required.
  Attributes such as `command`, `depends_on`, `networks`, `volumes`, `environment`
  and `healthcheck` remain ordinary Compose attributes.
- `networks`, `volumes`, `configs`, `secrets`: optional Compose top-level mappings.
  These declarations are available to both runtime and grader configurations.
- `verifiers`: separate grader contexts mapped to service definitions using the
  same `container` field. Keys are task-relative context paths such as `tests`
  or `steps/verify/tests`, not computed image hashes. Every separate context in
  `task.toml` must be covered. Shared graders need no separate entry.
- `builds`: optional named application/check derivations to retain alongside
  image outputs.
- `platform`: currently `linux/amd64`.
- `environmentFiles`: optional destination-directory to source-directory mapping
  for assets copied into the generated task environment. Values are local paths
  inside the environment.
- `executableFiles`: optional paths beneath the generated environment directory
  that need executable mode.

Container/build output names use letters, digits, underscores and hyphens. The
runtime owns `image`, `build` and `pull_policy`: services cannot author these
fields. Images come from loaded Nix outputs and run without builds or pulls.
The declaration creates no Dockerfile and discovers no dependencies.

`mkEnvironment` returns `images`, `builds`, and a versioned `declaration` attribute.
The CLI evaluates only `declaration` to obtain service mappings and Compose
metadata. It does not force the container/application derivations during this
step. `fetch` evaluates and builds the image and application attributes in the
usual way; any imports-from-derivation inside an authored recipe retain their
normal Nix evaluation behavior when those image attributes are evaluated.

`mango ENV declaration` prints the evaluated declaration as JSON, plus the
Nix-free runtime declaration it generates, without building anything. Tools that
inspect topology should read it rather than calling `nix-instantiate` directly.

For a separate Python grader, `h.pythonVerifier { tests = ./task/tests; packages = [ ... ]; }`
returns a container with the tests at `/tests` (`test.sh` executable) on a minimal
Python userland; declare it under `verifiers.<context>.main.container`.

Run the environment with:

```sh
nix-shell --run 'mango /path/to/environment validate'
nix-shell --run 'mango /path/to/environment fetch'
nix-shell --run 'mango /path/to/environment check'
nix-shell --run 'mango /path/to/environment load'
nix-shell --run 'mango /path/to/environment compose'
nix-shell --run 'mango /path/to/environment run --agent oracle'
```

Only generated workspace copies receive Compose files. Source task assets remain
unchanged. `compose` returns startup commands; `run --agent oracle|nop` performs
a Harbor trial with grading. These are distinct operations.

The source `task/` must not contain Dockerfiles or Compose files in any context;
the runner rejects them. Every Compose document is generated from the
declaration into the workspace copy. `main` is a required service, and every
separate grader context must declare `main`.
