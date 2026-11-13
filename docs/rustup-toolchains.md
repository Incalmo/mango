# Pinning Rustup toolchains

`mango-lock-rustup` records official Rust release manifests and SHA256-pinned
component archives. `dependencies.fetchRustupToolchain { lockFile = ./rustup.json; }`
produces an installation tar archive. Extract it into the original
`$RUSTUP_HOME/toolchains/<lock.toolchain>` directory before the authored Cargo
command. Keep the original `rust-toolchain.toml`, Rustup settings, Cargo flags,
and environment: a bare `cargo` in a project selecting `stable` does not mean
the same thing as `cargo +1.96.0`.

For a known selector and installation profile:

```sh
mango-lock-rustup --channel stable --target x86_64-unknown-linux-gnu \
  --profile minimal --output rustup.json
```

Moving channels are bound to a dated manifest whose bytes must match the
selected channel manifest. Exact releases and dated channels are supported.
Only metadata is downloaded during pinning; component archives are fetched
and verified by the declarative build. Additional components and standard
library targets in project selectors are resolved from the release inventory.

For faithful selection from an existing image, capture its original
`settings.toml` bytes and installed toolchain directory names. Supply a JSON
file containing `base` (an image reference with its SHA256 digest),
`settings_toml` (the exact text), and `installed_toolchains` (an array of names):

```sh
mango-lock-rustup --base-state original-rustup.json \
  --selector-file project/rust-toolchain.toml \
  --target x86_64-unknown-linux-gnu --output rustup.json
```

The Python APIs `select_toolchain` and `pin_selection` apply explicit command
selection, `RUSTUP_TOOLCHAIN`, the effective project selector, then the base
default, in that order. Pass command/environment selectors explicitly when
present. They bind settings and selector byte hashes and reuse an already
installed named toolchain without resolving a moving channel again. Such a
result has `kind: "base-toolchain"` and needs no new archive. Requested
extensions on an existing toolchain require a separate component inventory
review; ambiguous directory overrides and local custom toolchains fail closed.
Callers must locate the effective project selector, including ancestor files.

When packages were installed before inspecting Rustup, retain the
`package_state.prepare_prefix` receipt and bind inspection to its
`prepared_image.id`, original `prepared_image.base`, and canonical
`prepared_image.apt_request_sha256`. Capture settings and installed toolchains
from that exact prepared image. Replay must verify the settings/selector
hashes before installing new components. Changing Rustup's global default or
editing the source selector to avoid a download would change selection
semantics and is unnecessary.

The archive helper is independent of the Cargo dependency cache. Use both
when the build needs a toolchain missing from the base and registry crates.
