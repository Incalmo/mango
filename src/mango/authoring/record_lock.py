#!/usr/bin/env python3
"""Lock whatever a command downloads, for any tool that speaks HTTP(S).

``mango-lock-record`` runs a command once inside a digest-pinned base image
with ``mango-proxy`` recording every HTTP(S) exchange, then runs it again with
no network, answered only from the recording. A command that passes both runs
is locked completely: its recording is written to ``inputs.lock.json`` and the
offline build replays it with the same proxy::

    mango-lock-record --lock inputs.lock.json --prefix record/app \\
        --base maven@sha256:... --context ./app --workdir /app -- mvn -B package

Nothing here knows a package manager. Each recorded response becomes one lock
entry, named by content hash:

* a response its server declares long-lived is pinned by URL and SHA-256;
* any other response cannot be expected to stay available at its URL, so its
  bytes are committed beside the lock (``<lock stem>.d/<sha256>``);
* an unstable response above ``--commit-limit`` is still pinned by URL and
  reported, because it is too large to commit. Keep it in the binary cache.

Run it where running the project's build with network access is acceptable.
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
from pathlib import Path

from mango.authoring.packages import update_lock

ROOT = Path(__file__).resolve().parents[3]
BASE = re.compile(r"[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}")
PREFIX = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*")
SHA256 = re.compile(r"[0-9a-f]{64}")
# The same test the Nix fetcher applies: such URLs carry a credential.
SIGNED = re.compile(r".*[?&](token|access_token|password|signature|X-Amz-Signature|X-Goog-Signature)=.*")
COMMIT_LIMIT = 1 << 20


class RecordError(ValueError):
    pass


def addressable(key: str) -> str | None:
    """The URL a plain GET can fetch this exchange from, if there is one."""
    method, _, rest = key.partition(" ")
    if method != "GET" or " " in rest:  # another method, or a request body
        return None
    if not re.fullmatch(r"https?://[^/@\s]+(/\S*)?", rest) or SIGNED.fullmatch(rest):
        return None
    return rest


def lock_entries(index: dict, prefix: str, sidecar: str, commit_limit: int = COMMIT_LIMIT):
    """Turn a recording's index into lock artifacts.

    Returns ``(artifacts, committed, report)``: lock entries, the blob hashes
    whose bytes must be committed under ``sidecar``, and a summary.
    """
    if index.get("version") != 1 or not isinstance(index.get("entries"), dict):
        raise RecordError("unsupported recording index")
    urls: dict[str, set[str]] = {}
    commit: set[str] = set()
    oversized: list[str] = []
    for key, entry in sorted(index["entries"].items()):
        blob = entry.get("sha256")
        if not blob:
            continue
        if not SHA256.fullmatch(blob):
            raise RecordError(f"invalid blob name in recording: {blob!r}")
        url = addressable(key)
        if url and entry.get("stable"):
            urls.setdefault(blob, set()).add(url)
        elif url and entry.get("size", 0) > commit_limit:
            urls.setdefault(blob, set()).add(url)
            oversized.append(key)
        else:
            commit.add(blob)
    # Identical bytes served by a stable URL need no committed copy.
    commit -= set(urls)
    artifacts = [
        {"path": f"{prefix}/blobs/{blob}", "urls": sorted(found), "sha256": blob}
        for blob, found in sorted(urls.items())
    ] + [
        {"path": f"{prefix}/blobs/{blob}", "file": f"{sidecar}/{blob}", "sha256": blob}
        for blob in sorted(commit)
    ]
    report = {
        "exchanges": len(index["entries"]),
        "pinned_by_url": len(urls),
        "committed": len(commit),
        "unstable_but_too_large_to_commit": oversized,
    }
    return artifacts, commit, report


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_lock(lock: Path, prefix: str, recording: Path, commit_limit: int = COMMIT_LIMIT) -> dict:
    """Write one recording into ``lock`` and commit what no URL can serve."""
    index_bytes = (recording / "index.json").read_bytes()
    sidecar = lock.name.removesuffix(".json") + ".d"
    artifacts, commit, report = lock_entries(json.loads(index_bytes), prefix, sidecar, commit_limit)
    directory = lock.parent / sidecar
    directory.mkdir(parents=True, exist_ok=True)
    for blob in commit:
        source = recording / "blobs" / blob
        if sha256_file(source) != blob:
            raise RecordError(f"recorded body {blob} does not match its name")
        shutil.copyfile(source, directory / blob)
    index_hash = hashlib.sha256(index_bytes).hexdigest()
    (directory / index_hash).write_bytes(index_bytes)
    artifacts.append({"path": f"{prefix}/index.json", "file": f"{sidecar}/{index_hash}", "sha256": index_hash})
    document = update_lock(lock, artifacts, [prefix])
    # Drop committed files that no entry of any recording refers to any more.
    used = {a["file"].split("/")[-1] for a in document["artifacts"] if "file" in a}
    for stale in directory.iterdir():
        if stale.name not in used and SHA256.fullmatch(stale.name):
            stale.unlink()
    return report


def nix_build(attribute: str) -> Path:
    result = subprocess.run(
        ["nix-build", str(ROOT / "default.nix"), "-A", attribute, "--no-out-link"],
        check=True, stdout=subprocess.PIPE, text=True,
    )
    return Path(result.stdout.strip().splitlines()[-1])


def container(args, proxy: Path, recording: Path, mode: str, extra: list[str]) -> list[str]:
    """One ``docker run`` of the command under the proxy."""
    command = [
        "docker", "run", "--rm", "--platform", "linux/amd64",
        # A private mount namespace lets the proxy capture clients that ignore
        # proxy settings; it changes nothing outside this container.
        "--cap-add", "SYS_ADMIN", "--security-opt", "apparmor=unconfined",
        "--mount", f"type=bind,source={proxy},target=/mango/proxy,readonly",
        *extra,
    ]
    for assignment in args.env:
        command += ["--env", assignment]
    script = args.command[0] if len(args.command) == 1 else shlex.join(args.command)
    if args.context:
        command += ["--mount", f"type=bind,source={args.context.resolve()},target=/mango/context,readonly"]
        script = f"mkdir -p {shlex.quote(args.workdir)} && cp -a /mango/context/. {shlex.quote(args.workdir)}/ && cd {shlex.quote(args.workdir)} && {script}"
    elif args.workdir != "/":
        script = f"mkdir -p {shlex.quote(args.workdir)} && cd {shlex.quote(args.workdir)} && {script}"
    proxy_args = ["record", "/mango/recording", "--upstream-ca", "/mango/roots.pem"] if mode == "record" else ["replay", "/mango/recording"]
    return command + [args.base, "/mango/proxy", *proxy_args, "--", "sh", "-c", script]


def record(args) -> dict:
    proxy = nix_build("replayProxy")
    roots = nix_build("pkgs.cacert") / "etc/ssl/certs/ca-bundle.crt"
    with tempfile.TemporaryDirectory(prefix="mango-record-") as temporary:
        recording = Path(temporary) / "recording"
        recording.mkdir()
        print("mango-lock-record: recording (online)", file=sys.stderr)
        subprocess.run(
            container(args, proxy, recording, "record", [
                "--mount", f"type=bind,source={recording},target=/mango/recording",
                "--mount", f"type=bind,source={roots},target=/mango/roots.pem,readonly",
            ]),
            check=True,
        )
        # The container wrote as root; take ownership through the same engine.
        subprocess.run(
            ["docker", "run", "--rm", "--mount", f"type=bind,source={recording},target=/r",
             "--entrypoint", "/mango/proxy", "--mount", f"type=bind,source={proxy},target=/mango/proxy,readonly",
             args.base, "--chown", f"{os.getuid()}:{os.getgid()}", "/r"],
            check=True,
        )
        if not args.no_verify:
            print("mango-lock-record: verifying (no network)", file=sys.stderr)
            verified = subprocess.run(
                container(args, proxy, recording, "replay", [
                    "--network", "none",
                    "--mount", f"type=bind,source={recording},target=/mango/recording,readonly",
                ]),
            )
            if verified.returncode != 0:
                raise RecordError(
                    "the command failed when replayed without a network; the proxy lists "
                    "each request it could not answer above. Requests that differ between "
                    "runs (random or time-dependent URLs) cannot be replayed"
                )
        report = write_lock(args.lock, args.prefix, recording, args.commit_limit)
    report["prefix"] = args.prefix
    report["verified_offline"] = not args.no_verify
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lock", type=Path, required=True, help="inputs.lock.json to update")
    parser.add_argument("--prefix", required=True, help="lock path prefix of this recording, e.g. record/app")
    parser.add_argument("--base", required=True, help="pinned base image@sha256:...")
    parser.add_argument("--context", type=Path, help="directory copied into --workdir before the command runs")
    parser.add_argument("--workdir", default="/", help="directory the command runs in")
    parser.add_argument("--env", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--commit-limit", type=int, default=COMMIT_LIMIT, metavar="BYTES",
                        help="largest unstable response committed beside the lock (default 1 MiB)")
    parser.add_argument("--no-verify", action="store_true", help="skip the offline replay")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- COMMAND...; one argument is run by sh -c")
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    if not BASE.fullmatch(args.base):
        parser.error("--base must be image@sha256:<digest>")
    if not PREFIX.fullmatch(args.prefix) or ".." in args.prefix.split("/"):
        parser.error("--prefix must be a relative path of plain names")
    if not args.workdir.startswith("/"):
        parser.error("--workdir must be absolute")
    if args.context and not args.context.is_dir():
        parser.error("--context must be a directory")
    try:
        print(json.dumps(record(args), indent=2))
    except (RecordError, subprocess.CalledProcessError) as error:
        print(f"mango-lock-record: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
