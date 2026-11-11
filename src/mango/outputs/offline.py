"""Retain build closures, then repeat ordinary derivations in Nix network sandboxes.

Run freeze after online input realization. check refuses fixed-output derivations
(they are network-enabled by Nix), missing direct inputs, and sandbox overrides.
Nix --check rebuilds into fresh output paths and compares against existing outputs.

Everything is computed from the targets' own derivation graph (one
`nix derivation show --recursive` call), never from the rest of the host store.
"""

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

BATCH = 500


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def store_path(path):
    return path if path.startswith("/") else "/nix/store/" + path


def derivation_graph(drvs):
    data = json.loads(run("nix", "derivation", "show", "--recursive", *drvs))
    # Nix 2.34 uses a versioned envelope and relative keys; older versions
    # return a flat mapping with absolute keys.
    data = data.get("derivations", data)
    return {store_path(name): definition for name, definition in data.items()}


def input_drvs(definition):
    drvs = definition.get("inputDrvs", definition.get("inputs", {}).get("drvs", {}))
    return {
        store_path(drv): selection
        if isinstance(selection, list)
        else selection["outputs"]
        for drv, selection in drvs.items()
    }


def input_srcs(definition):
    srcs = definition.get("inputSrcs", definition.get("inputs", {}).get("srcs", []))
    return [store_path(path) for path in srcs]


def output_paths(drv, definition, names=None):
    paths = []
    for name in names or definition["outputs"]:
        path = definition["outputs"][name].get("path") or definition["env"].get(name)
        if not path:
            raise ValueError("Unresolved output: " + drv + "^" + name)
        paths.append(store_path(path))
    return paths


def required_inputs(graph, drv):
    definition = graph[drv]
    env = definition["env"]
    if env.get("outputHash") or env.get("__noChroot") or env.get("__impure"):
        raise ValueError(
            "Offline check requires an ordinary sandboxed derivation: " + drv
        )
    paths = input_srcs(definition)
    for dependency, names in input_drvs(definition).items():
        paths += output_paths(dependency, graph[dependency], names)
    return paths


def dependents(graph, targets):
    """Derivations that are targets or transitively build on one.

    A fresh build of the targets invalidates these too (image layers and
    configs, for example), so their own build inputs must be retained.
    """
    result = {}
    for start in graph:
        stack = [(start, False)]
        while stack:
            drv, expanded = stack.pop()
            if drv in result:
                continue
            inputs = input_drvs(graph[drv])
            if expanded:
                result[drv] = drv in targets or any(result[i] for i in inputs)
            else:
                stack.append((drv, True))
                stack.extend((i, False) for i in inputs if i not in result)
    return {drv for drv, affected in result.items() if affected}


def requisites(paths):
    paths = sorted(set(paths))
    result = set()
    for index in range(0, len(paths), BATCH):
        result.update(
            run(
                "nix-store", "--query", "--requisites", *paths[index : index + BATCH]
            ).splitlines()
        )
    return result


def resolve(derivations):
    return [
        p if p.endswith(".drv") else run("nix-store", "--query", "--deriver", p)
        for p in derivations
    ]


def freeze(receipt, drvs):
    roots = receipt.resolve().parent / (receipt.stem + "-roots")
    # Drop roots from a previous freeze so superseded inputs can be collected.
    if roots.exists():
        shutil.rmtree(roots)
    roots.mkdir(parents=True)
    graph = derivation_graph(drvs)
    rebuild = dependents(graph, set(drvs))
    needed = []
    for drv in rebuild:
        needed += required_inputs(graph, drv)
        needed += output_paths(drv, graph[drv])
    paths = requisites([*drvs, *needed])
    missing = [path for path in sorted(paths) if not Path(path).exists()]
    if missing:
        raise ValueError("Missing input: " + missing[0])
    ordered = sorted(paths)
    for index in range(0, len(ordered), 100):
        subprocess.run(
            [
                "nix",
                "build",
                "--offline",
                "--out-link",
                str(roots / f"input-{index}"),
                *ordered[index : index + 100],
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    sizes = json.loads(run("nix", "path-info", "--json", *ordered))
    sizes = sizes.values() if isinstance(sizes, dict) else sizes
    receipt.write_text(
        json.dumps(
            {
                "derivations": drvs,
                "rebuild_intermediate_derivations": sorted(rebuild),
                "paths": ordered,
                "deduplicated_nar_bytes": sum(x["narSize"] for x in sizes),
            },
            indent=2,
        )
        + "\n"
    )


def check(receipt_path, drvs):
    receipt = json.loads(receipt_path.read_text())
    if drvs != receipt["derivations"]:
        raise ValueError("Derivations differ from the frozen receipt")
    retained = set(receipt["paths"])
    for path in retained:
        if not Path(path).exists():
            raise ValueError("Retained input missing: " + path)
    graph = derivation_graph(drvs)
    checks = []
    for drv in drvs:
        for path in required_inputs(graph, drv):
            if path not in retained:
                raise ValueError("Unfrozen input: " + path)
        start = time.monotonic()
        subprocess.run(
            [
                "nix-store",
                "--realise",
                drv,
                "--check",
                "--keep-failed",
                "--option",
                "sandbox",
                "true",
                "--option",
                "sandbox-fallback",
                "false",
                "--option",
                "substitute",
                "false",
                "--option",
                "builders",
                "",
                "--option",
                "max-jobs",
                "1",
            ],
            check=True,
        )
        checks.append(
            {
                "derivation": drv,
                "seconds": time.monotonic() - start,
                "fresh_output_comparison": "equal",
            }
        )
    receipt_path.with_suffix(".check.json").write_text(
        json.dumps(checks, indent=2) + "\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["freeze", "check"])
    parser.add_argument("receipt", type=Path)
    parser.add_argument("derivations", nargs="+")
    args = parser.parse_args()
    drvs = resolve(args.derivations)
    (freeze if args.action == "freeze" else check)(args.receipt, drvs)


if __name__ == "__main__":
    main()
