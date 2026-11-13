"""Builder-side export adapter, executed with the environment's pinned harness.

No Docker socket or Docker executable is required inside the builder.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


def archive_layout(layout, index, destination, reference):
    (descriptor,) = index["manifests"]
    digest = descriptor["digest"]
    descriptor.setdefault("annotations", {})["io.containerd.image.name"] = (
        reference + ":" + digest.removeprefix("sha256:")[:16]
    )
    data = json.dumps(index).encode()
    with tarfile.open(destination, "w", dereference=True) as stream:
        for path in sorted(layout.rglob("*")):
            if path == layout / "index.json":
                continue
            stream.add(path, arcname=str(path.relative_to(layout)), recursive=False)
        info = tarfile.TarInfo("index.json")
        info.size = len(data)
        stream.addfile(info, io.BytesIO(data))
    return reference + "@" + digest


def main():
    # Imports deliberately resolve to the pinned harness's PYTHONPATH.
    from mango import HARNESS, cli
    from mango.environment.declaration import load_manifest
    from mango.outputs.oci import oci_layout
    from mango.runtime.materialize import materialize_runtime
    from mango.runtime.task import generate_task

    root, work, output = map(Path, sys.argv[1:4])
    uid, gid = map(int, sys.argv[4:6])
    action, *extra = sys.argv[6:]
    export = action in {"load", "prepare", "compose", "run"}
    subprocess.run(
        [sys.executable, "-c", "from mango.cli import main; main()",
         str(root), "--workspace", str(work), "fetch" if export else action, *extra],
        check=True,
    )
    if not export:
        return
    declared = load_manifest(root, HARNESS)
    app = declared.model_dump()
    verifiers = declared.validate_task_mappings(root)
    drvs = cli.instantiate(root, (app["images"] | app["builds"]).values())
    digest = cli.build_digest(drvs, app, verifiers)
    docker = subprocess.run(
        ["nix-build", "--out-link", str(work / "docker-client"), "--expr",
         "let pkgs = import (import " + str(HARNESS / "lib/pins/nixpkgs.nix")
         + ') { system = "x86_64-linux"; }; in pkgs.docker-client'],
        check=True, stdout=subprocess.PIPE, text=True,
    ).stdout.strip()
    controller = {
        "python": sys.executable, "harness": str(HARNESS),
        "path": docker + "/bin:" + os.environ["PATH"],
        "harbor": shutil.which("harbor"),
    }
    # Build bookkeeping stays in the private /nix volume. Drop privileges before
    # writing anything into the host-owned output directory.
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    references = {}
    records = []
    for number, name in enumerate(app["images"]):
        filename = f"image-{number}.tar"
        with oci_layout(name, work / "links" / name, scratch=output) as (layout, index):
            reference = archive_layout(layout, index, output / filename, "mango.local/" + name)
        references[name] = reference
        records.append({"file": filename, "reference": reference})
    manifest = cli.image_manifest(declared, references, build_id="mango-local", source_digest=digest)
    generate_task(root, manifest.runtime, output / "runtime")
    materialize_runtime(output / "runtime", manifest, digest)
    (output / "images.json").write_text(manifest.model_dump_json(indent=2) + "\n")
    (output / "archives.json").write_text(json.dumps(records) + "\n")
    (output / "controller.json").write_text(json.dumps(controller) + "\n")


if __name__ == "__main__":
    main()
