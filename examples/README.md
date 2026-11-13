# Examples

Each example holds the same Harbor task twice:

- `dockerfile/`: the original task, built from `environment/Dockerfile`.
- `mango/`: the translated environment (`default.nix`, `inputs.lock.json`,
  `mango.lock`) beside the unchanged task files under `task/`.

| Example | Source | Shows |
| --- | --- | --- |
| [nginx-request-logging](nginx-request-logging) | [Terminal-Bench 2](https://github.com/laude-institute/terminal-bench-2) | Pinned base image, apt packages and pip wheels |

Run a translated example from the repository root:

```sh
./bin/mango load examples/nginx-request-logging/mango
./bin/mango run examples/nginx-request-logging/mango --agent oracle
```

Task files are copied unmodified from their upstream benchmark and keep its
license; see the `LICENSE.*` file in each example.
