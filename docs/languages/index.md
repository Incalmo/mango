# Languages

Each page shows the same small image twice: as a Dockerfile, and as a Mango
environment. Pick your language and copy the recipe.

| Language | Package manager shown |
| --- | --- |
| [Python](python.md) | pip |
| [JavaScript](javascript.md) | npm |
| [TypeScript](typescript.md) | npm + tsc |
| [Java](java.md) | Maven |
| [C#](csharp.md) | NuGet |
| [C++](cpp.md) | apt, Conan |
| [C](c.md) | apt |
| [Go](go.md) | Go modules |
| [Rust](rust.md) | Cargo |
| [PHP](php.md) | Composer |
| [Kotlin](kotlin.md) | Gradle |
| [Swift](swift.md) | Swift Package Manager |
| [Ruby](ruby.md) | Bundler |
| [R](r.md) | CRAN |
| [Scala](scala.md) | sbt |
| [Dart](dart.md) | pub |
| [Perl](perl.md) | cpanm |
| [Lua](lua.md) | LuaRocks |
| [Haskell](haskell.md) | Cabal |
| [Elixir](elixir.md) | Hex |

## One recipe for all of them

Mango has no per-language support to maintain. Every page follows the same
three steps:

1. **Pin the base image** with `mango-pin-base`.
2. **Record the build's downloads** with `mango-lock-record`, which also
   re-runs the build with no network to prove the lock is complete.
3. **Replay them in the image build** by putting `mango-proxy replay` in
   front of the same command.

A language or tool that is not listed works the same way, as long as it
downloads over HTTP(S). See [record and replay](../record-replay.md).

!!! info "How these recipes were checked"
    Every package manager on these pages was recorded and then replayed with
    no network inside its base image. The Java (Maven) recipe was also built
    end to end as a Mango image. The base-image digests and hashes are
    placeholders: `mango-pin-base` prints the real values.
