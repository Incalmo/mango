"""Record the exact .deb files an apt install needs, for an offline native build.

Authoring-time only: this runs apt inside the pinned base image with network
access, using the author's explicit sources (normally a dated archive snapshot)
and package list. apt selects the packages; this tool downloads each selected
file once, checks it against apt's own index hash, and records its SHA-256 in
``inputs.lock.json``. Builds read only that lock.

    nix2rle-apt-lock --lock inputs.lock.json --prefix debs/target \\
        --base docker.io/library/ubuntu@sha256:<digest> \\
        --source 'deb https://snapshot.ubuntu.com/ubuntu/20261001T000000Z jammy main universe' \\
        curl openssh-server=1:8.9p1-3ubuntu0.10

Install offline in ``mkLinuxImage`` with the harness's stage-debs script:

    sh /.nix-inputs/files/stage-debs.sh /var/cache/apt/archives /.nix-inputs/files/debs/target/*.deb
    apt-get install -y --no-download --no-install-recommends /var/cache/apt/archives/*.deb

Only packages missing from the base are selected, matching what the offline
install will add. Changing the base or sources requires re-locking.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import urllib.request
from pathlib import Path

URI = re.compile(r"^'(?P<url>[^']+)' (?P<name>\S+) (?P<size>\d+) (?P<kind>[A-Za-z0-9]+):(?P<hash>[0-9a-f]+)$")
ALGORITHMS = {"SHA512": "sha512", "SHA256": "sha256", "SHA1": "sha1", "MD5Sum": "md5"}
CA_BUNDLE = "/etc/incalmo-lock-ca.crt"


def apt_uris(base: str, sources: list[str], packages: list[str]) -> list[dict]:
    """apt's selection for ``packages`` on ``base`` with only ``sources`` enabled."""
    if not packages:
        return []
    script = "\n".join(
        [
            "set -eu",
            "rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*",
            "printf '%s\\n' " + " ".join(shlex.quote(s) for s in sources)
            + " > /etc/apt/sources.list.d/lock.list",
            # Dated snapshots publish an expired Valid-Until; the date is the pin.
            "apt-get -qq -o Acquire::Check-Valid-Until=false"
            f" -o Acquire::https::CaInfo={CA_BUNDLE} update >&2",
            "apt-get -qq --print-uris --no-install-recommends -y install "
            + " ".join(shlex.quote(p) for p in packages),
        ]
    )
    ca = os.environ.get("SSL_CERT_FILE") or "/etc/ssl/certs/ca-certificates.crt"
    output = subprocess.run(
        [
            "docker", "run", "--rm", "--platform", "linux/amd64",
            "-v", f"{Path(ca).resolve()}:{CA_BUNDLE}:ro",
            "--entrypoint", "/bin/sh", base, "-c", script,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if output.returncode:
        raise RuntimeError("apt resolution failed:\n" + output.stderr)
    records = []
    for line in output.stdout.splitlines():
        match = URI.match(line.strip())
        if match:
            records.append(match.groupdict())
    if not records:
        raise RuntimeError(
            "apt selected no files: the packages may already be in the base image"
        )
    return records


def download_hash(record: dict, cache: Path) -> str:
    """SHA-256 of the file, after checking apt's own index hash."""
    algorithm = ALGORITHMS.get(record["kind"])
    if algorithm is None:
        raise ValueError(f"Unsupported apt hash type: {record['kind']}")
    target = cache / record["name"]
    if not target.exists():
        with urllib.request.urlopen(record["url"]) as response, tempfile.NamedTemporaryFile(
            dir=cache, delete=False
        ) as stream:
            for chunk in iter(lambda: response.read(1 << 20), b""):
                stream.write(chunk)
        os.replace(stream.name, target)
    data = target.read_bytes()
    if len(data) != int(record["size"]) or hashlib.new(algorithm, data).hexdigest() != record["hash"]:
        target.unlink()
        raise ValueError(f"Downloaded bytes do not match apt's index: {record['url']}")
    return hashlib.sha256(data).hexdigest()


def update_lock(lock: Path, prefix: str, artifacts: list[dict]) -> dict:
    """Replace every entry under ``prefix/`` with ``artifacts``; keep the rest."""
    document = (
        json.loads(lock.read_text())
        if lock.exists()
        else {"schema_version": 1, "artifacts": []}
    )
    if document.get("schema_version") != 1:
        raise ValueError("Unsupported inputs lock schema")
    kept = [a for a in document["artifacts"] if not a["path"].startswith(prefix + "/")]
    document["artifacts"] = sorted(kept + artifacts, key=lambda a: a["path"])
    lock.write_text(json.dumps(document, indent=2) + "\n")
    return document


def lock_packages(*, base, sources, packages, prefix, lock, cache=None):
    prefix = prefix.strip("/")
    if not prefix or ".." in prefix.split("/"):
        raise ValueError("Prefix must be a relative directory")
    records = apt_uris(base, sources, packages)
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(cache or temporary)
        directory.mkdir(parents=True, exist_ok=True)
        artifacts = [
            {
                "path": f"{prefix}/{record['name']}",
                "urls": [record["url"]],
                "sha256": download_hash(record, directory),
            }
            for record in records
        ]
    update_lock(lock, prefix, artifacts)
    return artifacts


def main():
    parser = argparse.ArgumentParser(prog="nix2rle-apt-lock", description=__doc__.split("\n")[0])
    parser.add_argument("--lock", type=Path, required=True, help="inputs.lock.json to update")
    parser.add_argument("--prefix", required=True, help="directory for these files in the lock")
    parser.add_argument("--base", required=True, help="pinned base image@sha256:...")
    parser.add_argument("--source", action="append", required=True, help="apt source line")
    parser.add_argument("--cache", type=Path, help="reuse downloaded files across runs")
    parser.add_argument("packages", nargs="+")
    args = parser.parse_args()
    if "@sha256:" not in args.base:
        parser.error("--base must be pinned by digest")
    artifacts = lock_packages(
        base=args.base,
        sources=args.source,
        packages=args.packages,
        prefix=args.prefix,
        lock=args.lock,
        cache=args.cache,
    )
    print(json.dumps({"locked": len(artifacts), "prefix": args.prefix}))


if __name__ == "__main__":
    main()
