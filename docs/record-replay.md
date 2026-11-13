# Record and replay: lock any package manager

Mango does not need to understand a package manager to lock it. A package
manager downloads over HTTP(S); `mango-lock-record` records what a command
downloads, and the offline build replays that recording to the same command.
Maven, Gradle, NuGet, Bundler, a `curl` in a build script and a tool released
next year are all the same case.

## Record

```sh
nix-shell --run 'mango-lock-record --lock "$ENV/inputs.lock.json" --prefix record/app \
  --base docker.io/library/maven@sha256:<digest> \
  --context "$ENV/app" --workdir /app -- mvn -B package'
```

The tool runs the command twice inside the pinned base image:

1. **Online**, with `mango-proxy` recording every HTTP(S) exchange.
2. **With no network**, answered only from the recording. A request that was
   not recorded fails here, at authoring time, and the proxy names it.

A command that passes both runs is locked completely. `--context` is copied
into `--workdir` first; `--env NAME=VALUE` adds environment. A single command
argument is run by `sh -c`. Use the command the build will run: a different
goal, profile or flag can request files that were never recorded.

Recording runs the project's build with network access. Do it where that is
acceptable, on trusted sources.

## Replay

Add the proxy to the image's inputs and put it in front of the same command:

```nix
inputs = h.dependencies.fetchLockedInputs {
  lock = ./inputs.lock.json;
  extra."mango-proxy" = h.replayProxy;
};
buildScript = ''
  set -eu
  in=/.nix-inputs/files
  cp -a /.nix-inputs/context /app && cd /app
  $in/mango-proxy replay $in/record/app -- mvn -B package
'';
```

`h.replayProxy` is one static executable, so it runs in any base image. In
replay it never opens an outbound connection.

## What is locked

A recording is an index of exchanges plus their response bodies, stored by
content hash under the prefix:

| Lock entry | When | Stored |
| --- | --- | --- |
| `urls` + `sha256` | The server declares the response long-lived | Fetched by Nix from the URL, verified by hash |
| `file` + `sha256` | Any other response | Committed beside the lock in `inputs.lock.d/<sha256>` |
| `urls` + `sha256`, reported | Short-lived but above `--commit-limit` (default 1 MiB) | Fetched by URL; keep it in the binary cache |

"Long-lived" is HTTP's own freshness rule (RFC 9111): `immutable`, a
`max-age` of at least a day, or a tenth of the time since `Last-Modified` when
no lifetime is given. Release artifacts are served that way; registry indexes
and "latest version" documents are not. Committing the short-lived responses
is what lets a cold rebuild work after the upstream index has moved on.

An exchange is identified by its method, its URL and, when the request has a
body, the body's digest. That covers Git's smart HTTP protocol, so tools that
clone repositories are locked the same way. Their responses have no URL to
fetch from and are always committed.

The tool prints a summary. Entries listed under
`unstable_but_too_large_to_commit` rebuild only from the binary cache once the
upstream content changes.

## How requests are captured

The command needs to honour only one of two mechanisms, and neither is
specific to a tool:

- **Environment.** The proxy and a certificate authority created for that one
  run are exported through the variables HTTP clients conventionally read
  (`HTTPS_PROXY`, `SSL_CERT_FILE`, `JAVA_TOOL_OPTIONS` and similar).
- **Namespace.** With root and `CAP_SYS_ADMIN`, the command runs in a private
  mount namespace where every host name resolves to the proxy and the system
  certificate bundles hold only its authority. Clients that ignore proxy
  settings are captured this way. Image builds and `mango-lock-record` both
  have this privilege.

No file of the image is modified by either mechanism: the authority, its
trust store and the resolver configuration live in a temporary directory and
in mounts that only the command sees.

## Verified package managers

Each of these was recorded and then replayed with no network, with no
tool-specific code or configuration in Mango:

| Language | Package managers |
| --- | --- |
| Python | pip, uv |
| JavaScript, TypeScript | npm, Yarn, pnpm |
| Java, Kotlin, Scala | Maven, Gradle, sbt |
| C# | NuGet (`dotnet`) |
| Go | Go modules |
| Rust | Cargo |
| PHP | Composer |
| Ruby | Bundler, RubyGems |
| Swift | Swift Package Manager |
| C, C++ | Conan, plus the distro managers below |
| R | CRAN (`install.packages`) |
| Dart | pub |
| Perl | cpanm |
| Lua | LuaRocks |
| Haskell | Cabal |
| Elixir | Hex (`mix`) |
| Distro packages | apt, apk, dnf |

Anything else that downloads over HTTP(S) should work the same way; the
offline verification run tells you whether it does.

## Limits

- **Requests must be repeatable.** A URL or request body that differs between
  runs (a random or time-dependent parameter) cannot be replayed. dnf's
  `countme` is one: pass `--setopt='*.countme=0'`.
- **HTTP(S) only.** `git://`, SSH and other protocols are not captured.
- **Public downloads only.** `Authorization` and `Cookie` headers are never
  forwarded or recorded.
- **Clients that pin certificates** or carry a private trust store that no
  environment variable overrides reject the proxy's authority.
- **Whole responses.** Range and conditional requests are answered with the
  full response.
- **One recording per command line.** Record again after changing the
  command, the sources that affect resolution, or the base image.

The dedicated lock tools (`mango-lock-packages`, `mango-lock-python`,
`mango-lock-go`, `mango-lock-npm` and others in
[authoring tools](authoring-tools.md)) remain useful where a native lockfile
exists: they pin exactly what that lockfile selects without running a build.
