#!/usr/bin/env python3
"""Resolve a Go workspace / local-module project to a hash-pinned module cache.

REMOTE HOST ONLY (``MANGO_REMOTE_AUTHOR=1`` plus Docker). This extends
``mango.authoring.go_lock`` to projects whose original build uses ``go.work``
and/or source-local ``replace M => ./relative/dir`` directives. It reuses go_lock's
immutable-base, public-proxy, size, ``h1:`` verification, ``.info`` normalisation
and lock-builder code. It is *authoring evidence*, not a proof that the
application compiles: the offline Mango build must still succeed.

Request file (``python -m mango.authoring.go_workspace requests.json --output out.json``)::

    {"requests": [{
        "id": "cadence",
        "base": "golang@sha256:<64 hex>",
        "goproxy": "https://proxy.golang.org",
        # Where the original build ran. ``project_dir`` is recorded, never used on
        # the author host; ``run_dir`` is project-relative and must be a declared
        # module directory.
        "original": {"project_dir": "/src/cadence", "run_dir": ".", "goflags": ""},
        # Exact manifest bytes, project-relative dirs. ``go_sum: null`` = the file
        # is absent (distinct from ``""`` = present and empty). Every key required.
        "modules": [{"dir": ".", "go_mod": "...", "go_sum": "..."},
                    {"dir": "cmd/server", "go_mod": "...", "go_sum": null}],
        # Exactly one of:
        "workspace": {"mode": "go.work", "dir": ".", "go_work": "...",
                      "go_work_sum": null},
        "workspace": {"mode": "none"},     # request declares NO effective workspace
        # optional, as go_lock: scope, gosumdb, graph_mod_files,
        # allow_missing_graph_mods, limits, platform
    }]}

Semantics (each violation fails closed with a specific ``reason_code``):

* One isolated project directory in the container keeps the *relative layout*;
  no manifest is rewritten, flattened or merged. Go sees the original ``go.work``
  (``GOWORK`` = the staged file, never silently ``off``) or, only when the request
  declares ``workspace.mode == "none"``, ``GOWORK=off``.
* Local replacements (go.mod or go.work) must be ``./``/``../`` relative, normalise
  to a directory inside the project, and name a directory with a *declared*
  manifest whose ``module`` equals the replaced path. Absolute, escaping, missing
  (e.g. Harbor's ``=> ../`` with no go.mod) and mismatched targets are rejected.
  Cycles (cadence children replacing ``=> ../..``) are handled by directory identity.
* Every declared module must be referenced (workspace ``use``/run dir/local replace
  target); no duplicate module paths. Fixed-version remote replacements keep go_lock's
  rules. ``go``/``toolchain`` directives are checked against the base's real Go.
* ``go mod edit -json`` / ``go work edit -json`` (read-only) cross-check this
  module's own directive parsing. Original file hashes (plus an exact manifest file
  inventory, so nothing new such as ``go.work.sum`` may appear) are verified before
  and after Go runs.
* ``go mod download`` entries without a module-cache artifact (workspace members and
  local replacement targets) are dropped only when their path *and* directory match a
  declared module; any other such entry fails.

Limitations: ``go mod download`` is run once, from ``run_dir``; modules reachable only
through other build contexts are not covered. Source patches and source-tree equality
stay the caller's duty. See ``docs/go-workspaces.md``.
"""

from __future__ import annotations

import argparse
import copy
import json
import posixpath
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from mango.authoring import go_lock as lock
from mango.authoring.go_lock import ResolverError

RESOLVER_VERSION = "1"
KIND = "mango.go-workspace-lock-receipt"
PROJECT = f"{lock.WORK}/project"
PROJECT_PLACEHOLDER = "${MANGO_GO_PROJECT}"
CACHE_PLACEHOLDER = "${MANGO_GO_CACHE}"

REQUEST_KEYS = {
    "id", "base", "goproxy", "scope", "gosumdb", "graph_mod_files",
    "allow_missing_graph_mods", "limits", "platform",
    "original", "modules", "workspace",
}  # fmt: skip
COMMON_KEYS = REQUEST_KEYS - {"original", "modules", "workspace"}
ORIGINAL_KEYS = {"project_dir", "run_dir", "goflags"}
MODULE_KEYS = {"dir", "go_mod", "go_sum"}
WORKSPACE_KEYS = {"mode", "dir", "go_work", "go_work_sum"}
MANIFEST_NAMES = ("go.mod", "go.sum", "go.work", "go.work.sum")


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------
def check_dir(value: object, what: str) -> str:
    if (
        not isinstance(value, str)
        or not lock.DIR_RE.match(value)
        or ".." in value.split("/")
    ):
        raise ResolverError(f"unsafe {what} {value!r}", "invalid-request")
    return value


def join_rel(directory: str, name: str) -> str:
    return posixpath.normpath(posixpath.join(directory, name))


def check_text(value: object, what: str, cap: int) -> str:
    if not isinstance(value, str):
        raise ResolverError(f"{what} must be text", "invalid-request")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ResolverError(f"{what} is not valid UTF-8 text", "invalid-request")
    if size > cap:
        raise ResolverError(f"{what} exceeds {cap} bytes", "limit-exceeded")
    return value


def check_goflags(value: object) -> str:
    if not isinstance(value, str):
        raise ResolverError("original.goflags must be text", "invalid-request")
    for token in value.split():
        name = token.split("=", 1)[0]
        if token == "-mod=readonly" or name not in (
            "-mod",
            "-modfile",
            "-workfile",
            "-overlay",
        ):
            continue
        raise ResolverError(
            f"original goflags {token!r} would change manifests or module selection "
            "in a way authoring does not reproduce",
            "unsupported-goflags",
        )
    return value


def validate_request(request: object) -> dict:
    """Validate against the explicit schema and return a normalised copy."""
    if not isinstance(request, dict):
        raise ResolverError("request must be an object", "invalid-request")
    unknown = set(request) - REQUEST_KEYS
    if unknown:
        raise ResolverError(
            f"unsupported request keys: {sorted(unknown)}", "invalid-request"
        )
    for key in ("original", "modules", "workspace"):
        if key not in request:
            raise ResolverError(f"missing request key: {key}", "invalid-request")
    # Common fields (id/base/proxy/limits/...) are validated by go_lock itself.
    common = {k: copy.deepcopy(v) for k, v in request.items() if k in COMMON_KEYS}
    common["modules"] = [{"dir": ".", "go_mod": ""}]
    normal = lock.validate_request(common)
    del normal["modules"]
    limits = normal["limits"]

    original = request["original"]
    if not isinstance(original, dict) or set(original) - ORIGINAL_KEYS:
        raise ResolverError("original: unsupported shape", "invalid-request")
    project_dir = original.get("project_dir")
    if (
        not isinstance(project_dir, str)
        or not project_dir.startswith("/")
        or project_dir == "/"
        or posixpath.normpath(project_dir) != project_dir
        or ".." in project_dir.split("/")
    ):
        raise ResolverError(
            "original.project_dir must be an explicit normalised absolute directory",
            "invalid-request",
        )
    normal["original"] = {
        "project_dir": project_dir,
        "run_dir": check_dir(original.get("run_dir", "."), "original.run_dir"),
        "goflags": check_goflags(original.get("goflags", "")),
    }

    modules = request["modules"]
    if not isinstance(modules, list) or not modules:
        raise ResolverError("modules must be a non-empty list", "invalid-request")
    clean, seen = [], set()
    for index, module in enumerate(modules):
        if not isinstance(module, dict) or set(module) != MODULE_KEYS:
            raise ResolverError(
                f"modules[{index}]: needs exactly dir, go_mod, go_sum "
                "(go_sum null = absent file)",
                "invalid-request",
            )
        directory = check_dir(module["dir"], f"modules[{index}].dir")
        if directory in seen:
            raise ResolverError(
                f"duplicate module dir {directory!r}", "invalid-request"
            )
        seen.add(directory)
        go_sum = module["go_sum"]
        clean.append(
            {
                "dir": directory,
                "go_mod": check_text(
                    module["go_mod"], f"{directory}/go.mod", limits["max_mod_bytes"]
                ),
                "go_sum": None
                if go_sum is None
                else check_text(
                    go_sum, f"{directory}/go.sum", limits["max_go_sum_bytes"]
                ),
            }
        )
    normal["modules"] = clean
    if normal["original"]["run_dir"] not in seen:
        raise ResolverError(
            "original.run_dir has no declared module manifest", "run-dir-missing"
        )

    workspace = request["workspace"]
    if not isinstance(workspace, dict) or set(workspace) - WORKSPACE_KEYS:
        raise ResolverError("workspace: unsupported shape", "invalid-request")
    mode = workspace.get("mode")
    if mode == "none":
        if set(workspace) != {"mode"}:
            raise ResolverError(
                "workspace mode none takes no other keys", "invalid-request"
            )
        normal["workspace"] = {"mode": "none"}
    elif mode == "go.work":
        if set(workspace) != WORKSPACE_KEYS:
            raise ResolverError(
                "workspace go.work needs mode, dir, go_work, go_work_sum "
                "(go_work_sum null = absent file)",
                "invalid-request",
            )
        work_sum = workspace["go_work_sum"]
        normal["workspace"] = {
            "mode": "go.work",
            "dir": check_dir(workspace["dir"], "workspace.dir"),
            "go_work": check_text(
                workspace["go_work"], "go.work", limits["max_mod_bytes"]
            ),
            "go_work_sum": None
            if work_sum is None
            else check_text(work_sum, "go.work.sum", limits["max_go_sum_bytes"]),
        }
    else:
        raise ResolverError(
            f"unsupported workspace mode {mode!r} (use 'go.work' or 'none')",
            "unsupported-mode",
        )
    return normal


def directives(text: str):
    """Yield (directive, tokens) for a go.mod/go.work text, handling ``( ... )`` blocks."""
    block = None
    for raw in text.splitlines():
        line = lock._strip_comment(raw).strip()
        if not line:
            continue
        if block:
            if line == ")":
                block = None
                continue
            yield block, [lock._unquote(t) for t in line.split()]
            continue
        tokens = line.split()
        if len(tokens) >= 2 and tokens[1] == "(" and len(tokens) == 2:
            block = tokens[0]
            continue
        yield tokens[0], [lock._unquote(t) for t in tokens[1:]]


def parse_go_work(data: bytes) -> dict:
    """``go``/``toolchain``/``use``/``replace`` of a go.work; unknown directives fail."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ResolverError("go.work is not UTF-8", "invalid-go-work") from None
    parsed: dict = {"go": None, "toolchain": None, "use": [], "replaces": []}
    for directive, tokens in directives(text):
        if directive in ("go", "toolchain") and len(tokens) == 1:
            parsed[directive] = tokens[0]
        elif directive == "use" and len(tokens) == 1:
            parsed["use"].append(tokens[0])
        elif directive == "replace":
            if "=>" not in tokens or tokens.index("=>") in (0, len(tokens) - 1):
                raise ResolverError("malformed replace directive", "invalid-go-work")
            arrow = tokens.index("=>")
            parsed["replaces"].append(
                {"old": " ".join(tokens[:arrow]), "new": tokens[arrow + 1 :]}
            )
        elif directive == "godebug":
            continue
        else:
            raise ResolverError(
                f"unsupported go.work directive {directive!r}", "invalid-go-work"
            )
    return parsed


def classify_replace(replace: dict, where: str) -> str:
    """ "local" or "remote"; reject every local-ish form that is not ``./``/``../``."""
    new = replace["new"]
    target = new[0]
    if (
        "\\" in target
        or target.startswith(("/", "~"))
        or re.match(r"^[A-Za-z]:", target)
    ):
        raise ResolverError(
            f"{where}: replace {replace['old']} => absolute/unsupported path {target!r}",
            "go-local-absolute",
        )
    if target.startswith(("./", "../")) or target in (".", ".."):
        if len(new) != 1:
            raise ResolverError(
                f"{where}: local replacement {target!r} must not carry a version",
                "go-mod-replace",
            )
        return "local"
    if len(new) == 1:
        raise ResolverError(
            f"{where}: replace {replace['old']} => {target!r} is neither a "
            "relative directory nor a versioned module",
            "go-mod-replace",
        )
    return "remote"


def resolve_local(base_dir: str, target: str, where: str) -> str:
    joined = posixpath.normpath(posixpath.join(base_dir, target))
    if joined == ".." or joined.startswith("../") or joined.startswith("/"):
        raise ResolverError(
            f"{where}: local replacement {target!r} escapes the declared project",
            "go-local-escape",
        )
    return joined


def analyze(request: dict) -> dict:
    """Static, network-free model of the project: identities, edges, selection."""
    modules = {m["dir"]: m for m in request["modules"]}
    parsed: dict[str, dict] = {}
    by_path: dict[str, str] = {}
    for directory, module in modules.items():
        info = lock.parse_go_mod(module["go_mod"].encode())
        name = info["module"]
        if not name or any(ch.isspace() for ch in name):
            raise ResolverError(
                f"{directory}/go.mod: missing module directive", "invalid-go-mod"
            )
        if name in by_path:
            raise ResolverError(
                f"module {name} is declared by both {by_path[name]!r} and {directory!r}",
                "module-conflict",
            )
        by_path[name] = directory
        parsed[directory] = info

    workspace = request["workspace"]
    work = None
    use: list[str] = []
    work_dir = None
    if workspace["mode"] == "go.work":
        work_dir = workspace["dir"]
        work = parse_go_work(workspace["go_work"].encode())
        where = f"{join_rel(work_dir, 'go.work')}"
        for entry in work["use"]:
            if (
                not (entry.startswith(("./", "../")) or entry in (".", ".."))
                or "\\" in entry
            ):
                raise ResolverError(
                    f"{where}: use {entry!r} must be a relative directory",
                    "go-work-use",
                )
            target = resolve_local(work_dir, entry, f"{where}: use")
            if target not in modules:
                raise ResolverError(
                    f"{where}: use {entry!r} -> {target!r} has no declared go.mod",
                    "go-work-use-missing",
                )
            if target in use:
                raise ResolverError(
                    f"{where}: {target!r} is used twice", "module-conflict"
                )
            use.append(target)
        if not use:
            raise ResolverError(f"{where}: no use directives", "go-work-use")
        run_dir = request["original"]["run_dir"]
        if run_dir not in use:
            raise ResolverError(
                f"run_dir {run_dir!r} is not a workspace member",
                "run-dir-not-in-workspace",
            )
        if work_dir != "." and not (run_dir + "/").startswith(work_dir + "/"):
            raise ResolverError(
                "go.work directory is not an ancestor of run_dir",
                "run-dir-not-in-workspace",
            )

    local, remote = [], []
    sources = [(d, f"{join_rel(d, 'go.mod')}", parsed[d]["replaces"]) for d in modules]
    if work is not None:
        sources.append((work_dir, join_rel(work_dir, "go.work"), work["replaces"]))
    for base_dir, where, replaces in sources:
        for replace in replaces:
            if classify_replace(replace, where) == "remote":
                remote.append(
                    {"source": where, "old": replace["old"], "new": replace["new"]}
                )
                continue
            old_path = replace["old"].split()[0]
            target = resolve_local(base_dir, replace["new"][0], where)
            if target not in modules:
                raise ResolverError(
                    f"{where}: replace {old_path} => {replace['new'][0]!r} -> {target!r} "
                    "has no declared go.mod (Go needs a go.mod in a replacement "
                    "directory; the target is not guessed or skipped)",
                    "go-local-missing",
                )
            if parsed[target]["module"] != old_path:
                raise ResolverError(
                    f"{where}: {target!r}/go.mod declares {parsed[target]['module']!r}, "
                    f"replaced as {old_path!r}",
                    "go-local-module-mismatch",
                )
            local.append(
                {"source": where, "old": replace["old"], "target_dir": target,
                 "from_dir": base_dir}
            )  # fmt: skip

    run_dir = request["original"]["run_dir"]
    roots = set(use) if work is not None else {run_dir}
    # go.work-level local replacements are applied by the workspace itself.
    roots |= {i["target_dir"] for i in local if i["source"].endswith("go.work")}
    edges: dict[str, set[str]] = {}
    for item in local:
        if not item["source"].endswith("go.work"):
            edges.setdefault(item["from_dir"], set()).add(item["target_dir"])
    reached, queue = set(roots), sorted(roots)
    while queue:  # identity-based: cycles terminate at the visited set
        for nxt in sorted(edges.get(queue.pop(), ())):
            if nxt not in reached:
                reached.add(nxt)
                queue.append(nxt)
    loose = sorted(set(modules) - reached)
    if loose:
        raise ResolverError(
            f"declared modules not reachable from the {'workspace' if work else 'run dir'}"
            f" or any local replacement: {loose}",
            "module-unreferenced",
        )

    # go.sum authorities: exactly the files Go consults for this selection.
    if work is not None:
        authority = [join_rel(d, "go.sum") for d in use]
        sum_texts = [modules[d]["go_sum"] for d in use] + [workspace["go_work_sum"]]
        authority.append(join_rel(work_dir, "go.work.sum"))
    else:
        authority = [join_rel(run_dir, "go.sum")]
        sum_texts = [modules[run_dir]["go_sum"]]
    sums: dict = {}
    for text in sum_texts:
        for key, hashes in lock.parse_go_sum((text or "").encode()).items():
            sums.setdefault(key, set()).update(hashes)

    return {
        "mode": workspace["mode"],
        "run_dir": run_dir,
        "workdir": f"{PROJECT}/{run_dir}" if run_dir != "." else PROJECT,
        "work_dir": work_dir,
        "work_path": join_rel(work_dir, "go.work") if work_dir is not None else None,
        "use": use,
        "work": work,
        "parsed": parsed,
        "module_paths": {d: p["module"] for d, p in parsed.items()},
        "local": local,
        "remote": remote,
        "sums": sums,
        "sum_authorities": authority,
    }


def screen(plan: dict, go_version: str | None) -> None:
    """go_lock's screening on remote replacements + go/toolchain directives."""
    for directory, info in plan["parsed"].items():
        remote = [
            r for r in info["replaces"] if classify_replace(r, "go.mod") == "remote"
        ]
        lock.screen_go_mod(
            dict(info, replaces=remote), go_version, f"{join_rel(directory, 'go.mod')}"
        )
    if plan["work"] is not None:
        work = plan["work"]
        remote = [
            r for r in work["replaces"] if classify_replace(r, "go.work") == "remote"
        ]
        lock.screen_go_mod(
            dict(work, replaces=remote, requires=[], module=None),
            go_version,
            plan["work_path"],
        )


def manifest_files(request: dict) -> list[dict]:
    """Every original manifest file (present or absent) with its exact bytes."""
    files = []

    def add(path: str, role: str, text: str | None) -> None:
        data = None if text is None else text.encode()
        files.append({"path": path, "role": role, "bytes": data})

    for module in sorted(request["modules"], key=lambda m: m["dir"]):
        add(join_rel(module["dir"], "go.mod"), "go.mod", module["go_mod"])
        add(join_rel(module["dir"], "go.sum"), "go.sum", module["go_sum"])
    workspace = request["workspace"]
    if workspace["mode"] == "go.work":
        add(join_rel(workspace["dir"], "go.work"), "go.work", workspace["go_work"])
        add(
            join_rel(workspace["dir"], "go.work.sum"),
            "go.work.sum",
            workspace["go_work_sum"],
        )
    return files


def manifest_receipt(files: list[dict]) -> list[dict]:
    return [
        {
            "path": f["path"],
            "role": f["role"],
            "present": f["bytes"] is not None,
            "sha256": None if f["bytes"] is None else lock.sha256_hex(f["bytes"]),
            "size": None if f["bytes"] is None else len(f["bytes"]),
        }
        for f in files
    ]


def public_path(text: str) -> str:
    """Never leak author-host/container paths into receipts."""
    return text.replace(PROJECT, PROJECT_PLACEHOLDER) if text else text


# --------------------------------------------------------------------------
# Container work (Docker and Go; remote host only)
# --------------------------------------------------------------------------
class _DownloadView:
    """Wrap a container; identify (never guess) cache-less ``go mod download`` entries."""

    def __init__(self, container, plan: dict) -> None:
        self._container = container
        self._plan = plan
        self.local_modules: list[dict] = []

    def __getattr__(self, name):
        return getattr(self._container, name)

    def exec(self, argv, env=None, workdir=None, check=True):
        done = self._container.exec(argv, env=env, workdir=workdir, check=check)
        if (
            list(argv[:3]) != ["go", "mod", "download"]
            or not (done.stdout or "").strip()
        ):
            return done
        keep = []
        for item in lock.parse_download_json(done.stdout):
            cached = any(
                item.get(k) for k in ("Zip", "GoMod", "Info", "Sum", "GoModSum")
            )
            if item.get("Error") or cached:
                keep.append(item)
            else:
                self.local_modules.append(self._identify(item))
        return subprocess.CompletedProcess(
            done.args,
            done.returncode,
            "\n".join(json.dumps(i) for i in keep),
            done.stderr,
        )

    def _identify(self, item: dict) -> dict:
        path, directory = item.get("Path"), item.get("Dir")
        for dirname, name in self._plan["module_paths"].items():
            staged = PROJECT if dirname == "." else f"{PROJECT}/{dirname}"
            if name == path and directory == staged:
                return {"module": path, "dir": dirname}
        raise ResolverError(
            f"go mod download reported {path!r} (dir {public_path(str(directory))!r}) "
            "without module-cache artifacts and it matches no declared local module; "
            "unsupported output is not dropped",
            "go-output-unsupported",
        )


def _hashes(container, files: list[dict]) -> dict[str, str]:
    paths = [f"{PROJECT}/{f['path']}" for f in files if f["bytes"] is not None]
    return lock.container_hashes(container, paths)


def _inventory(container) -> set[str]:
    argv = ["find", PROJECT, "("]
    for index, name in enumerate(MANIFEST_NAMES):
        argv += (["-o"] if index else []) + ["-name", name]
    argv.append(")")
    return {line for line in container.exec(argv).stdout.splitlines() if line}


def _check_stage(container, files: list[dict], expected: dict, step: str) -> None:
    if _hashes(container, files) != expected:
        raise ResolverError(
            f"manifest bytes changed {step} (original selection was not preserved)",
            "go-mod-mutated" if step == "after Go ran" else "source-mutated",
        )
    wanted = {f"{PROJECT}/{f['path']}" for f in files if f["bytes"] is not None}
    extra = _inventory(container) ^ wanted
    if extra:
        raise ResolverError(
            f"manifest files differ from the declared set {step}: "
            f"{sorted(public_path(p) for p in extra)[:5]}",
            "go-mod-mutated" if step == "after Go ran" else "source-mutated",
        )


def _stage(container, files: list[dict], tmp: Path) -> dict[str, str]:
    present = [f for f in files if f["bytes"] is not None]
    dirs = sorted({posixpath.dirname(f"{PROJECT}/{f['path']}") for f in present})
    container.exec(["mkdir", "-p", *dirs])
    for index, item in enumerate(present):
        local = tmp / f"stage-{index}"
        local.write_bytes(item["bytes"])
        local.chmod(0o600)
        container.copy_in(local, f"{PROJECT}/{item['path']}")
        local.unlink()
    expected = {f"{PROJECT}/{f['path']}": lock.sha256_hex(f["bytes"]) for f in present}
    _check_stage(container, files, expected, "during copy")
    return expected


def _replace_tuples(items) -> list[tuple]:
    out = []
    for item in items or []:
        old, new = item.get("Old") or {}, item.get("New") or {}
        out.append(
            (
                old.get("Path", ""),
                old.get("Version", ""),
                new.get("Path", ""),
                new.get("Version", ""),
            )
        )
    return sorted(out)


def _own_tuples(replaces: list[dict]) -> list[tuple]:
    out = []
    for r in replaces:
        old = r["old"].split()
        new = r["new"]
        out.append(
            (
                old[0],
                old[1] if len(old) > 1 else "",
                new[0],
                new[1] if len(new) > 1 else "",
            )
        )
    return sorted(out)


def _json_exec(container, argv, env, workdir, what) -> dict:
    done = container.exec(argv, env=env, workdir=workdir, check=False)
    try:
        value = json.loads(done.stdout or "")
    except ValueError:
        value = None
    if done.returncode != 0 or not isinstance(value, dict):
        raise ResolverError(
            f"go rejected {what}: {lock.redact((done.stderr or done.stdout or '')[-300:])}",
            "invalid-go-work" if "work" in what else "invalid-go-mod",
        )
    return value


def cross_check(container, plan: dict, env: dict) -> None:
    """Read-only ``go {mod,work} edit -json`` must agree with this module's parser."""
    off = dict(env, GOWORK="off")
    for directory, info in sorted(plan["parsed"].items()):
        data = _json_exec(
            container,
            ["go", "mod", "edit", "-json"],
            off,
            f"{PROJECT}/{directory}" if directory != "." else PROJECT,
            f"{join_rel(directory, 'go.mod')}",
        )
        if (data.get("Module") or {}).get("Path") != info["module"] or _replace_tuples(
            data.get("Replace")
        ) != _own_tuples(info["replaces"]):
            raise ResolverError(
                f"{join_rel(directory, 'go.mod')}: Go's parse disagrees with the resolver's",
                "go-parse-mismatch",
            )
    if plan["work"] is not None:
        data = _json_exec(
            container,
            ["go", "work", "edit", "-json"],
            env,
            f"{PROJECT}/{plan['work_dir']}" if plan["work_dir"] != "." else PROJECT,
            plan["work_path"],
        )
        disk = sorted(u.get("DiskPath", "") for u in data.get("Use") or [])
        if disk != sorted(plan["work"]["use"]) or _replace_tuples(
            data.get("Replace")
        ) != _own_tuples(plan["work"]["replaces"]):
            raise ResolverError(
                f"{plan['work_path']}: Go's parse disagrees with the resolver's",
                "go-parse-mismatch",
            )


def authoring_env(request: dict, plan: dict) -> dict[str, str]:
    env = lock.go_env(request)
    env["GOFLAGS"] = request["original"]["goflags"]
    if plan["mode"] == "go.work":
        env["GOWORK"] = f"{PROJECT}/{plan['work_path']}"  # the ORIGINAL go.work
    else:  # declared: no effective workspace
        env["GOWORK"] = "off"
    return env


def offline_guidance(request: dict, plan: dict, files: list[dict]) -> dict:
    original = request["original"]
    run = posixpath.normpath(
        posixpath.join(original["project_dir"], original["run_dir"])
    )
    workspace = plan["mode"] == "go.work"
    gowork = f"{PROJECT_PLACEHOLDER}/{plan['work_path']}" if workspace else "off"
    common = {
        "GOTOOLCHAIN": "local",
        "GOENV": "off",
        "GOWORK": gowork,
        "GOFLAGS": original["goflags"],
    }
    return {
        "placeholders": {
            "MANGO_GO_CACHE": "absolute path of the writable copy of cache.prefix",
            "MANGO_GO_PROJECT": (
                "root of the source tree whose relative layout equals the original "
                f"project (original path {original['project_dir']})"
            ),
        },
        "project": {
            "original_project_dir": original["project_dir"],
            "run_dir": original["run_dir"],
            "run_directory": run,
            "workspace_mode": plan["mode"],
            "go_work": gowork if workspace else None,
        },
        "proxy_mode": {
            "env": {
                **common,
                "GOPROXY": f"file://{CACHE_PLACEHOLDER}/cache/download",
                "GOSUMDB": "off",
            },
            "note": (
                "Preferred. Run the ORIGINAL build commands and flags in the original run "
                "directory with the original manifests byte-identical (absent go.sum/"
                "go.work.sum files stay absent). Workspace builds keep the original "
                "go.work; GOWORK=off appears only because the request declared no "
                "workspace. GOSUMDB=off only because no sum database is reachable "
                "offline; the manifests' sums plus Mango's SHA-256 lock hashes are the "
                "integrity authority. Do not add -mod=mod, -modfile or -workfile."
            ),
        },
        "modcache_mode": {
            "env": {
                **common,
                "GOMODCACHE": CACHE_PLACEHOLDER,
                "GOPROXY": "off",
                "GOSUMDB": "off",
            },
            "note": "Go re-extracts zips from cache/download; .ziphash metadata is required.",
        },
        "manifest_sha256": manifest_receipt(files),
        "verified_by_offline_build": False,
        "limitations": [
            "authoring evidence only; it does not prove the application compiles",
            "source patches and source-tree equality are the caller's responsibility",
            "exact pinned versions only: no @v/list, so version queries fail offline",
            "go mod download ran once from run_dir; other build contexts are uncovered",
            "vendor/ is neither used nor checked",
        ],
    }


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------
def resolve(
    request: dict,
    fetch: Callable[[str, Path, int], str] = lock.download,
    container_factory: Callable[[str], object] = lock.Container,
) -> dict:
    lock.require_remote_author()
    request = validate_request(request)
    plan = analyze(request)
    screen(plan, None)
    files = manifest_files(request)
    receipt_files = manifest_receipt(files)
    state = {
        "modules": {}, "graph": {}, "graph_sums": [], "missing_graph": [], "bytes": 0,
    }  # fmt: skip
    budget = lock.Budget(request["limits"])
    with (
        container_factory(request["base"]) as container,
        tempfile.TemporaryDirectory() as tmp,
    ):
        work = Path(tmp)
        platform = container.image_platform()
        if platform != "linux/amd64":
            raise ResolverError(
                f"base resolves to {platform}, not linux/amd64", "base-platform"
            )
        container.exec(
            ["mkdir", "-p", lock.MODCACHE, f"{lock.WORK}/gopath",
             f"{lock.WORK}/gocache", f"{lock.WORK}/home"]
        )  # fmt: skip
        # Source-independent probe: nothing staged yet, so GOWORK=off is harmless.
        probe = container.exec(
            ["go", "env", "-json", "GOVERSION", "GOOS", "GOARCH"],
            env=lock.go_env(request),
            workdir="/",
            check=False,
        )
        if probe.returncode != 0:
            raise ResolverError("base image has no usable go toolchain", "go-missing")
        info = json.loads(probe.stdout)
        if (info.get("GOOS"), info.get("GOARCH")) != ("linux", "amd64"):
            raise ResolverError("base go is not linux/amd64", "base-platform")
        go_version = lock.local_go_version(info["GOVERSION"])
        screen(plan, go_version)

        expected = _stage(container, files, work)
        env = authoring_env(request, plan)
        cross_check(container, plan, env)
        selection_env = _json_exec(
            container,
            ["go", "env", "-json", "GOWORK", "GOMOD", "GOFLAGS"],
            env,
            plan["workdir"],
            "go env",
        )
        want_work = env["GOWORK"]
        got_work = selection_env.get("GOWORK")
        if (
            got_work not in ("off", "")
            if plan["mode"] == "none"
            else got_work != want_work
        ):
            raise ResolverError(
                f"Go selected workspace {public_path(str(got_work))!r}, "
                f"expected {public_path(want_work)!r}",
                "workspace-selection-mismatch",
            )
        view = _DownloadView(container, plan)
        lock.PROGRESS(f"{request['id']}: go mod download in {plan['run_dir']}")
        lock.resolve_module_dir(
            view,
            request,
            {"dir": plan["run_dir"], "go_sum": b""},
            0,
            work,
            budget,
            fetch,
            state,
            directory=plan["workdir"],
            environment=env,
            sums=plan["sums"],
        )
        _check_stage(container, files, expected, "after Go ran")
        if request["graph_mod_files"]:
            lock.resolve_graph_mods(request, work, budget, fetch, state)
        state["bytes"] = budget.total
        go_version_text = container.exec(
            ["go", "version"], env=lock.go_env(request), workdir="/"
        ).stdout

    evidence = {
        "go_version": lock.redact(go_version_text.strip()),
        "go_env": {
            k: public_path(v)
            for k, v in env.items()
            if k.startswith("GO") and k != "GOPATH"
        },
        "go_env_reported": {
            "GOWORK": public_path(str(selection_env.get("GOWORK"))),
            "GOMOD": public_path(str(selection_env.get("GOMOD"))),
            "GOFLAGS": selection_env.get("GOFLAGS"),
        },
        "base_platform": "linux/amd64",
        "resolver_version": RESOLVER_VERSION,
    }
    lock_request = dict(request, kind=KIND, resolver=RESOLVER_VERSION)
    result = lock.build_result(lock_request, state, {}, evidence)
    result["kind"] = KIND
    result["authoring_only"] = True
    result["application_compilation_proven"] = False
    result["offline"] = offline_guidance(request, plan, files)
    result["package_request"] = {
        k: request[k]
        for k in ("base", "platform", "goproxy", "gosumdb", "scope", "limits")
    } | {"original": request["original"], "workspace_mode": plan["mode"]}
    result["evidence"] |= {
        "manifests": receipt_files,
        "selection": {
            "mode": plan["mode"],
            "run_dir": plan["run_dir"],
            "go_work": plan["work_path"],
            "use": sorted(plan["use"]),
            "main_modules": (
                {d: plan["module_paths"][d] for d in sorted(plan["use"])}
                if plan["mode"] == "go.work"
                else {plan["run_dir"]: plan["module_paths"][plan["run_dir"]]}
            ),
            "modules": dict(sorted(plan["module_paths"].items())),
            "local_replacements": sorted(
                (
                    {
                        "source": p["source"],
                        "module": p["old"],
                        "target_dir": p["target_dir"],
                    }
                    for p in plan["local"]
                ),
                key=lambda p: (p["source"], p["module"], p["target_dir"]),
            ),
            "remote_replacements": [
                {"source": r["source"], "old": r["old"], "new": " ".join(r["new"])}
                for r in plan["remote"]
            ],
            "sum_authorities": sorted(plan["sum_authorities"]),
            "local_modules_reported_by_go": sorted(
                view.local_modules, key=lambda m: (m["dir"], m["module"])
            ),
        },
        "unchanged_after_go": True,
    }
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def request_signature(request: object) -> str:
    payload = {"request": request, "resolver": RESOLVER_VERSION, "kind": KIND}
    return lock.sha256_hex(json.dumps(payload, sort_keys=True).encode())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("requests", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--retry-failures", action="store_true")
    args = parser.parse_args(argv)
    try:
        lock.require_remote_author()
    except ResolverError as exc:
        parser.error(str(exc))
    requests = json.loads(args.requests.read_text())["requests"]
    results = (
        json.loads(args.output.read_text()) if args.output.exists() else {"results": {}}
    )
    for request in requests:
        key = request.get("id") if isinstance(request, dict) else None
        if not isinstance(key, str):
            parser.error("every request needs a string id")
        signature = request_signature(request)
        previous = results["results"].get(key, {})
        if previous.get("request_sha256") == signature and (
            previous.get("status") == "completed" or not args.retry_failures
        ):
            continue
        try:
            result = resolve(request)
            result["status"] = "completed"
        except Exception as exc:
            result = {
                "id": key,
                "kind": KIND,
                "status": "failed",
                "reason_code": getattr(exc, "code", "unexpected"),
                "error": lock.redact(str(exc)),
            }
        result["request_sha256"] = signature
        results["results"][key] = result
        lock.atomic_json(args.output, results)
        print(json.dumps({"id": key, "status": result["status"]}), flush=True)
    return int(any(r["status"] != "completed" for r in results["results"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
