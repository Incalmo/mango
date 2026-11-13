"""Small, standard-library-only launcher for environment-pinned harnesses.

The launcher understands the lock format; the selected harness owns evaluation,
builds and runtime tooling. It can also launch revisions predating this module.
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

from mango import cache

ROOT = Path(__file__).resolve().parents[2]
ACTIONS = {
    "validate",
    "declaration",
    "validate-contract",
    "build",
    "fetch",
    "check",
    "load",
    "prepare",
    "compose",
    "run",
    "publish",
    "export",
    "offline-export",
    "offline-import",
}


def read_lock(path):
    document = json.loads(path.read_text())
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError(f"{path}: unsupported mango.lock version")
    pin = document.get("mango", {})
    if not isinstance(pin, dict) or set(pin) != {"url", "rev", "narHash"}:
        raise ValueError(f"{path}: expected mango url, rev and narHash")
    if not isinstance(pin["url"], str) or not pin["url"].startswith("https://"):
        raise ValueError(f"{path}: mango URL must use HTTPS")
    if not re.fullmatch(r"[0-9a-f]{40}", str(pin["rev"])):
        raise ValueError(f"{path}: mango rev must be a full commit hash")
    if not re.fullmatch(r"sha256-[A-Za-z0-9+/]{43}=", str(pin["narHash"])):
        raise ValueError(f"{path}: mango narHash must be a SHA-256 SRI hash")
    return pin


def ensure_lock(root):
    path = root / "mango.lock"
    if not path.exists():
        # A release's default is immutable. Never resolve a moving branch here.
        default = ROOT / "mango.lock"
        read_lock(default)
        try:
            with path.open("x") as stream:
                stream.write(default.read_text())
            print(f"Pinned Mango in {path}", file=sys.stderr)
        except FileExistsError:
            pass
    return read_lock(path)


def resolve(pin, env=None):
    source = {"type": "git", "allRefs": True, **pin}
    expression = (
        "(builtins.fetchTree (builtins.fromJSON "
        + json.dumps(json.dumps(source))
        + ")).outPath"
    )
    result = subprocess.run(
        [
            "nix",
            "--extra-experimental-features",
            "nix-command flakes",
            "eval",
            "--impure",
            "--json",
            "--expr",
            expression,
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        env=env,
    )
    harness = Path(json.loads(result.stdout))
    if not (harness / "shell.nix").is_file():
        raise ValueError(f"{harness}: locked source is not a Mango harness")
    return harness


def invocation(harness, arguments, actions=None):
    # Call the backend directly to avoid recursively launching ourselves, even
    # when the chosen revision also has a version-aware frontend.
    backend = ["python", "-c", "from mango.cli import main; main()"]
    if actions is None:
        commands = [backend + arguments]
    else:
        root, workspace, action, extra = arguments
        common = [root, "--workspace", workspace]
        commands = [
            backend + common + [step] + (extra if step == action else [])
            for step in actions
        ]
    return [
        "nix-shell",
        str(harness / "shell.nix"),
        "--run",
        " && ".join(shlex.join(command) for command in commands),
    ]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="mango",
        description="Build and load an environment using its mango.lock.",
        epilog="Legacy `mango ENV ACTION ...` remains supported. Nix is required.",
    )
    parser.add_argument("action", choices=sorted(ACTIONS | {"lock"}))
    parser.add_argument("environment", type=Path)
    parser.add_argument("--workspace", type=Path)
    try:
        with ExitStack() as stack:
            return launch(argv, parser, stack)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"mango: {error}\n")


def launch(argv, parser, stack):
    if argv and argv[0] not in ACTIONS | {"lock", "--help", "-h"}:
        # Preserve the old CLI's flags and semantics, but select its harness
        # from the environment lock when one exists.
        root = Path(argv[0]).resolve()
        if not (root / "default.nix").is_file():
            parser.error(f"no default.nix at {root}")
        env = stack.enter_context(cache.session(root))
        lock = root / "mango.lock"
        harness = resolve(read_lock(lock), env=env) if lock.exists() else ROOT
        command = invocation(harness, argv)
    else:
        args, extra = parser.parse_known_args(argv)
        root = args.environment.resolve()
        if not (root / "default.nix").is_file():
            parser.error(f"no default.nix at {root}")
        if args.action == "lock" and extra:
            parser.error(
                "lock takes no additional arguments; edit mango.lock to upgrade"
            )
        workspace = (args.workspace or root.parent / "build" / root.name).resolve()
        if workspace.is_relative_to(root) or root.is_relative_to(workspace):
            parser.error("workspace must be separate from the environment")
        env = stack.enter_context(cache.session(root))
        pin = ensure_lock(root)
        harness = resolve(pin, env=env)
        if args.action == "lock":
            print(
                json.dumps(
                    {"lock": str(root / "mango.lock"), "harness": str(harness)}
                )
            )
            return
        steps = {
            "load": ["fetch", "load"],
            "compose": ["fetch", "load", "compose"],
            "run": ["fetch", "load", "run"],
        }.get(args.action, [args.action])
        command = invocation(
            harness, [str(root), str(workspace), args.action, extra], steps
        )
    raise SystemExit(subprocess.run(command, env=env).returncode)

if __name__ == "__main__":
    main()
