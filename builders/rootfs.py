"""Execute an authored shell build in a Nix build VM.

Runs inside `vmTools.runInLinuxVM` (no network device): applies the pinned base
image's layers, executes the authored script in a chroot, and writes the
resulting filesystem difference as one OCI layer. Runtime configuration comes
from the explicit mkLinuxImage config argument.
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from artifacts import export_artifacts

INPUTS = ".nix-inputs"
# Mounted or managed by the container runtime; never part of a layer.
EXCLUDED = {
    "proc",
    "sys",
    "dev",
    INPUTS,
    "etc/resolv.conf",
    "etc/hosts",
    "etc/hostname",
}


class BaseImage:
    """Read one pinned image without converting layers or its configuration."""

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.oci = (self.directory / "oci-layout").is_file()
        if self.oci:
            layout = json.loads((self.directory / "oci-layout").read_text())
            if layout.get("imageLayoutVersion") != "1.0.0":
                raise ValueError("Unsupported OCI base layout version")
            index = json.loads((self.directory / "index.json").read_text())
            descriptors = index.get("manifests", [])
            if len(descriptors) != 1 or descriptors[0].get("mediaType") not in {
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            }:
                raise ValueError(
                    "OCI base must contain exactly one image manifest, not an image index"
                )
            self.manifest = json.loads(self.blob(descriptors[0]).read_text())
        else:
            self.manifest = json.loads((self.directory / "manifest.json").read_text())
        self.config = json.loads(self.blob(self.manifest["config"]).read_text())

    def blob(self, descriptor):
        digest = descriptor["digest"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError(f"Unsupported image blob digest: {digest}")
        prefix = self.directory / "blobs/sha256" if self.oci else self.directory
        path = prefix / digest.removeprefix("sha256:")
        if not path.is_file():
            raise ValueError(f"Missing image blob: {digest}")
        return path


def layer_stream(path):
    with open(path, "rb") as handle:
        compressed = handle.read(2) == b"\x1f\x8b"
    return gzip.open(path) if compressed else open(path, "rb")


def remove(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def apply_layer(root, blob):
    """Extract one image layer over root, honouring OCI whiteouts."""
    with layer_stream(blob) as stream, tarfile.open(fileobj=stream, mode="r|") as tar:
        for member in tar:
            name = layer_path(member.name)
            if name == ".":
                continue
            directory, _, base = name.rpartition("/")
            parent = root / directory
            if base == ".wh..wh..opq":
                for child in parent.iterdir() if parent.is_dir() else []:
                    remove(child)
                continue
            if base.startswith(".wh."):
                remove(parent / base[4:])
                continue
            target = root / name
            if target.is_symlink() or (
                target.exists() and not (member.isdir() and target.is_dir())
            ):
                remove(target)
            member.name = name
            if member.islnk():
                member.linkname = layer_path(member.linkname)
            tar.extract(member, root, numeric_owner=True, filter="fully_trusted")
            for key, value in member.pax_headers.items():
                if key.startswith("SCHILY.xattr.") and not member.issym():
                    os.setxattr(
                        target,
                        key.removeprefix("SCHILY.xattr."),
                        value.encode("utf-8", "surrogateescape"),
                        follow_symlinks=False,
                    )


def layer_path(value):
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Invalid layer path: {value}")
    return str(path)


def snapshot(root):
    """Metadata of every path; a replaced file gets a new inode or mtime."""
    state = {}
    for directory, dirs, files in os.walk(root):
        relative_dir = os.path.relpath(directory, root)
        for name in dirs + files:
            relative = os.path.normpath(os.path.join(relative_dir, name))
            if relative.split("/")[0] in EXCLUDED or relative in EXCLUDED:
                continue
            info = os.lstat(os.path.join(directory, name))
            link = (
                os.readlink(os.path.join(directory, name))
                if stat.S_ISLNK(info.st_mode)
                else None
            )
            state[relative] = (
                info.st_ino,
                info.st_mtime_ns,
                info.st_size,
                info.st_mode,
                info.st_uid,
                info.st_gid,
                link,
            )
        dirs[:] = [
            d
            for d in dirs
            if os.path.normpath(os.path.join(relative_dir, d)) not in EXCLUDED
        ]
    return state


def write_layer(root, before, after, output, timestamp=None):
    changed = sorted(path for path, meta in after.items() if before.get(path) != meta)
    deleted = sorted(path for path in before if path not in after)
    # A deleted directory needs one whiteout, not one per descendant.
    deleted = [
        p for p in deleted if not any(p.startswith(d + "/") for d in deleted if d != p)
    ]

    def clean(info):
        info.uname = info.gname = ""
        if timestamp is not None:
            info.mtime = timestamp
        # xattrs (ACLs, file capabilities) as PAX records, as Docker writes them.
        full = root / info.name
        for key in os.listxattr(full, follow_symlinks=False):
            value = os.getxattr(full, key, follow_symlinks=False)
            info.pax_headers["SCHILY.xattr." + key] = value.decode(
                "utf-8", "surrogateescape"
            )
        return info

    with tarfile.open(output, "w", format=tarfile.PAX_FORMAT) as tar:
        for path in deleted:
            directory, _, base = path.rpartition("/")
            whiteout = tarfile.TarInfo(
                (directory + "/" if directory else "") + ".wh." + base
            )
            tar.addfile(whiteout)
        for path in changed:
            tar.add(root / path, arcname=path, recursive=False, filter=clean)
    return len(changed), len(deleted)


def run_in_chroot(root, command, env, workdir):
    (root / workdir.lstrip("/")).mkdir(parents=True, exist_ok=True)
    script = f"cd {shlex.quote(workdir)} && exec /bin/sh -c {shlex.quote(command)}"
    # Same isolation dockerTools uses for runAsRoot: fresh PID/mount/IPC/UTS
    # namespaces, the VM's /dev and /sys, and a private /proc. The fresh network
    # namespace has only lo; enable it for local service tests without egress.
    # The VM's PATH finds unshare/chroot; the chroot gets only the image env.
    assignments = [f"{key}={value}" for key, value in env.items()]
    subprocess.run(
        [
            "unshare",
            "-imnpuf",
            "--mount-proc",
            "sh",
            "-c",
            (
                "chroot=$(command -v chroot) root=$1 script=$2; shift 2; "
                "ip link set lo up && "
                'mount --rbind /proc "$root/proc" && '
                'exec env -i "$@" "$chroot" "$root" /bin/sh -c "$script"'
            ),
            "sh",
            str(root),
            script,
            *assignments,
        ],
        check=True,
    )


@contextmanager
def mounted_inputs(source, destination):
    """Mount independent components without exposing host-store symlinks."""
    with tempfile.TemporaryDirectory(prefix="linux-build-mounts-") as directory:
        staging = Path(directory)
        components = []
        for entry in sorted(Path(source).iterdir()):
            resolved = entry.resolve(strict=True)
            target = staging / entry.name
            if resolved.is_dir():
                target.mkdir()
            elif resolved.is_file():
                target.touch()
            else:
                raise ValueError(f"Unsupported build input: {entry}")
            components.append((resolved, Path(destination) / entry.name))
        staging.chmod(0o555)
        mounted = False
        try:
            subprocess.run(
                ["mount", "--bind", "-o", "ro", str(staging), str(destination)],
                check=True,
            )
            mounted = True
            for resolved, target in components:
                subprocess.run(
                    ["mount", "--bind", "-o", "ro", str(resolved), str(target)],
                    check=True,
                )
            yield
        finally:
            if mounted:
                # Also clean up successfully mounted siblings after a failure.
                subprocess.run(["umount", "-R", str(destination)], check=True)


def build(args):
    plan = json.loads(Path(args.plan).read_text())
    if plan.get("requireOffline") and any(
        name != "lo" for _, name in socket.if_nameindex()
    ):
        raise RuntimeError("Linux image build requires an environment with no network device")
    base = BaseImage(args.base)
    manifest = base.manifest
    root = Path(args.root)
    root.mkdir(parents=True)
    for layer in manifest["layers"]:
        apply_layer(root, base.blob(layer))
    config = base.config
    if plan.get("requireOffline") and (
        config.get("os") != "linux" or config.get("architecture") != "amd64"
    ):
        raise ValueError("Linux image build requires a linux/amd64 base image")
    image = config.get("config", {})
    env = dict(item.split("=", 1) for item in image.get("Env") or [])
    workdir = image.get("WorkingDir") or "/"
    history = []

    before = snapshot(root)
    for mount in ("proc", "sys", "dev", INPUTS):
        (root / mount).mkdir(exist_ok=True)
    subprocess.run(["mount", "--rbind", "/dev", str(root / "dev")], check=True)
    # The Nix sandbox (isolation = "namespace") has no /sys; a VM does.
    if Path("/sys/kernel").is_dir():
        subprocess.run(["mount", "--rbind", "/sys", str(root / "sys")], check=True)
    try:
        with mounted_inputs(args.inputs, root / INPUTS):
            for index, step in enumerate(plan["steps"]):
                op = step["op"]
                print(f"--- step {index}: {op}", flush=True)
                history.append(
                    {
                        "created_by": f"{op} "
                        + json.dumps(step.get("original", step.get("cmd", step)))[:200]
                    }
                )
                if op == "RUN":
                    run_env = {
                        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                        "HOME": "/root",
                    }
                    run_env.update(env)
                    if "sourceDateEpoch" in plan:
                        run_env["SOURCE_DATE_EPOCH"] = str(plan["sourceDateEpoch"])
                    run_env.update(plan.get("buildEnv", {}))
                    run_in_chroot(root, step["cmd"], run_env, workdir)
                else:
                    raise ValueError(f"Unsupported step {op}")
    finally:
        subprocess.run(["umount", "-R", str(root / "dev")], check=False)
        subprocess.run(["umount", "-R", str(root / "sys")], check=False)
        for mount in ("proc", "sys", "dev", INPUTS):
            if (
                (root / mount).is_dir()
                and not any((root / mount).iterdir())
                and mount not in before
            ):
                (root / mount).rmdir()
    after = snapshot(root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    changed, deleted = write_layer(
        root, before, after, out / "layer.tar", plan.get("sourceDateEpoch")
    )
    if plan.get("exportPaths"):
        export_artifacts(
            root,
            plan["exportPaths"],
            out / "artifacts.tar",
            plan.get("sourceDateEpoch", 1),
        )
    image["Env"] = [f"{k}={v}" for k, v in env.items()]
    (out / "config.json").write_text(json.dumps(image, indent=2))
    (out / "history.json").write_text(json.dumps(history, indent=2))
    print(f"layer: {changed} changed, {deleted} deleted paths", flush=True)


def assemble(args):
    """OCI layout: base layers (symlinked), new layers, updated config."""
    base = BaseImage(args.base)
    manifest = base.manifest
    config = base.config
    out = Path(args.out)
    blobs = out / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    media = {
        "application/vnd.docker.image.rootfs.diff.tar.gzip": "application/vnd.oci.image.layer.v1.tar+gzip",
        "application/vnd.docker.image.rootfs.diff.tar": "application/vnd.oci.image.layer.v1.tar",
    }
    layers = []
    for layer in manifest["layers"]:
        hexdigest = layer["digest"].split(":")[1]
        if not (blobs / hexdigest).exists():
            (blobs / hexdigest).symlink_to(base.blob(layer))
        layers.append(
            {**layer, "mediaType": media.get(layer["mediaType"], layer["mediaType"])}
        )
    for directory in args.layer:
        directory = Path(directory)
        tar = directory / "layer.tar"
        with tar.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if not (blobs / digest).exists():
            (blobs / digest).symlink_to(tar)
        layers.append(
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar",
                "digest": "sha256:" + digest,
                "size": tar.stat().st_size,
            }
        )
        config["rootfs"]["diff_ids"].append("sha256:" + digest)
        if (directory / "config.json").exists():
            config["config"] = json.loads((directory / "config.json").read_text())
        for entry in (
            json.loads((directory / "history.json").read_text())
            if (directory / "history.json").exists()
            else [{"created_by": "nix layer"}]
        ):
            config.setdefault("history", []).append(
                {**entry, "created": "1970-01-01T00:00:01Z"}
            )
    if getattr(args, "config", None):
        config["config"].update(json.loads(Path(args.config).read_text()))
    config["created"] = "1970-01-01T00:00:01Z"

    def write_blob(data):
        digest = hashlib.sha256(data).hexdigest()
        (blobs / digest).write_bytes(data)
        return digest

    config_bytes = json.dumps(config, sort_keys=True).encode()
    config_digest = write_blob(config_bytes)
    oci_manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": "sha256:" + config_digest,
                "size": len(config_bytes),
            },
            "layers": layers,
        },
        sort_keys=True,
    ).encode()
    manifest_digest = write_blob(oci_manifest)
    (out / "index.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": "sha256:" + manifest_digest,
                        "size": len(oci_manifest),
                    }
                ],
            }
        )
    )
    (out / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    b = sub.add_parser("build")
    for name in ("base", "plan", "inputs", "root", "out"):
        b.add_argument("--" + name, required=True)
    a = sub.add_parser("assemble")
    a.add_argument("--base", required=True)
    a.add_argument("--layer", action="append", default=[])
    a.add_argument("--out", required=True)
    a.add_argument("--config", help="Explicit runtime configuration overrides")
    args = parser.parse_args()
    (build if args.action == "build" else assemble)(args)


if __name__ == "__main__":
    main()
