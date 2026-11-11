"""Pin a registry image for ``fetchLinuxBase`` / ``mkContainer { base = ...; }``.

Authoring-time only: this contacts the registry. It resolves a tag to the
linux/amd64 manifest digest, then fetches that digest with the harness's own
``fetchLinuxBase`` to learn the Nix content hash. Commit the printed
``{ image; digest; hash; }``; builds never resolve tags.

    nix2rle-pin-base docker.io/library/ubuntu:22.04
    nix2rle-pin-base docker.io/library/ubuntu@sha256:<digest> --nix
"""

import argparse
import json
import re
import subprocess

from nix2rle import HARNESS

GOT = re.compile(r"got:\s+(sha256-[A-Za-z0-9+/=]+)")


def split_reference(reference: str) -> tuple[str, str | None]:
    if "@" in reference:
        image, digest = reference.split("@", 1)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError(f"Invalid digest: {digest}")
        return image, digest
    return reference, None


def resolve_digest(reference: str) -> str:
    """The linux/amd64 manifest digest a tag currently names."""
    output = subprocess.run(
        [
            "skopeo",
            "inspect",
            "--override-os",
            "linux",
            "--override-arch",
            "amd64",
            "--format",
            "{{.Digest}}",
            f"docker://{reference}",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", output):
        raise ValueError(f"Unexpected digest from registry: {output!r}")
    return output


def content_hash(image: str, digest: str) -> str:
    """Fetch with a fake hash and read the real one from Nix's mismatch report."""
    expression = (
        f"let h = import {json.dumps(str(HARNESS))} {{}}; in h.fetchLinuxBase {{ "
        f"image = {json.dumps(image)}; digest = {json.dumps(digest)}; "
        f"hash = h.pkgs.lib.fakeHash; "
        # A unique name keeps parallel probes from sharing one store lock.
        f'name = "probe-{digest[7:19]}"; }}'
    )
    result = subprocess.run(
        ["nix-build", "--no-out-link", "--expr", expression],
        capture_output=True,
        text=True,
        check=False,
    )
    match = GOT.search(result.stderr)
    if not match:
        raise RuntimeError("Cannot determine base content hash:\n" + result.stderr)
    return match.group(1)


def pin(reference: str) -> dict:
    image, digest = split_reference(reference)
    if image.count("/") == 0:
        image = "docker.io/library/" + image
    if digest is None:
        digest = resolve_digest(reference)
    tagless = re.sub(r":[^/:]+$", "", image)
    return {"image": tagless, "digest": digest, "hash": content_hash(tagless, digest)}


def nix(value: dict) -> str:
    return "{ " + " ".join(f"{k} = {json.dumps(v)};" for k, v in value.items()) + " }"


def main():
    parser = argparse.ArgumentParser(prog="nix2rle-pin-base", description=__doc__.split("\n")[0])
    parser.add_argument("reference", nargs="+", help="image:tag or image@sha256:...")
    parser.add_argument("--nix", action="store_true", help="print Nix attribute sets")
    args = parser.parse_args()
    for reference in args.reference:
        value = pin(reference)
        print(nix(value) if args.nix else json.dumps(value))


if __name__ == "__main__":
    main()
