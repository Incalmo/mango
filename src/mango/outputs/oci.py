"""Load Nix-built images into Docker, or push them to a registry, by digest.

skopeo reads each image's layers straight from /nix/store into a temporary OCI
layout, which is streamed into `docker load` and then deleted. Docker's
containerd image store keeps OCI manifests unchanged, so the digest skopeo
wrote is the digest Docker reports and Compose can reference.
"""

import argparse
import contextlib
import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

#: Not a resolvable registry: references can only be satisfied locally.
REPOSITORY = "mango.local"


def require_containerd_store():
    status = subprocess.run(
        ["docker", "info", "--format", "{{json .DriverStatus}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if "io.containerd.snapshotter" not in status:
        raise RuntimeError(
            "Docker must use the containerd image store to keep image digests; "
            "see https://docs.docker.com/engine/storage/containerd/"
        )


def describe(image):
    """Layer and config digests from a nix2container image description."""
    document = json.loads(Path(image).read_text())
    return {
        "layer_digests": [layer["digest"] for layer in document["layers"]],
        "config": document["image-config"],
        "architecture": document.get("arch"),
    }


def stream_layout(layout, index, reference):
    """docker load an OCI layout dir, naming it via the index annotation."""
    (descriptor,) = index["manifests"]
    digest = descriptor["digest"]
    # Docker names an OCI import from this annotation; a digest-derived
    # tag keeps every build addressable without moving a shared tag.
    descriptor.setdefault("annotations", {})["io.containerd.image.name"] = (
        f"{reference}:{digest.removeprefix('sha256:')[:16]}"
    )
    data = json.dumps(index).encode()
    loader = subprocess.Popen(["docker", "load", "--quiet"], stdin=subprocess.PIPE)
    # Blobs may be symlinks into other store paths: dereference them.
    with tarfile.open(fileobj=loader.stdin, mode="w|", dereference=True) as stream:
        for path in sorted(layout.rglob("*")):
            if path.name == "index.json" and path.parent == layout:
                continue
            stream.add(path, arcname=str(path.relative_to(layout)), recursive=False)
        info = tarfile.TarInfo("index.json")
        info.size = len(data)
        stream.addfile(info, io.BytesIO(data))
    loader.stdin.close()
    if loader.wait():
        raise RuntimeError(f"docker load failed for {reference}")
    return digest


def restore_runtime_config(layout, configuration):
    """Keep all declared runtime fields across nix2container serialization.

    The transport's OCI struct can omit Docker extensions such as Healthcheck.
    Restore only the trusted Nix image description's runtime config; serialized
    architecture, OS, rootfs/diff IDs and generated history remain untouched.
    """
    if not isinstance(configuration, dict):
        raise ValueError("Nix image-config must be an object")
    layout = Path(layout)
    blobs = layout / "blobs/sha256"
    index = json.loads((layout / "index.json").read_text())
    (descriptor,) = index["manifests"]
    manifest = json.loads(
        (blobs / descriptor["digest"].removeprefix("sha256:")).read_text()
    )
    config_descriptor = manifest["config"]
    config = json.loads(
        (blobs / config_descriptor["digest"].removeprefix("sha256:")).read_text()
    )
    config["config"] = configuration

    def write(document, previous):
        data = json.dumps(document, sort_keys=True).encode()
        digest = hashlib.sha256(data).hexdigest()
        (blobs / digest).write_bytes(data)
        return {**previous, "digest": "sha256:" + digest, "size": len(data)}

    manifest["config"] = write(config, config_descriptor)
    index["manifests"][0] = write(manifest, descriptor)
    (layout / "index.json").write_text(json.dumps(index))
    return index


@contextlib.contextmanager
def oci_layout(name, image, scratch=None):
    """Yield (layout directory, index) for a Nix image output.

    OCI-layout outputs (mkLinuxImage, pinnedImage) are used in place. A
    nix2container description is serialized to a temporary layout with its
    complete declared runtime config restored.
    """
    image = Path(image).resolve()
    if (image / "oci-layout").exists():
        yield image, json.loads((image / "index.json").read_text())
        return
    with tempfile.TemporaryDirectory(prefix=f"oci-{name}-", dir=scratch) as tmp:
        layout = Path(tmp) / "layout"
        subprocess.run(
            [
                "skopeo",
                "--insecure-policy",
                "copy",
                "--quiet",
                f"nix:{image}",
                f"oci:{layout}:{name}",
            ],
            check=True,
        )
        configuration = json.loads(image.read_text())["image-config"]
        yield layout, restore_runtime_config(layout, configuration)


def load_one(name, image, scratch):
    reference = f"{REPOSITORY}/{name}"
    with oci_layout(name, image, scratch) as (layout, index):
        digest = stream_layout(layout, index, reference)
    pinned = f"{reference}@{digest}"
    subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", pinned],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return pinned


def push_one(name, image, repository, scratch=None, authfile=None):
    """Copy one image to ``repository/name`` without a Docker daemon.

    The manifest bytes are pushed unchanged, so the returned digest is the
    digest of the layout's own manifest, checked against the registry's answer.
    """
    reference = f"{repository.rstrip('/')}/{name}"
    with oci_layout(name, image, scratch) as (layout, index):
        (descriptor,) = index["manifests"]
        expected = descriptor["digest"]
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            digestfile = Path(tmp) / "digest"
            subprocess.run(
                [
                    "skopeo",
                    "--insecure-policy",
                    "copy",
                    "--quiet",
                    "--retry-times",
                    "3",
                    "--preserve-digests",
                    *(["--dest-authfile", str(authfile)] if authfile else []),
                    "--digestfile",
                    str(digestfile),
                    f"oci:{layout}",
                    f"docker://{reference}:{expected.removeprefix('sha256:')[:16]}",
                ],
                check=True,
            )
            pushed = digestfile.read_text().strip()
    if pushed != expected:
        raise RuntimeError(f"Registry digest {pushed} differs from {expected}")
    return f"{reference}@{expected}"


def push(images, repository, scratch=None, authfile=None):
    """Push {name: image path}; return {name: repository/name@digest}."""
    if scratch is not None:
        Path(scratch).mkdir(parents=True, exist_ok=True)
    return {
        name: push_one(name, path, repository, scratch, authfile)
        for name, path in images.items()
    }


def load(images, scratch=None):
    """Load {name: image JSON path}; return {name: repository@digest}."""
    require_containerd_store()
    if scratch is not None:
        Path(scratch).mkdir(parents=True, exist_ok=True)
    return {name: load_one(name, path, scratch) for name, path in images.items()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("image", type=Path, help="nix2container JSON or OCI layout")
    args = parser.parse_args()
    print(load({args.name: args.image})[args.name])
