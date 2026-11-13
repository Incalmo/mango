"""Run pinned Harbor tooling in a separate, unprivileged controller container."""

import argparse
import json
import sys
from pathlib import Path


def main():
    from mango import HARNESS

    root, workspace = map(Path, sys.argv[1:3])
    action, *extra = sys.argv[3:]
    if action == "compose":
        from mango.harbor.compose import generate_compose
        from mango.runtime.manifest import BuildManifest

        manifest = BuildManifest.model_validate_json((workspace / "images.json").read_text())
        document = generate_compose(workspace, manifest)
        document["grading"] = {
            agent: ["mango", "run", str(root), "--workspace", str(workspace), "--agent", agent]
            for agent in ("oracle", "nop")
        }
        document["note"] = "Compose starts runtime services. Use the grading commands for Harbor oracle/nop trials."
        print(json.dumps(document, indent=2))
    else:
        from mango.harbor.verdict import run_trial

        parser = argparse.ArgumentParser(prog="mango run")
        parser.add_argument("--agent", default="oracle")
        options, harbor_args = parser.parse_known_args(extra)
        if harbor_args[:1] == ["--"]:
            harbor_args = harbor_args[1:]
        run_trial(root, workspace, HARNESS, options.agent, harbor_args)


if __name__ == "__main__":
    main()
