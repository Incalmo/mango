"""Build, retain, publish, export and run a nix2rle environment."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from nix2rle import HARNESS
from nix2rle.environment.declaration import load_manifest
from nix2rle.environment.workspace import workspace_lock
from nix2rle.outputs import offline
from nix2rle.outputs.oci import load, push
from nix2rle.runtime.manifest import BuildManifest
from nix2rle.runtime.materialize import materialize_runtime
from nix2rle.runtime.task import generate_task

#: Exit statuses for failures a hosted builder must tell apart. Argparse keeps 2.
EXIT_EVALUATION = 10  # declaration or derivation evaluation failed: permanent
EXIT_BUILD = 11  # an offline build failed: permanent for these inputs
EXIT_INPUT_DOWNLOAD = 12  # a pinned input could not be downloaded: retryable
EXIT_PUSH = 13  # pushing to the registry failed: retryable
#: Nix messages for a fixed-output download that failed to transfer (not a hash
#: mismatch, which is permanent).
DOWNLOAD_FAILURES = (
    "unable to download",
    "cannot download",
    "Could not resolve host",
    "Connection timed out",
    "Connection reset",
    "HTTP error 429",
    "HTTP error 50",
)


class Failure(Exception):
    """A classified failure; ``main`` reports it and exits with ``status``."""

    def __init__(self, status, stage, message):
        super().__init__(message)
        self.status, self.stage = status, stage


def state_dir(root):
    """Where everything generated for the environment at ``root`` goes."""
    return root.resolve().parent / "build" / root.name


def instantiate(root, attributes):
    """Derivation paths of the environment's build attributes, in order."""
    result = subprocess.run(
        [
            "nix-instantiate",
            str(root / "default.nix"),
            "--arg",
            "harness",
            "builtins.toPath " + json.dumps(str(HARNESS)),
            *[arg for attr in attributes for arg in ("-A", attr)],
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    # Unrooted .drv files are fine here: fetch roots what it builds.
    for line in result.stderr.splitlines():
        if "did not specify '--add-root'" not in line:
            print(line, file=sys.stderr)
    if result.returncode:
        raise Failure(EXIT_EVALUATION, "evaluation", "Derivation evaluation failed")
    return result.stdout.split()


def realise_derivations(drvs, cwd):
    """Build ``drvs`` in one Nix invocation, classifying a failure.

    Nix's log streams through to stderr unchanged.
    """
    process = subprocess.Popen(
        ["nix-store", "--realise", *drvs],
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    download_failed = False
    for line in process.stderr:
        sys.stderr.write(line)
        download_failed |= any(marker in line for marker in DOWNLOAD_FAILURES)
    if process.wait():
        if download_failed:
            raise Failure(
                EXIT_INPUT_DOWNLOAD, "input-download", "A pinned input failed to download"
            )
        raise Failure(EXIT_BUILD, "build", "An image or application build failed")


def build_digest(drvs, app, verifier_images):
    """Bind loaded images to their derivations and explicit runtime mappings.

    Compose configuration is re-read during prepare, but service-to-image
    mappings require a fresh load so an old images.json cannot silently win.
    """
    identity = {
        "derivations": drvs,
        "images": app["images"],
        "services": app["services"],
        "verifiers": verifier_images,
        "platform": app["platform"],
    }
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def prepare_runtime(root, manifest, digest, workspace=None):
    """Generate the Harbor task from the current declaration and bind its images.

    Compose configuration is re-read from the declaration on every call; the
    loaded image manifest only supplies image references.
    """
    app = load_manifest(root, HARNESS)
    manifest = manifest.model_copy(update={"runtime": app.runtime()})
    state = workspace or state_dir(root)
    temporary = state / "prepared-task"
    if temporary.exists():
        shutil.rmtree(temporary)
    generate_task(root, manifest.runtime, temporary)
    materialize_runtime(temporary, manifest, digest)
    runtime = state / "runtime"
    if runtime.exists():
        shutil.rmtree(runtime)
    temporary.rename(runtime)


def image_manifest(app, references, *, build_id, source_digest):
    """Bind container outputs to services, graders and the egress sidecar."""
    return BuildManifest(
        build_id=build_id,
        source_digest=source_digest,
        services={
            service: references[name] for service, name in app.services.items()
        },
        verifiers={
            key: {service: references[image] for service, image in services.items()}
            for key, services in app.verifiers.items()
        },
        egress_image=references["egress"],
        platform=app.platform,
        runtime=app.runtime(),
    )


def export_source(root, destination):
    """Export a supplied environment and only the runtime harness files."""
    root, destination = root.resolve(), destination.resolve()
    if destination.exists():
        raise ValueError("Export destination must not exist")
    if destination.is_relative_to(HARNESS) or destination.is_relative_to(root):
        raise ValueError(
            "Export destination must be outside the harness and environment"
        )
    ignored = shutil.ignore_patterns(
        "__pycache__", ".git", ".jj", ".direnv", "result", "result-*", "build"
    )
    shutil.copytree(root, destination / "environment", ignore=ignored)
    for name in ("lib", "src", "builders"):
        shutil.copytree(HARNESS / name, destination / name, ignore=ignored)
    for name in ("shell.nix", "default.nix", "flake.nix", "pyproject.toml", "LICENSE", "NOTICE"):
        shutil.copy2(HARNESS / name, destination / name)


def main():
    parser = argparse.ArgumentParser(prog="nix2rle", description=__doc__.split("\n")[0])
    parser.add_argument(
        "environment", type=Path, help="customer-owned environment directory"
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        help="generated state directory outside the environment",
    )
    parser.add_argument(
        "action",
        choices=[
            "validate",
            "validate-contract",
            "build",
            "fetch",
            "check",
            "load",
            "prepare",
            "compose",
            "run",
            "publish",
            "export",
            "offline-export",
            "offline-import",
        ],
    )
    parser.add_argument("--agent", choices=["oracle", "nop"], default="nop")
    parser.add_argument("--destination", type=Path)
    publish = parser.add_argument_group("publish")
    publish.add_argument("--repository", help="registry repository prefix")
    publish.add_argument("--build-id", help="identifier recorded in the manifest")
    publish.add_argument(
        "--source-digest", help="digest of the submitted source snapshot"
    )
    publish.add_argument("--authfile", type=Path, help="registry auth file")
    publish.add_argument("--output", type=Path, help="write the manifest here")
    args = parser.parse_args()
    root = args.environment.resolve()
    if not (root / "default.nix").is_file():
        parser.error(f"no default.nix at {root}")
    if args.action == "validate-contract":
        from nix2rle.environment.contract import validate_environment_contract

        print(json.dumps(validate_environment_contract(root), indent=2))
        return
    try:
        app = load_manifest(root, HARNESS)
        verifier_images = app.validate_task_mappings(root)
    except ValueError as error:
        report(Failure(EXIT_EVALUATION, "evaluation", str(error)))
    if args.action == "validate":
        print(
            json.dumps(
                {
                    "valid": True,
                    "services": app.services,
                    "verifiers": verifier_images,
                    "images": app.images,
                },
                indent=2,
            )
        )
        return
    work = (args.workspace or state_dir(root)).resolve()
    if (
        work.is_relative_to(root)
        or root.is_relative_to(work)
        or HARNESS.is_relative_to(work)
    ):
        parser.error("workspace must be separate from the environment and harness root")
    with workspace_lock(work):
        try:
            execute(args, root, app, work, verifier_images)
        except Failure as failure:
            report(failure)


def report(failure):
    """Print a machine-readable last line and exit with the failure's status."""
    print(
        json.dumps({"error": failure.stage, "message": str(failure)}),
        file=sys.stderr,
    )
    sys.exit(failure.status)


def execute(args, root, declared, work, verifier_images):
    app = declared.model_dump()
    # One link per manifest attribute; its own directory so attribute names
    # (an image called "runtime", say) never collide with generated files.
    links = work / "links"
    links.mkdir(parents=True, exist_ok=True)
    attributes = app["images"] | app["builds"]
    receipt = work / "inputs.json"

    def command(*command, **kwargs):
        return subprocess.run(command, cwd=root, check=True, **kwargs)

    def current_manifest():
        if not (work / "images.json").is_file():
            raise ValueError("No loaded image manifest; run fetch and load first")
        return BuildManifest.model_validate_json((work / "images.json").read_text())

    def frozen_drvs():
        drvs = instantiate(root, attributes.values())
        if json.loads(receipt.read_text()).get("derivations") != drvs:
            raise ValueError(
                "Environment changed since fetch; rebuild before loading/checking"
            )
        return drvs

    def realise():
        drvs = instantiate(root, attributes.values())
        # One invocation so Nix schedules independent builds in parallel.
        realise_derivations(drvs, root)
        for name, drv in zip(attributes, drvs):
            link = links / name
            link.unlink(missing_ok=True)
            command(
                "nix-store",
                "--realise",
                drv,
                "--add-root",
                str(link),
                "--indirect",
                stdout=subprocess.DEVNULL,
            )
        return drvs

    if args.action == "publish":
        missing = [
            option
            for option in ("repository", "build_id", "source_digest")
            if not getattr(args, option, None)
        ]
        if missing:
            raise ValueError(
                "publish requires " + ", ".join("--" + m.replace("_", "-") for m in missing)
            )
        realise()
        try:
            references = push(
                {name: links / name for name in app["images"]},
                args.repository,
                scratch=work / "tmp",
                authfile=args.authfile,
            )
        except (subprocess.CalledProcessError, RuntimeError) as error:
            raise Failure(EXIT_PUSH, "push", f"Registry push failed: {error}") from error
        manifest = image_manifest(
            declared,
            references,
            build_id=args.build_id,
            source_digest=args.source_digest,
        )
        document = manifest.model_dump_json(indent=2) + "\n"
        if args.output:
            args.output.write_text(document)
        print(document, end="")
    elif args.action == "build":
        realise()
    elif args.action == "fetch":
        drvs = realise()
        command(
            "nix-instantiate",
            str(HARNESS / "shell.nix"),
            "--add-root",
            str(work / "shell.drv"),
            "--indirect",
        )
        command(
            "nix-store",
            "--realise",
            str((work / "shell.drv").resolve()),
            "--add-root",
            str(work / "shell-inputs"),
            "--indirect",
        )
        offline.freeze(receipt, offline.resolve(drvs))
    elif args.action == "check":
        offline.check(receipt, offline.resolve(frozen_drvs()))
    elif args.action == "load":
        digest = build_digest(frozen_drvs(), app, verifier_images)
        references = load(
            {name: links / name for name in app["images"]}, scratch=work / "tmp"
        )
        manifest = image_manifest(
            declared, references, build_id="nix2rle-local", source_digest=digest
        )
        prepare_runtime(root, manifest, digest, work)
        (work / "images.json").write_text(manifest.model_dump_json(indent=2) + "\n")
    elif args.action in {"prepare", "compose", "run"}:
        digest = build_digest(
            instantiate(root, attributes.values()), app, verifier_images
        )
        prepare_runtime(root, current_manifest(), digest, work)
        if args.action == "compose":
            from nix2rle.harbor.compose import generate_compose

            result = generate_compose(work, current_manifest())
            result["grading"] = {
                agent: [
                    "nix2rle",
                    str(root),
                    "--workspace",
                    str(work),
                    "run",
                    "--agent",
                    agent,
                ]
                for agent in ("oracle", "nop")
            }
            print(json.dumps(result, indent=2))
        elif args.action == "run":
            from nix2rle.harbor.verdict import run_trial

            run_trial(root, work, HARNESS, args.agent)
    elif args.action in {"export", "offline-export"}:
        if args.destination is None:
            raise ValueError("--destination is required")
        destination = args.destination.resolve()
        export_source(root, destination)
        if args.action == "export":
            return
        frozen_drvs()
        records = json.loads(receipt.read_text())
        extra = subprocess.check_output(
            [
                "nix-store",
                "--query",
                "--requisites",
                "--include-outputs",
                str((work / "shell.drv").resolve()),
            ],
            text=True,
        ).splitlines()
        # Pinned sources fetched during evaluation (fetchTarball), not builds.
        sources = [
            subprocess.check_output(
                [
                    "nix",
                    "eval",
                    "--raw",
                    "--impure",
                    "--expr",
                    (f'(import {HARNESS}/lib/pins/locked-source.nix "{name}").outPath'),
                ],
                text=True,
            )
            for name in ("nixpkgs", "nix2container")
        ]
        retained = sorted(set(records["paths"] + extra + sources))
        with (destination / "inputs.nar").open("wb") as stream:
            command("nix-store", "--export", *retained, stdout=stream)
        (destination / "offline-paths.json").write_text(
            json.dumps(retained, indent=2) + "\n"
        )
        target_work = destination / "build" / "environment"
        target_work.mkdir(parents=True)
        # Only explicit receipts/output links: never trial logs or secrets.
        shutil.copy2(receipt, target_work / "inputs.json")
        (target_work / "links").mkdir()
        for name in attributes:
            (target_work / "links" / name).symlink_to((links / name).resolve())
        for name in ("shell.drv", "shell-inputs"):
            (target_work / name).symlink_to((work / name).resolve())
    else:
        with (HARNESS / "inputs.nar").open("rb") as stream:
            command("nix-store", "--import", stdin=stream)
        retained = json.loads((HARNESS / "offline-paths.json").read_text())
        for index in range(0, len(retained), 100):
            command(
                "nix",
                "build",
                "--offline",
                "--out-link",
                str(work / f"imported-{index}"),
                *retained[index : index + 100],
            )
