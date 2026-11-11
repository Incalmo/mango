"""Package a fixed-output skopeo directory without changing image bytes."""

import argparse
import hashlib
import json
import re
from pathlib import Path

MANIFEST_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}


def package(base, output):
    base = Path(base).resolve(strict=True)
    raw = (base / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    media_type = manifest.get(
        "mediaType", "application/vnd.docker.distribution.manifest.v2+json"
    )
    if manifest.get("schemaVersion") != 2 or media_type not in MANIFEST_TYPES:
        raise ValueError("Expected one selected platform's schema-2 image manifest")
    sources = {}
    for descriptor in [manifest["config"], *manifest["layers"]]:
        digest = descriptor["digest"]
        if not isinstance(digest, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", digest
        ):
            raise ValueError("Image blobs require SHA-256 descriptors")
        source = base / digest.removeprefix("sha256:")
        with source.open("rb") as stream:
            hasher = hashlib.sha256()
            size = 0
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
                size += len(chunk)
        if size != descriptor["size"] or "sha256:" + hasher.hexdigest() != digest:
            raise ValueError(f"Image blob hash or size mismatch: {digest}")
        sources[digest.removeprefix("sha256:")] = source
    manifest_hash = hashlib.sha256(raw).hexdigest()
    sources[manifest_hash] = base / "manifest.json"
    output = Path(output)
    output.mkdir()
    blobs = output / "blobs/sha256"
    blobs.mkdir(parents=True)
    for digest, source in sources.items():
        (blobs / digest).symlink_to(source)
    (output / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}\n')
    (output / "index.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {
                        "mediaType": media_type,
                        "digest": "sha256:" + manifest_hash,
                        "size": len(raw),
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    package(args.base, args.out)


if __name__ == "__main__":
    main()
