# Environment declaration checks

`default.nix` returns `mkEnvironment`; its `declaration` attribute is the only
description of mappings and topology. There is no `manifest.json`. Check it
without building, Docker access or workspace mutation:

```sh
nix-shell --run 'nix2rle /absolute/environment validate'
```

Success prints JSON containing `valid`, `services`, `verifiers`, and `images`.
Failure exits nonzero with the invalid field or mapping. `validate-factory`
additionally rejects authored Dockerfiles/Compose files and external symlinks
(including Nix `result` links) and instantiates every image and build output.
Neither command builds anything or qualifies a task.

The evaluated declaration provides:

| Field | Meaning |
| --- | --- |
| `platform` | `linux/amd64` (currently the only platform) |
| `task` | Environment-relative Harbor task directory |
| `images` | Container output name to Nix attribute; must include `egress` |
| `builds` | Optional application/check output name to Nix attribute |
| `services` | Runtime service name to container output name; must include `main` |
| `verifiers` | Grader context to service-to-container mapping; each must include `main` |
| `environment_files` | Destination under the generated environment to source directory |
| `executable_files` | Files under the generated environment to mark executable |

Output names contain letters, digits, underscores and hyphens, starting with a
letter or digit. Paths are relative without `.` or `..` components, and sources
cannot escape the environment through symlinks.

Loaded image manifests are bound to the image outputs and service/verifier
mappings. After changing those mappings, run `load` again before `prepare` or
`run`; existing Nix outputs can be reused. Compose settings are re-read from the
declaration on each preparation.

## Separate graders

Declare each separate grader context in `verifiers` with every service it runs,
for example `verifiers."steps/check/tests" = { main.container = "graderA";
database.container = "graderDb"; };`. Context names are task-relative paths
(`tests` or `steps/<step>/tests`). The runner expands them to Harbor's variant
keys, `<context>/<variant>`, where `variant` is the first 16 hexadecimal
characters of SHA-256 of Python `json.dumps` of the configured `docker_image`
(`null` when absent). The declaration must cover the task's separate contexts
exactly; shared graders need no entry.

## Parallel commands

All stateful commands take a nonblocking OS lock on their workspace. Concurrent
use of the same workspace fails clearly; a crash releases the lock. Use a
different `--workspace` for each environment being built concurrently.

Workspace isolation does not isolate Docker network subnets, published ports,
or other authored runtime resources. Environments that reuse those resources
must run sequentially under a scheduler resource lock or on separate Docker
hosts. The harness preserves the authored network configuration.
