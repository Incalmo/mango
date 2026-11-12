"""Deterministic APT index inputs with explicit repository URI remapping.

This captures index bytes, not an environment image. Mapping a dated authoring
repository to a runtime URI preserves that snapshot's package metadata; it does
not assert equivalence to an earlier mutable repository state.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


def uri_filename(uri: str) -> str:
    """APT URItoFileName encoding for public HTTP(S) repository index URIs.

    Matches apt-pkg/contrib/strutl.cc: strip scheme, percent-quote unsafe
    characters (including literal underscores), then replace slashes.
    """
    value = urlsplit(uri)
    if (value.scheme not in {"http", "https"} or not value.hostname
            or value.username or value.password or value.query or value.fragment
            or ":" in value.hostname):
        raise ValueError("Index URI must be public HTTP(S), without credentials or query")
    raw = value.netloc + value.path
    bad = '\\|{}[]<>"^~_=!@#$%^&*'
    return "".join(f"%{byte:02x}" if byte <= 32 or byte >= 127 or chr(byte) in bad
                   else "_" if byte == 47 else chr(byte) for byte in raw.encode())


def prefix_mappings(mappings: list[dict[str, str]]) -> dict[str, str]:
    """Mappings must name complete suite URI prefixes, ending with '/'."""
    result = {}
    for item in mappings:
        if set(item) != {"author_uri", "runtime_uri"}:
            raise ValueError("Index mapping requires author_uri and runtime_uri")
        if not all(value.endswith("/") for value in item.values()):
            raise ValueError("Index URI prefixes must end with a slash")
        if any("/dists/" not in value for value in item.values()) or (
                item["author_uri"].split("/dists/", 1)[1] != item["runtime_uri"].split("/dists/", 1)[1]):
            raise ValueError("Index mappings must preserve the complete repository suite")
        source, target = (uri_filename(item[key]) for key in ("author_uri", "runtime_uri"))
        if source in result and result[source] != target:
            raise ValueError("Ambiguous index URI mapping")
        result[source] = target
    if not result:
        raise ValueError("Explicit index URI mappings are required")
    return result


def _mapped_name(name: str, mappings: dict[str, str]) -> str:
    # Docker cp emits './...' or a directory root named 'lists'.
    while name.startswith("./"):
        name = name[2:]
    if name in {"", ".", "lists", "lists/"}:
        return "."
    if name.startswith("lists/"):
        name = name[6:]
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Unsafe APT index archive member")
    if len(path.parts) == 1 and name in {"lock", "partial", "auxfiles"}:
        return name
    matches = [prefix for prefix in mappings if name.startswith(prefix)]
    if len(matches) != 1:
        raise ValueError(f"APT index member has no unique declared URI mapping: {name}")
    prefix = matches[0]
    return mappings[prefix] + name[len(prefix):]


class _HashedReader:
    def __init__(self, source):
        self.source = source
        self.digest = hashlib.sha256()

    def read(self, size=-1):
        data = self.source.read(size)
        self.digest.update(data)
        return data


def canonicalize(source: Path, destination: Path, mappings: list[dict[str, str]]) -> dict:
    """Verify/sanitize Docker's lists tar and publish a deterministic tar.gz.

    Reject links, devices, incomplete acquisition files and mapping collisions.
    Preserve regular file bytes, modes and numeric owners; normalize timestamps.
    """
    prefixes = prefix_mappings(mappings)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".apt-index-")
    os.close(fd)
    members = []
    try:
        with tarfile.open(source, "r:*") as original:
            selected = {}
            for item in original.getmembers():
                if not (item.isfile() or item.isdir()):
                    raise ValueError("APT index archive contains a link or special file")
                name = _mapped_name(item.name, prefixes)
                if name in selected:
                    raise ValueError("APT index mapping collides with another member")
                if name.startswith(("partial/", "auxfiles/")):
                    raise ValueError("APT index acquisition directories must be empty")
                if name == "lock" and item.size:
                    raise ValueError("APT lock file must be empty")
                selected[name] = item
            if not any(item.isfile() and name != "lock" for name, item in selected.items()):
                raise ValueError("APT index archive contains no repository indexes")
            with open(temporary, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as target:
                    for name, item in sorted(selected.items()):
                        entry = tarfile.TarInfo(name)
                        entry.type = tarfile.DIRTYPE if item.isdir() else tarfile.REGTYPE
                        entry.mode, entry.uid, entry.gid = item.mode, item.uid, item.gid
                        entry.size = item.size if item.isfile() else 0
                        record = {"path": name, "mode": item.mode, "uid": item.uid, "gid": item.gid,
                                  "size": entry.size, "type": "directory" if item.isdir() else "file"}
                        if item.isfile():
                            with original.extractfile(item) as content:
                                reader = _HashedReader(content)
                                target.addfile(entry, reader)
                                record["sha256"] = reader.digest.hexdigest()
                        else:
                            target.addfile(entry)
                        members.append(record)
        with open(temporary, "rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        os.replace(temporary, destination)
        return {"sha256": digest, "size": destination.stat().st_size,
                "members": members, "uri_mappings": mappings}
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def capture(request, transaction: int, mappings: list[dict[str, str]],
            expected: list[list[dict]], destination: Path) -> dict:
    """Capture an update before a transaction, validating its selected archives.

    ``expected`` contains filename/sha256 pairs for every transaction through the
    capture point. Earlier stages are installed in bounded remote author caches;
    the final stage only updates and selects. Callers must place replay at that
    original update and bind runtime sources to the explicit mappings.
    """
    import json
    import subprocess
    import uuid

    from mango.authoring import packages as p
    from mango.authoring.apt_prefix_cache import _resolve_output, docker
    from mango.authoring.package_inputs import mount_args

    if request.manager != "apt" or not 0 <= transaction < len(request.stages):
        raise ValueError("Invalid APT index capture transaction")
    if len(expected) != transaction + 1:
        raise ValueError("Expected archive transactions must cover the capture prefix")
    stage = request.stages[transaction]
    if stage.operation == "dpkg_then_fix":
        raise ValueError("Capture an explicit update before a conditional dpkg repair")
    prefix_mappings(mappings)
    outputs = []
    image = request.base
    if transaction:
        prior = request.model_copy(update={"stages": request.stages[:transaction]})
        previous, image = _resolve_output(prior, install_final=True)
        outputs.append(previous)
    needs_update = stage.clear_lists_before or not (
        transaction == 0 or stage.sources is not None or stage.extra_sources)
    current = request.model_copy(update={"stages": (*request.stages[:transaction],
                                                     stage.model_copy(update={"update_before": needs_update}))})
    script = p.apt_script(current, start=transaction, initialize=transaction == 0)
    script += "\necho @@mango-index-policy\napt-cache policy"
    ca = os.environ.get("SSL_CERT_FILE") or "/etc/ssl/certs/ca-certificates.crt"
    name = "mango-apt-index-" + uuid.uuid4().hex
    raw = destination.with_suffix("." + uuid.uuid4().hex + ".raw.tar")
    try:
        result = docker("run", "--name", name, "--platform", "linux/amd64",
                        "-v", f"{Path(ca).resolve()}:{p.CA_BUNDLE}:ro", *mount_args(current),
                        "--entrypoint", "/bin/sh", image, "-c", script)
        selection, policy = result.stdout.split("@@mango-index-policy\n", 1)
        outputs.append(p.filter_local_deb_uris(current, selection))
        output = "\n".join(outputs)
        lines = [line.strip() for line in output.splitlines()]
        if [line for line in lines if line.startswith(p.STAGE_MARKER)] != [
                f"{p.STAGE_MARKER}{index}" for index in range(transaction + 1)]:
            raise p.LockError("APT index capture has incomplete transaction markers")
        if any(line.startswith("'") and not p.APT_URI.fullmatch(line) for line in lines):
            raise p.LockError("APT index capture has an unsupported archive hash")
        records = p._split_stages(output, transaction + 1, p.APT_URI)
        for index, selected in enumerate(records):
            actual = [{"name": item["name"], "sha256": p.download_hash(item, p.cache_dir() / "downloads")}
                      for item in selected]
            if sorted(actual, key=lambda x: x["name"]) != sorted(expected[index], key=lambda x: x["name"]):
                raise p.LockError("APT indexes selected archives different from the locked transaction")
        raw.parent.mkdir(parents=True, exist_ok=True)
        with raw.open("wb") as stream:
            subprocess.run(["docker", "cp", name + ":/var/lib/apt/lists/.", "-"],
                           check=True, stdout=stream, stderr=subprocess.PIPE)
        receipt = canonicalize(raw, destination, mappings)
        receipt.update({"transaction": transaction,
                        "capture_request_sha256": hashlib.sha256(json.dumps(
                            request.cache_document(), sort_keys=True).encode()).hexdigest(),
                        "selection_matches_lock": True, "author_policy": policy,
                        "provenance": "authoring repository snapshot; not historical mutable repository equivalence"})
        return receipt
    finally:
        docker("rm", "-f", name, check=False)
        raw.unlink(missing_ok=True)


def normalized_policy(policy: str, mappings: list[dict[str, str]], *, author: bool) -> list[tuple[str, ...]]:
    """Compare policy records without separating priorities from release fields.

    Only declared repository URI/host substitutions are normalized. Unknown
    records and pinned-package text remain in the comparison verbatim.
    """
    import re

    records = []
    current = []
    for raw in policy.splitlines():
        line = raw.strip()
        if re.match(r"-?\d+ ", line) or not raw.startswith(" "):
            if current:
                records.append(current)
            current = [line]
        elif line:
            current.append(line)
    if current:
        records.append(current)
    if author:
        for record in records:
            if not record:
                continue
            for item in mappings:
                source, source_suite = item["author_uri"].rstrip("/").rsplit("/dists/", 1)
                target, target_suite = item["runtime_uri"].rstrip("/").rsplit("/dists/", 1)
                if source_suite != target_suite:
                    raise ValueError("APT index URI mappings must preserve suites")
                needle = " " + source + " " + source_suite + "/"
                if needle not in record[0]:
                    continue
                record[0] = record[0].replace(needle, " " + target + " " + target_suite + "/", 1)
                origin = "origin " + urlsplit(source).hostname
                if origin not in record:
                    raise ValueError("Author policy origin does not match declared repository URI")
                record[record.index(origin)] = "origin " + urlsplit(target).hostname
                break
    return sorted(tuple(record) for record in records)
