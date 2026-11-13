# How it works

![Mango architecture](assets/architecture-light.svg#only-light)
![Mango architecture](assets/architecture-dark.svg#only-dark)

An environment moves through three stages. Only the first one talks to the
internet.

## 1. Author: online, once

You declare the image: its base, its packages and its build steps. Mango's
pinning tools resolve every download to a content hash.

| You have | Use |
| --- | --- |
| A base image tag | `mango-pin-base` |
| Distro packages (apt, apk, opkg) | `mango-lock-packages` |
| Any build command that downloads | `mango-lock-record` ([record and replay](record-replay.md)) |
| A single file or a Git repository | `mango-pin-file`, `mango-pin-source` |

## The environment: frozen

What you commit is plain files:

| File | Holds |
| --- | --- |
| `default.nix` | Images, services and verifiers |
| `inputs.lock.json` | A URL and SHA-256 for every downloaded file |
| `inputs.lock.d/` | Responses no URL keeps serving, such as registry indexes |
| `mango.lock` | The Mango and builder versions |
| `task/` | The Harbor task: instruction, tests, solution |

Nothing in it names "latest". Building it next year uses the same bytes.

## 2. Build: offline, repeatable

`mango load` fetches the pinned inputs, verifies each hash, and runs the
build steps in a sandbox or VM with no network, using the base image's own
shell and installers. The result is a set of OCI images.

Inputs come from your [binary cache](remote-cache.md) when it has them.
Upstream is contacted only on a cache miss, and the download must match the
lock. A dependency you forgot to pin fails the build instead of drifting.

## 3. Run

`mango run` loads the images into Docker and starts a
[Harbor](https://github.com/harbor-framework/harbor) trial: the main
container, an egress sidecar, the agent and the verifier.

## Why this gives you the three properties

- **Reproducible:** inputs are frozen by hash and the build cannot reach the
  network, so there is nothing left that can change.
- **Scalable:** every build reads from your cache, so a thousand builds make
  no requests to Docker Hub, PyPI or Maven Central.
- **General:** the build runs the distro's own installers, and downloads are
  locked by recording them, so no language needs special support.
