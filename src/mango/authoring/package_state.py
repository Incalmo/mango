"""Public authoring APIs for exact installed APT prefixes and retained indexes.

Prepared Docker images are disposable authoring caches, never environment inputs.
Portable builds consume the immutable base, ordered package lock and index files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from mango import HARNESS
from mango.authoring import apt_indexes, apt_prefix_cache, packages
from mango.authoring.package_inputs import mount_args

LEASE_LABEL = "org.mango.package-state"


def _check_space(directory: Path) -> None:
    reserve = int(os.environ.get("MANGO_AUTHOR_MIN_FREE_BYTES", "0"))
    if reserve < 0:
        raise ValueError("Authoring disk reserve cannot be negative")
    if reserve:
        directory.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(directory).free < reserve:
            raise packages.LockError("Authoring disk reserve reached before package-state work")


def resolve_packages(request: packages.LockRequest, lock: Path) -> dict:
    """Resolve a typed repository/transaction timeline and return its portable receipt."""
    prefixes = packages.lock_request(request, lock)
    document = json.loads(lock.read_text())
    artifacts = document["artifacts"]
    return {"package_request": request.model_dump(mode="json"), "prefixes": prefixes,
            "canonical_request_sha256": request_sha256(request),
            "lock": {**document, "artifacts": [a for a in artifacts if any(
                a["path"].startswith(prefix + "/") for prefix in prefixes)]}}


def request_sha256(request: packages.LockRequest) -> str:
    """Canonical full SHA, independent of local input transport paths."""
    return hashlib.sha256(json.dumps(request.cache_document(), sort_keys=True).encode()).hexdigest()


def locked_transactions(receipt: dict) -> tuple[packages.LockRequest, list[list[dict]]]:
    """Validate a package receipt and recover each exact archive selection."""
    request = packages.LockRequest.model_validate(receipt["package_request"])
    if request.manager != "apt":
        raise ValueError("Installed prefix and index state currently require APT")
    if receipt["lock"].get("schema_version", 1) != 1:
        raise ValueError("Unsupported package input lock schema")
    prefixes = receipt["prefixes"]
    if (len(prefixes) != len(request.stages) or len(set(prefixes)) != len(prefixes)
            or any(not isinstance(p, str) or not p or PurePosixPath(p).is_absolute()
                   or ".." in PurePosixPath(p).parts for p in prefixes)
            or any(a.startswith(b + "/") for a in prefixes for b in prefixes if a != b)):
        raise ValueError("Package prefixes must uniquely cover the ordered transactions")
    expected = [[] for _ in prefixes]
    seen = set()
    for artifact in receipt["lock"]["artifacts"]:
        path = artifact["path"]
        matches = [i for i, prefix in enumerate(prefixes) if path.startswith(prefix + "/")]
        if not matches:
            continue
        index = matches[0]
        name = path[len(prefixes[index]) + 1:]
        if (not name or "/" in name or name in {".", ".."} or path in seen
                or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])):
            raise ValueError("Invalid or duplicate locked package artifact")
        seen.add(path)
        expected[index].append({"name": name, "sha256": artifact["sha256"]})
    return request, expected


def _verify_selection(output: str, expected: list[list[dict]]) -> None:
    lines = [line.strip() for line in output.splitlines()]
    if [x for x in lines if x.startswith(packages.STAGE_MARKER)] != [
            f"{packages.STAGE_MARKER}{i}" for i in range(len(expected))]:
        raise packages.LockError("Prepared prefix has incomplete package transactions")
    if any(line.startswith("'") and not packages.APT_URI.fullmatch(line) for line in lines):
        raise packages.LockError("Prepared prefix has an unsupported package hash")
    records = packages._split_stages(output, len(expected), packages.APT_URI)
    for selected, locked in zip(records, expected, strict=True):
        actual = [{"name": item["name"], "sha256": packages.download_hash(
            item, packages.cache_dir() / "downloads")} for item in selected]
        if sorted(actual, key=lambda x: x["name"]) != sorted(locked, key=lambda x: x["name"]):
            raise packages.LockError("Prepared prefix selected packages different from its lock")


def prepare_prefix(receipt: dict, *, count: int | None = None, retain: bool = True) -> dict:
    """Install an exact locked prefix; optionally lease its immutable local image.

    Repository changes, signing keys, preferences and transaction policy remain
    part of the canonical request. Every selected archive is checked against the
    supplied lock before this API returns a usable toolchain binding.
    """
    request, expected = locked_transactions(receipt)
    count = len(request.stages) if count is None else count
    if type(count) is not int or not 1 <= count <= len(request.stages):
        raise ValueError("Prefix count must name one or more complete transactions")
    _check_space(packages.cache_dir())
    prefix = request.model_copy(update={"stages": request.stages[:count]})
    output, image = apt_prefix_cache._resolve_output(prefix, install_final=True)
    _verify_selection(output, expected[:count])
    info = json.loads(apt_prefix_cache.docker("image", "inspect", image).stdout)[0]
    if info["Id"] != image or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise packages.LockError("Prepared image identity changed")
    sha = request_sha256(prefix)
    prefixes = receipt["prefixes"][:count]
    selected = [a for a in receipt["lock"]["artifacts"]
                if any(a["path"].startswith(p + "/") for p in prefixes)]
    result = {"format": 1, "prepared_image": {"id": image, "base": prefix.base,
              "apt_request_sha256": sha}, "apt_receipt": {
                  "package_request": prefix.model_dump(mode="json"), "prefixes": prefixes,
                  "lock": {**receipt["lock"], "artifacts": selected}, "canonical_request_sha256": sha},
              "transaction_count": count, "selection_matches_lock": True,
              "provenance": "authoring-only installed package prefix; not a final environment image"}
    if retain:
        name = "mango-prepared-" + uuid.uuid4().hex
        apt_prefix_cache.docker("create", "--name", name, "--label", LEASE_LABEL + "=" + sha,
                                "--entrypoint", "/bin/true", image)
        result["lease"] = {"container": name, "request_sha256": sha}
    return result


def release_prefix(receipt: dict) -> None:
    """Release only this API's stopped cache lease; never delete an image."""
    lease = receipt["lease"]
    name = lease["container"]
    if not re.fullmatch(r"mango-prepared-[0-9a-f]{32}", name):
        raise ValueError("Invalid prepared-prefix lease")
    inspected = json.loads(apt_prefix_cache.docker("inspect", name).stdout)[0]
    if (inspected["Image"] != receipt["prepared_image"]["id"]
            or inspected.get("State", {}).get("Running", True)
            or inspected.get("Config", {}).get("Labels", {}).get(LEASE_LABEL) != lease["request_sha256"]
            or lease["request_sha256"] != receipt["prepared_image"]["apt_request_sha256"]):
        raise ValueError("Prepared-prefix lease ownership or state changed")
    apt_prefix_cache.docker("rm", name)


def observe_prefix_files(receipt: dict, paths: list[str]) -> dict:
    """Read regular-file identity at an exact prepared prefix without networking.

    This can prove that an original setup write is already satisfied, without
    dropping it from the environment recipe or inferring filesystem state from
    package names. Missing files, symlinks and other file types remain explicit.
    """
    request, _ = locked_transactions(receipt["apt_receipt"])
    prepared = receipt["prepared_image"]
    if (receipt.get("selection_matches_lock") is not True
            or prepared["base"] != request.base
            or prepared["apt_request_sha256"] != request_sha256(request)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", prepared["id"])):
        raise ValueError("File observation requires an exact verified prefix binding")
    if not paths or len(set(paths)) != len(paths):
        raise ValueError("File observation requires unique paths")
    for path in paths:
        target = PurePosixPath(path)
        if (not target.is_absolute() or str(target) != path or ".." in target.parts
                or any(ord(c) < 32 for c in path)):
            raise ValueError("Observed paths must be absolute normalized filenames")
    _check_space(packages.cache_dir())
    info = json.loads(apt_prefix_cache.docker("image", "inspect", prepared["id"]).stdout)[0]
    if info["Id"] != prepared["id"]:
        raise packages.LockError("Observed image identity changed")
    lines = ["set -eu"]
    for index, path in enumerate(paths):
        target = shlex.quote(path)
        lines += [f"if [ -L {target} ]; then printf '{index}\\tsymlink\\n';",
                  f"elif [ -f {target} ]; then",
                  f"  metadata=$(stat -c '%a %u %g %s' {target})",
                  f"  digest=$(sha256sum < {target})",
                  f"  printf '{index}\\tregular\\t%s\\t%s\\n' \"$metadata\" \"${{digest%% *}}\"",
                  f"elif [ -e {target} ]; then printf '{index}\\tother\\n';",
                  f"else printf '{index}\\tmissing\\n'; fi"]
    output = apt_prefix_cache.docker("run", "--rm", "--pull", "never", "--network", "none",
                                     "--entrypoint", "/bin/sh", prepared["id"],
                                     "-c", "\n".join(lines)).stdout.splitlines()
    if len(output) != len(paths):
        raise packages.LockError("Incomplete prepared-file observation")
    files = {}
    for index, (path, line) in enumerate(zip(paths, output, strict=True)):
        fields = line.split("\t")
        if len(fields) < 2 or fields[0] != str(index):
            raise packages.LockError("Unexpected prepared-file observation")
        kind = fields[1]
        if kind == "regular" and len(fields) == 4:
            metadata = fields[2].split()
            if len(metadata) != 4 or not re.fullmatch(r"[0-9a-f]{64}", fields[3]):
                raise packages.LockError("Invalid regular-file observation")
            mode, uid, gid, size = metadata
            files[path] = {"kind": kind, "mode": int(mode, 8), "uid": int(uid),
                           "gid": int(gid), "size": int(size), "sha256": fields[3]}
        elif kind in {"missing", "symlink", "other"} and len(fields) == 2:
            files[path] = {"kind": kind}
        else:
            raise packages.LockError("Unsupported prepared-file observation")
    return {"format": 1, "prepared_image": dict(prepared), "network": "none", "files": files}


def capture_indexes(receipt: dict, transaction: int, uri_mappings: list[dict],
                    runtime_sources: dict[str, str], directory: Path) -> dict:
    """Capture locked index bytes and prove replay with original runtime sources.

    Replay is a real Docker run with networking disabled. The output preserves
    file modes and owners and binds the exact package request, update position,
    runtime source bytes, URI mapping and structured APT repository policy.
    """
    request, expected = locked_transactions(receipt)
    if type(transaction) is not int or not 0 <= transaction < len(request.stages):
        raise ValueError("Invalid index capture transaction")
    if not runtime_sources:
        raise ValueError("Original runtime repository files are required")
    for path, content in runtime_sources.items():
        target = PurePosixPath(path)
        if (".." in target.parts or not isinstance(content, str)
                or not (path == "/etc/apt/sources.list" or
                        target.parent == PurePosixPath("/etc/apt/sources.list.d")
                        and target.suffix in {".list", ".sources"})):
            raise ValueError("Invalid runtime APT source configuration")
    apt_indexes.prefix_mappings(uri_mappings)
    directory = directory.resolve()
    _check_space(directory)
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / ("capture-" + uuid.uuid4().hex + ".tar.gz")
    prepared = None
    try:
        state = apt_indexes.capture(request, transaction, uri_mappings,
                                    expected[:transaction + 1], artifact)
        image = request.base
        if transaction:
            prepared = prepare_prefix(receipt, count=transaction)
            image = prepared["prepared_image"]["id"]
        stage = request.stages[transaction]
        lines = ["set -eu"]
        if stage.policy_rc_d == "deny":
            lines += ["printf '%s' " + shlex.quote("#!/bin/sh\nexit 101\n")
                      + " > /usr/sbin/policy-rc.d", "chmod +x /usr/sbin/policy-rc.d"]
        for key in stage.repository_keys:
            target = shlex.quote(key.path)
            lines += ["mkdir -p " + shlex.quote(str(Path(key.path).parent)), "rm -f " + target]
            source = "/mango/author-keys/" + key.sha256
            lines += [("gpg --batch --yes --dearmor --output " + target + " " + source)
                      if key.dearmor else ("cp " + source + " " + target),
                      f"chmod {key.mode:04o} " + target]
        for preference in stage.apt_preferences:
            lines += ["mkdir -p " + shlex.quote(str(Path(preference.path).parent)),
                      "printf %s " + shlex.quote(preference.content) + " > " + shlex.quote(preference.path)]
        lines += ["rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*"]
        for path, content in runtime_sources.items():
            lines += ["mkdir -p " + shlex.quote(str(Path(path).parent)),
                      "printf %s " + shlex.quote(content) + " > " + shlex.quote(path)]
        lines += ["sh /mango/index-helpers/restore-apt-indexes.sh /mango/indexes.tar.gz " + state["sha256"],
                  "apt-cache policy"]
        replay = subprocess.run(["docker", "run", "--rm", "--network", "none",
                                 "-v", str(artifact.resolve()) + ":/mango/indexes.tar.gz:ro",
                                 "-v", str(HARNESS / "builders") + ":/mango/index-helpers:ro",
                                 *mount_args(request), "--entrypoint", "/bin/sh", image,
                                 "-c", "\n".join(lines)], capture_output=True, text=True, check=True)
        state["runtime_policy"] = replay.stdout
        state["runtime_policy_equal"] = (
            apt_indexes.normalized_policy(state["author_policy"], uri_mappings, author=True)
            == apt_indexes.normalized_policy(replay.stdout, uri_mappings, author=False))
        if not state["runtime_policy_equal"]:
            raise packages.LockError("Disconnected index replay changed APT repository policy")
        destination = directory / (state["sha256"] + ".tar.gz")
        artifact.replace(destination)
        state.update({"path": "apt/indexes/" + destination.name,
                      "author_source": str(destination), "runtime_sources": runtime_sources,
                      "author_request": request.model_dump(mode="json")})
        return state
    finally:
        artifact.unlink(missing_ok=True)
        if prepared is not None:
            release_prefix(prepared)


def write_receipt(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_main(argv=None):
    parser = argparse.ArgumentParser(prog="mango-prepare-prefix", description=prepare_prefix.__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--count", type=int)
    parser.add_argument("--no-retain", action="store_true")
    parser.add_argument("--release", action="store_true", help="release the supplied prepared receipt's cache lease")
    parser.add_argument("--observe", action="append", help="observe a file in the supplied prepared receipt; repeat for multiple paths")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    value = json.loads(args.receipt.read_text())
    if args.observe:
        if args.release or args.count is not None or args.no_retain or args.output is None:
            parser.error("--observe requires --output and cannot prepare or release a prefix")
        write_receipt(args.output, observe_prefix_files(value, args.observe))
        return
    if args.release:
        release_prefix(value)
        return
    if args.output is None:
        parser.error("--output is required when preparing a prefix")
    prepared = prepare_prefix(value, count=args.count, retain=not args.no_retain)
    try:
        write_receipt(args.output, prepared)
    except BaseException:
        if "lease" in prepared:
            release_prefix(prepared)
        raise


def indexes_main(argv=None):
    parser = argparse.ArgumentParser(prog="mango-lock-indexes", description=capture_indexes.__doc__)
    parser.add_argument("request", type=Path, help="package_receipt, transaction, uri_mappings, runtime_sources JSON")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    value = json.loads(args.request.read_text())
    if set(value) != {"package_receipt", "transaction", "uri_mappings", "runtime_sources"}:
        parser.error("index request has missing or unsupported fields")
    write_receipt(args.output, capture_indexes(value["package_receipt"], value["transaction"],
                  value["uri_mappings"], value["runtime_sources"], args.directory))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "indexes"])
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in {"-h", "--help"}:
        parser.print_help()
        return
    args = parser.parse_args(argv[:1])
    {"prepare": prepare_main, "indexes": indexes_main}[args.command](argv[1:])


if __name__ == "__main__":
    main()
