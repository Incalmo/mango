"""Pin a Rust release channel and its profile components from official manifests.

Only metadata is fetched here. Declarative builds fetch and verify the component
archives using the recorded SHA256 values; source rust-toolchain files stay intact.
"""

import argparse
import hashlib
import json
import re
import tomllib
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def channel_parts(channel):
    match = re.fullmatch(r"(stable|beta|nightly)(?:-(\d{4}-\d{2}-\d{2}))?", channel)
    if match:
        return match.group(1), match.group(2)
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", channel):
        return channel, None
    raise ValueError(
        "Only official stable/beta/nightly, dated channels, and exact release selectors are supported"
    )


def select_toolchain(
    selector,
    settings,
    *,
    base,
    target,
    installed_toolchains,
    command_toolchain=None,
    environment_toolchain=None,
):
    """Bind effective Rustup selection to original source and pinned-base state.

    Pass the effective project selector file, not an arbitrary ancestor's file.
    Directory overrides need an explicit command/environment selection because
    their precedence depends on filesystem proximity. Existing named toolchains
    are reused from the pinned base rather than re-resolving a moving channel.
    """
    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", base):
        raise ValueError("Rustup base state needs an exact image digest")
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", target):
        raise ValueError("Unsafe Rust host target")
    state = tomllib.loads(settings.decode())
    if state.get("default_host_triple", target) != target:
        raise ValueError("Rustup base host and requested target differ")
    if not isinstance(installed_toolchains, list) or any(
        not isinstance(x, str) for x in installed_toolchains
    ):
        raise ValueError(
            "An explicit original installed-toolchain inventory is required"
        )
    extra_components, extra_targets = [], []
    profile = state.get("profile", "default")
    if command_toolchain:
        channel, origin = command_toolchain.removeprefix("+"), "command"
    elif environment_toolchain:
        channel, origin = environment_toolchain, "environment"
    else:
        if state.get("overrides"):
            raise ValueError("Directory overrides require explicit effective selection")
        if selector is not None:
            text = selector.decode().strip()
            if text.startswith("["):
                document = tomllib.loads(text)
                if set(document) != {"toolchain"} or set(document["toolchain"]) - {
                    "channel",
                    "profile",
                    "components",
                    "targets",
                }:
                    raise ValueError("Unsupported Rustup project selector fields")
                selected = document["toolchain"]
            else:
                if len(text.splitlines()) != 1:
                    raise ValueError("Legacy Rustup selector must be one channel line")
                selected = {"channel": text}
            channel, origin = selected["channel"], "source-file"
            profile = selected.get("profile", profile)
            extra_components = selected.get("components", [])
            extra_targets = selected.get("targets", [])
        else:
            channel, origin = state["default_toolchain"], "base-default"
    channel = channel.removesuffix("-" + target)
    channel_parts(channel)
    if profile not in ("minimal", "default", "complete"):
        raise ValueError("Unknown Rustup installation profile")
    for values in (extra_components, extra_targets):
        if not isinstance(values, list) or any(
            not isinstance(x, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", x)
            for x in values
        ):
            raise ValueError("Components and targets must be explicit safe name lists")
    name = channel + "-" + target
    if name in installed_toolchains and (extra_components or extra_targets):
        raise ValueError(
            "Existing toolchain with requested extensions needs component inventory verification"
        )
    return {
        "base": base,
        "channel": channel,
        "target": target,
        "toolchain": name,
        "profile": profile,
        "components": extra_components,
        "targets": extra_targets,
        "origin": origin,
        "provided_by_base": name in installed_toolchains,
        "selector_sha256": hashlib.sha256(selector).hexdigest()
        if selector is not None
        else None,
        "settings_sha256": hashlib.sha256(settings).hexdigest(),
        "installed_toolchains": list(installed_toolchains),
    }


def component_lock(
    data,
    expected_sha256,
    *,
    channel,
    target,
    profile,
    manifest_url,
    extra_components=(),
    extra_targets=(),
):
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError("Rust channel manifest checksum mismatch")
    _, requested_date = channel_parts(channel)
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", target):
        raise ValueError("Unsafe Rust target triple")
    document = tomllib.loads(data.decode())
    if document.get("manifest-version") != "2" or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", document.get("date", "")
    ):
        raise ValueError("Expected dated Rust manifest version2")
    if requested_date and requested_date != document["date"]:
        raise ValueError("Rust manifest date differs from explicit dated selector")
    if profile not in ("minimal", "default", "complete") or profile not in document.get(
        "profiles", {}
    ):
        raise ValueError("Unknown Rust installation profile")
    host = document["pkg"]["rust"]["target"].get(target)
    if not host or not host.get("available"):
        raise ValueError("Rust release is unavailable for the requested host")
    inventory = {
        (item["pkg"], item["target"])
        for item in host.get("components", []) + host.get("extensions", [])
    }
    supported = {
        name
        for name, component_target in inventory
        if component_target in (target, "*")
    }
    components = []
    selected = []
    for requested in [*document["profiles"][profile], *extra_components]:
        name = document.get("renames", {}).get(requested, {}).get("to", requested)
        if not re.fullmatch(r"[a-z0-9_-]+", name):
            raise ValueError("Unsafe component name")
        # Profiles are cross-platform: e.g. minimal includes rust-mingw, which
        # is absent from the Linux release's component/extension inventory.
        if name not in supported:
            if requested in extra_components:
                raise ValueError(
                    f"Explicit component {name} is unsupported for {target}"
                )
            continue
        selected.append((name, target if (name, target) in inventory else "*"))
    for extra_target in extra_targets:
        if (
            not re.fullmatch(r"[a-zA-Z0-9_.-]+", extra_target)
            or ("rust-std", extra_target) not in inventory
        ):
            raise ValueError("Explicit standard-library target is unsupported")
        selected.append(("rust-std", extra_target))
    for name, component_target in dict.fromkeys(selected):
        package = document["pkg"][name]
        record = package["target"].get(component_target)
        if not record or not record.get("available"):
            raise ValueError(f"Component {name} is unavailable for {target}")
        url, sha256 = record["xz_url"], record["xz_hash"]
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.path.endswith(".tar.xz")
        ):
            raise ValueError("Rust component URL must be public HTTPS tar.xz")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("Every Rust component needs its SHA256")
        components.append(
            {
                "name": name,
                "target": component_target,
                "version": package["version"],
                "url": url,
                "sha256": sha256,
            }
        )
    if not {"rustc", "rust-std", "cargo"}.issubset({x["name"] for x in components}):
        raise ValueError("Profile lacks the compiler, standard library, or Cargo")
    return {
        "schema_version": 1,
        "channel": channel,
        "target": target,
        "profile": profile,
        "date": document["date"],
        "toolchain": channel + "-" + target,
        "rust_version": document["pkg"]["rust"]["version"],
        "manifest": {"url": manifest_url, "sha256": expected_sha256},
        "components": components,
    }


def pin(channel, target, profile, *, extra_components=(), extra_targets=()):
    release, date = channel_parts(channel)
    url = f"https://static.rust-lang.org/dist/{date + '/' if date else ''}channel-rust-{release}.toml"
    data = urllib.request.urlopen(url, timeout=60).read()
    expected = (
        urllib.request.urlopen(url + ".sha256", timeout=60).read().decode().split()[0]
    )
    arguments = {
        "channel": channel,
        "target": target,
        "profile": profile,
        "extra_components": extra_components,
        "extra_targets": extra_targets,
    }
    preliminary = component_lock(data, expected, manifest_url=url, **arguments)
    if release in ("stable", "beta", "nightly") and date is None:
        dated_url = f"https://static.rust-lang.org/dist/{preliminary['date']}/channel-rust-{release}.toml"
        dated = urllib.request.urlopen(dated_url, timeout=60).read()
        if dated != data:
            raise ValueError("Dated Rust manifest differs from selected channel")
        url = dated_url
    return component_lock(data, expected, manifest_url=url, **arguments)


def pin_selection(selection):
    if selection["provided_by_base"]:
        return {"schema_version": 1, "kind": "base-toolchain", "selection": selection}
    lock = pin(
        selection["channel"],
        selection["target"],
        selection["profile"],
        extra_components=selection["components"],
        extra_targets=selection["targets"],
    )
    return {**lock, "kind": "rustup-toolchain", "selection": selection}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--channel", help="Explicit official channel/release to pin")
    mode.add_argument(
        "--base-state",
        type=Path,
        help="JSON with exact base, settings_toml, installed_toolchains",
    )
    parser.add_argument("--target", required=True)
    parser.add_argument("--profile", choices=["minimal", "default", "complete"])
    parser.add_argument("--selector-file", type=Path)
    parser.add_argument("--command-toolchain")
    parser.add_argument("--environment-toolchain")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.channel:
        if (
            not args.profile
            or args.selector_file
            or args.command_toolchain
            or args.environment_toolchain
        ):
            parser.error(
                "Explicit channel mode requires --profile and cannot override source selection inputs"
            )
        lock = pin(args.channel, args.target, args.profile)
    else:
        if args.profile:
            parser.error("Base-state mode retains the original selected profile")
        raw = args.base_state.read_bytes()
        state = json.loads(raw)
        selection = select_toolchain(
            args.selector_file.read_bytes() if args.selector_file else None,
            state["settings_toml"].encode(),
            base=state["base"],
            target=args.target,
            installed_toolchains=state["installed_toolchains"],
            command_toolchain=args.command_toolchain,
            environment_toolchain=args.environment_toolchain,
        )
        selection["base_state_sha256"] = hashlib.sha256(raw).hexdigest()
        lock = pin_selection(selection)
    with open(args.output, "w") as stream:
        json.dump(lock, stream, indent=2)
        stream.write("\n")
    print(
        json.dumps(
            {
                "toolchain": lock.get(
                    "toolchain", lock.get("selection", {}).get("toolchain")
                ),
                "date": lock.get("date"),
                "rust_version": lock.get("rust_version"),
                "provided_by_base": lock.get("kind") == "base-toolchain",
            }
        )
    )


if __name__ == "__main__":
    main()
