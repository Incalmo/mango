"""Tests for mango.authoring.php_prefix.

EVERYTHING HERE USES A MOCK DOCKER (``MockDocker``, PROOF = "MOCK-NOT-A-VERIFICATION") AND, unless noted, a
mock apt lock hash. Nothing runs Docker, PHP, Composer, Mango's prepare_image or the network, and a passing
test is NOT a verification claim about any real image. Real-Mango hashing is exercised only where Mango
imports (``RealMangoHashTests``).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mango.authoring import composer_lock, remote_common
from mango.authoring import php_prefix as pre

BASE = "php@sha256:" + "1" * 64
RUN = (
    "apt-get update && apt-get install -y --no-install-recommends "
    "bash curl git libzip-dev "
    "&& docker-php-ext-configure gd --with-freetype --with-jpeg "
    "&& docker-php-ext-install -j4 bcmath exif gd zip "
    "&& a2enmod rewrite headers "
    "&& rm -rf /var/lib/apt/lists/*"
)
POST = ["docker-php-ext-configure gd --with-freetype --with-jpeg", "docker-php-ext-install -j4 bcmath exif gd zip",
        "a2enmod rewrite headers", "rm -rf /var/lib/apt/lists/*"]
ENV = {"COMPOSER_ALLOW_SUPERUSER": "1"}
APT_IMAGE = "sha256:" + "a" * 64
MODULES = "[PHP Modules]\nbcmath\nCore\nexif\ngd\nzip\n\n[Zend Modules]\n"


def fake_lock_hash(package_request: dict) -> str:
    """MOCK: stands in for Mango's canonical hash so tests can run without Mango."""
    return hashlib.sha256(json.dumps(package_request, sort_keys=True).encode()).hexdigest()


def package_request() -> dict:
    return {"manager": "apt", "base": "docker.io/library/" + BASE, "sources": ["deb http://deb.debian.org/debian bookworm main"],
            "stages": [{"packages": ["bash", "curl", "git", "libzip-dev"]}]}


def make_request(**over) -> dict:
    pr = package_request()
    request = {
        "id": "snipe-it-test-toolchain-prefix", "base": BASE, "platform": "linux/amd64", "php_version": "8.3.30",
        "required_php_extensions": ["bcmath", "exif", "gd", "zip"],
        "apt": {"package_request": pr, "request_sha256": fake_lock_hash(pr)},
        "recipe": {"runs": [RUN], "env": dict(ENV)},
        "post_apt": {"commands": POST, "script": " && ".join(POST)},
        "env": dict(ENV), "inputs": [], "limits": dict(pre.LIMITS),
    }
    request.update(over)
    return request


class MockDocker:
    PROOF = "MOCK-NOT-A-VERIFICATION"

    def __init__(self, *, free=100 * pre.GIB, exit_code=0, php="8.3.30", modules=MODULES, network=None, bad_run_image=False):
        self.free, self.exit_code, self.php, self.modules = free, exit_code, php, modules
        self.network_override, self.bad_run_image = network, bad_run_image
        self.containers: dict[str, dict] = {}
        self.images = {APT_IMAGE, "sha256:" + "b" * 64}
        self.commits: list[list[str]] = []
        self.copied: list[tuple[str, bytes, str]] = []
        self.removed: list[str] = []
        self.n = 0

    def free_bytes(self):
        return self.free

    def image_inspect(self, ref):
        if pre.normalise_repo(ref) == BASE or ref.startswith("composer@"):
            return {"Id": "sha256:" + "c" * 64, "Os": "linux", "Architecture": "amd64", "RepoDigests": [ref],
                    "Config": {"Entrypoint": ["docker-php-entrypoint"], "Cmd": ["apache2-foreground"]}}
        if ref in self.images:
            return {"Id": ref}
        raise pre.PrefixError("no such image", "docker")

    def create(self, image, entrypoint, cmd, network, limits):
        self.n += 1
        name = f"mock-{self.n}"
        self.containers[name] = {"image": image, "entrypoint": entrypoint, "cmd": cmd, "network": network, "limits": limits}
        return name

    def copy_in(self, name, tar, dest):
        self.copied.append((name, tar, dest))

    def copy_out(self, name, source, destination):
        Path(destination, "composer").write_bytes(b"#!/usr/bin/env php\nPHAR-BYTES")

    def start(self, name):
        c = self.containers[name]
        if c["entrypoint"] == "/bin/sh":
            return self.exit_code, "", "boom" if self.exit_code else ""
        if c["cmd"][:1] == ["-r"]:
            return 0, self.php, ""
        if c["cmd"] == ["-m"]:
            return 0, self.modules, ""
        return 0, "", ""

    def inspect(self, name):
        c = self.containers[name]
        limited = c["limits"]
        return {"Image": "sha256:" + "f" * 64 if self.bad_run_image else c["image"],
                "State": {"ExitCode": self.exit_code if c["entrypoint"] == "/bin/sh" else 0},
                "HostConfig": {"NetworkMode": self.network_override or c["network"],
                               "NanoCpus": 1_500_000_000 if limited else 0, "Memory": 4 * pre.GIB if limited else 0,
                               "MemorySwap": 4 * pre.GIB if limited else 0, "PidsLimit": 1024 if limited else 0},
                "Config": {"Entrypoint": [c["entrypoint"]], "Cmd": c["cmd"]}}

    def commit(self, name, changes):
        self.commits.append(changes)
        image = "sha256:" + hashlib.sha256(f"commit-{len(self.commits)}".encode()).hexdigest()
        self.images.add(image)
        self.last_commit_source = self.containers[name]["image"]
        return image

    def remove(self, name):
        self.removed.append(name)


def apt_prepare_mock(package_request_doc):
    """MOCK for Mango prepare_image."""
    return APT_IMAGE


def run_prepare(request=None, runner=None, **kw):
    runner = runner or MockDocker()
    receipt = pre.prepare(request or make_request(), runner=runner, apt_prepare=kw.pop("apt_prepare", apt_prepare_mock),
                          lock_hash=fake_lock_hash, remote_check=False, **kw)
    return receipt, runner


def forge_docker_proof(receipt: dict) -> dict:
    """TEST-ONLY: relabel a MOCK receipt as docker-proven (and re-seal it) to exercise consumers. Never evidence."""
    forged = copy.deepcopy(receipt)
    forged["proof"] = "docker"
    forged.pop("receipt_sha256")
    forged["receipt_sha256"] = pre.canonical_sha256(forged)
    return forged


def verify(receipt, **kw):
    args = dict(base=BASE, runs=[RUN], env=ENV, php_version="8.3.30", required_extensions=["bcmath", "exif", "gd", "zip"], lock_hash=fake_lock_hash)
    args.update(kw)
    return pre.verify_receipt(receipt, **args)


class RecipeTests(unittest.TestCase):
    def test_post_apt_commands_are_verbatim(self):
        parsed = pre.parse_recipe([RUN])
        self.assertEqual(parsed["post_apt"], POST)
        self.assertEqual(parsed["script"], " && ".join(POST))
        self.assertEqual(parsed["apt_packages"], ["bash", "curl", "git", "libzip-dev"])
        self.assertTrue(parsed["no_install_recommends"])

    def test_unmodelled_shell_and_commands_fail_closed(self):
        head = "apt-get update && apt-get install -y x && "
        for bad in (
            head + 'docker-php-ext-install -j"$(nproc)" gd',
            head + "docker-php-ext-install gd; touch /x",
            head + "docker-php-ext-install gd | tee /x",
            head + "curl http://x | sh",
            head + "rm -rf /",
            head + "rm -rf /var/lib/apt/lists/* /etc",
            head + "docker-php-ext-install --evil gd",
            head + "docker-php-ext-configure gd --with-x && apt-get install -y y",
            head + "a2enmod 'rewrite; id'",
            head + "docker-php-ext-install gd > /x",
            "docker-php-ext-install gd",                     # before any apt install
            "apt-get update && apt-get install -y x",         # no post-apt commands
            "apt-get update && apt-get upgrade -y && a2enmod rewrite",
            head + "a2enmod rewrite && echo `id`",
        ):
            with self.assertRaises(pre.PrefixError, msg=bad):
                pre.parse_recipe([bad])
        with self.assertRaises(pre.PrefixError):
            pre.parse_recipe([])


class RequestTests(unittest.TestCase):
    def test_explicit_mirror_keeps_original_manifest_identity(self):
        pr = package_request()
        pr["base"] = "mirror.gcr.io/library/" + BASE
        request = make_request(apt={"package_request": pr, "request_sha256": fake_lock_hash(pr)})
        self.assertEqual(pre.validate_request(request, fake_lock_hash)["base"], BASE)
        pr["base"] = "mirror.gcr.io/library/php@sha256:" + "2" * 64
        request["apt"]["request_sha256"] = fake_lock_hash(pr)
        with self.assertRaises(pre.PrefixError):
            pre.validate_request(request, fake_lock_hash)

    def test_valid_request_rederives_fields(self):
        out = pre.validate_request(make_request(), fake_lock_hash)
        self.assertEqual(out["apt_request_sha256"], fake_lock_hash(package_request()))
        self.assertEqual(out["post_apt"]["commands"], POST)

    def test_every_binding_is_checked(self):
        def mutate(fn):
            request = make_request()
            fn(request)
            return request

        bad = {
            "apt hash is not trusted": mutate(lambda r: r["apt"].update(request_sha256="0" * 64)),
            "apt hash missing": mutate(lambda r: r["apt"].pop("request_sha256")),
            "apt base differs": mutate(lambda r: r["apt"]["package_request"].update(base="docker.io/library/php@sha256:" + "9" * 64)),
            "apt packages differ": mutate(lambda r: r["apt"]["package_request"]["stages"][0].update(packages=["git"])),
            "apt recommends differ": mutate(lambda r: r["apt"]["package_request"]["stages"][0].update(install_recommends=True)),
            "script tampered": mutate(lambda r: r["post_apt"].update(script="true")),
            "commands tampered": mutate(lambda r: r["post_apt"].update(commands=POST[:1])),
            "env differs": mutate(lambda r: r.update(env={"COMPOSER_ALLOW_SUPERUSER": "0"})),
            "env unmodelled": mutate(lambda r: r["recipe"].update(env={"FOO": "1"})),
            "extensions differ": mutate(lambda r: r.update(required_php_extensions=["gd"])),
            "limits loosened": mutate(lambda r: r.update(limits={**pre.LIMITS, "memory_bytes": 8 * pre.GIB})),
            "tag base": mutate(lambda r: r.update(base="php:8.3")),
            "unknown key": mutate(lambda r: r.update(network="host")),
            "input relative": mutate(lambda r: r.update(inputs=[{"name": "a", "path": "rel", "sha256": "0" * 64}])),
            "non-apt operation": mutate(lambda r: r["apt"]["package_request"]["stages"][0].update(operation="apt_update")),
        }
        for label, request in bad.items():
            with self.subTest(label), self.assertRaises(pre.PrefixError):
                pre.validate_request(request, fake_lock_hash)

    def test_apt_hash_failure_is_not_masked_by_caller_value(self):
        request = make_request()
        request["apt"]["package_request"]["sources"].append("deb http://evil/ x y")
        with self.assertRaises(pre.PrefixError) as ctx:
            pre.validate_request(request, fake_lock_hash)
        self.assertEqual(ctx.exception.code, "apt-hash-mismatch")


class PrepareTests(unittest.TestCase):
    def test_happy_path_commits_after_offline_exact_script(self):
        receipt, docker = run_prepare()
        script_run = docker.containers["mock-1"]
        self.assertEqual((script_run["image"], script_run["entrypoint"], script_run["network"], script_run["limits"]),
                         (APT_IMAGE, "/bin/sh", "none", True))
        self.assertEqual(script_run["cmd"], ["-c", " && ".join(POST)])
        self.assertEqual(docker.last_commit_source, APT_IMAGE)
        changes = docker.commits[0]
        self.assertIn('ENTRYPOINT ["docker-php-entrypoint"]', changes)   # original config restored, not /bin/sh
        self.assertIn('CMD ["apache2-foreground"]', changes)
        self.assertIn("ENV COMPOSER_ALLOW_SUPERUSER=1", changes)
        self.assertTrue(any(c.startswith("LABEL " + pre.LABEL + "=") for c in changes))
        self.assertEqual(receipt["proof"], "MOCK-NOT-A-VERIFICATION")
        self.assertTrue(receipt["authoring_only"])
        self.assertEqual(receipt["toolchain"]["php_extensions"], ["bcmath", "core", "exif", "gd", "zip"])
        self.assertEqual(receipt["toolchain"]["runtime_image_id"], receipt["image_id"])
        self.assertEqual(receipt["chain"]["apt_request_sha256"], fake_lock_hash(package_request()))
        self.assertEqual(set(docker.removed), set(docker.containers))     # nothing left behind
        # verification containers ran --network none too
        self.assertTrue(all(c["network"] == "none" for c in docker.containers.values()))

    def test_mock_receipts_are_never_accepted_as_proof(self):
        receipt, _ = run_prepare()
        with self.assertRaises(pre.PrefixError) as ctx:
            verify(receipt)
        self.assertEqual(ctx.exception.code, "receipt-unproven")
        facts = verify(receipt, allow_unproven=True)
        self.assertEqual(facts["image_id"], receipt["image_id"])
        self.assertEqual(facts["recipe_sha256"], pre.recipe_sha256(BASE, [RUN], ENV))

    def test_each_failure_fails_closed_without_receipt(self):
        cases = {
            "exit code": dict(runner=MockDocker(exit_code=2)),
            "network not none": dict(runner=MockDocker(network="bridge")),
            "wrong runtime image": dict(runner=MockDocker(bad_run_image=True)),
            "disk reserve": dict(runner=MockDocker(free=10 * pre.GIB)),
            "php version": dict(runner=MockDocker(php="8.2.1")),
            "missing extension": dict(runner=MockDocker(modules="[PHP Modules]\nbcmath\ngd\nzip\n")),
            "bad apt image": dict(runner=MockDocker(), apt_prepare=lambda pr: "php:latest"),
            "unknown apt image": dict(runner=MockDocker(), apt_prepare=lambda pr: "sha256:" + "d" * 64),
        }
        for label, kw in cases.items():
            with self.subTest(label), self.assertRaises(pre.ResolverError):
                run_prepare(**kw)
        docker = MockDocker(exit_code=2)
        with self.assertRaises(pre.PrefixError):
            run_prepare(runner=docker)
        self.assertEqual(docker.commits, [])                                # nothing committed after a failed run

    def test_input_artifact_is_hash_bound_and_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blob.bin"
            path.write_bytes(b"artifact")
            good = hashlib.sha256(b"artifact").hexdigest()
            request = make_request(inputs=[{"name": "blob.bin", "path": str(path), "sha256": good}])
            receipt, docker = run_prepare(request)
            self.assertEqual(receipt["chain"]["inputs"], {"blob.bin": good})
            self.assertEqual(verify(forge_docker_proof(receipt), inputs={"blob.bin": good})["image_id"], receipt["image_id"])
            with self.assertRaises(pre.PrefixError):                        # receipt with inputs, plan without
                verify(forge_docker_proof(receipt))
            self.assertEqual(docker.copied[0][2], pre.INPUT_DIR)
            request["inputs"][0]["sha256"] = "0" * 64
            with self.assertRaises(pre.PrefixError):
                run_prepare(request)
            link = Path(tmp) / "link"
            link.symlink_to(path)
            with self.assertRaises(pre.PrefixError):
                run_prepare(make_request(inputs=[{"name": "link", "path": str(link), "sha256": good}]))

    def test_remote_only_guard(self):
        with self.assertRaises(pre.ResolverError):
            pre.prepare(make_request(), runner=MockDocker(), apt_prepare=apt_prepare_mock, lock_hash=fake_lock_hash)


class VerifyReceiptTests(unittest.TestCase):
    def setUp(self):
        receipt, _ = run_prepare()
        self.receipt = forge_docker_proof(receipt)       # TEST-ONLY forged proof label

    def resealed(self, mutate):
        receipt = copy.deepcopy(self.receipt)
        mutate(receipt)
        receipt.pop("receipt_sha256")
        receipt["receipt_sha256"] = pre.canonical_sha256(receipt)   # attacker re-seals: chain must still fail
        return receipt

    def test_forged_docker_label_passes_only_structurally(self):
        self.assertEqual(verify(self.receipt)["image_id"], self.receipt["image_id"])

    def test_tampering_is_detected(self):
        unsealed = copy.deepcopy(self.receipt)
        unsealed["image_id"] = "sha256:" + "e" * 64
        with self.assertRaises(pre.PrefixError):
            verify(unsealed)
        for label, mutate in {
            "script": lambda r: r["post_apt"].update(script="true"),
            "recipe runs": lambda r: r["recipe"].update(runs=["apt-get update"]),
            "chain apt hash": lambda r: r["chain"].update(apt_request_sha256="0" * 64),
            "chain script hash": lambda r: r["chain"].update(script_sha256="0" * 64),
            "chain env hash": lambda r: r["chain"].update(env_sha256="0" * 64),
            "chain recipe hash": lambda r: r["chain"].update(recipe_sha256="0" * 64),
            "chain base": lambda r: r["chain"].update(base="php@sha256:" + "2" * 64),
            "chain inputs": lambda r: r["chain"].update(inputs={"x": "0" * 64}),
            "apt packages": lambda r: r["apt"]["package_request"]["stages"][0].update(packages=["git"]),
            "network": lambda r: r["execution"].update(network_mode="bridge"),
            "exit code": lambda r: r["execution"].update(exit_code=1),
            "image id": lambda r: r.update(image_id="php:latest"),
            "runtime image": lambda r: r["toolchain"].update(runtime_image_id="sha256:" + "e" * 64),
            "modules hash": lambda r: r["toolchain"].update(php_extensions_sha256="0" * 64),
            "missing ext": lambda r: r["toolchain"].update(php_extensions=["gd"], php_extensions_sha256=pre.sha256_hex(b"gd")),
            "limits": lambda r: r.update(limits={**pre.LIMITS, "cpus": "8"}),
            "not authoring": lambda r: r.update(authoring_only=False),
        }.items():
            with self.subTest(label), self.assertRaises(pre.PrefixError):
                verify(self.resealed(mutate))

    def test_caller_side_plan_must_match(self):
        for kw in (dict(base="php@sha256:" + "2" * 64), dict(runs=[RUN.replace("bcmath ", "")]), dict(env={}),
                   dict(php_version="8.3.31"), dict(required_extensions=["gd"]), dict(lock_hash=lambda pr: "0" * 64)):
            with self.subTest(kw), self.assertRaises(pre.PrefixError):
                verify(self.receipt, **kw)

    def test_garbage_receipts(self):
        for value in (None, {}, [], {"kind": pre.KIND}):
            with self.assertRaises(pre.PrefixError):
                verify(value)


class ExtractComposerTests(unittest.TestCase):
    PINNED = "composer@sha256:" + "5" * 64

    def test_extracts_without_executing(self):
        docker = MockDocker()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "composer.phar"
            evidence = pre.extract_composer_phar(self.PINNED, out, runner=docker, remote_check=False)
            self.assertEqual(evidence["sha256"], hashlib.sha256(out.read_bytes()).hexdigest())
            self.assertFalse(evidence["executed"])
            self.assertEqual(evidence["extracted_from"], self.PINNED)
            self.assertTrue(all(c["network"] == "none" for c in docker.containers.values()))
            self.assertEqual(docker.removed, ["mock-1"])
            self.assertFalse(list(Path(tmp).glob("tmp*")))                  # scratch cleaned
            with self.assertRaises(pre.PrefixError):                          # never overwrites
                pre.extract_composer_phar(self.PINNED, out, runner=docker, remote_check=False)

    def test_tags_and_other_refs_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            for ref in ("composer:2.8.12", "composer@sha256:abc", "evil/composer@sha256:" + "5" * 64):
                with self.assertRaises(pre.PrefixError):
                    pre.extract_composer_phar(ref, Path(tmp) / "x", runner=MockDocker(), remote_check=False)

    def test_remote_guard_and_non_php_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(pre.ResolverError):
                pre.extract_composer_phar(self.PINNED, Path(tmp) / "x", runner=MockDocker())

            class Bad(MockDocker):
                def copy_out(self, name, source, destination):
                    Path(destination, "composer").write_bytes(b"\x7fELF")

            with self.assertRaises(pre.PrefixError):
                pre.extract_composer_phar(self.PINNED, Path(tmp) / "y", runner=Bad(), remote_check=False)
            self.assertFalse((Path(tmp) / "y").exists())


try:
    import mango.authoring.packages  # noqa: F401
    HAVE_MANGO = True
except ImportError:
    HAVE_MANGO = False


@unittest.skipUnless(HAVE_MANGO, "Mango not importable")
class RealMangoHashTests(unittest.TestCase):
    """Uses Mango's real LockRequest code (pure hashing only; no Docker)."""

    def test_hash_is_canonical_request_and_sensitive(self):
        base = "docker.io/library/php@sha256:" + "1" * 64
        pr = {"manager": "apt", "base": base, "sources": ["deb http://deb.debian.org/debian bookworm main"], "stages": [{"packages": ["git"]}]}
        h1 = pre.mango_lock_hash(pr)
        from mango.authoring.packages import LockRequest
        self.assertEqual(h1, hashlib.sha256(json.dumps(
            LockRequest.model_validate(pr).cache_document(), sort_keys=True
        ).encode()).hexdigest())
        self.assertNotEqual(h1, pre.mango_lock_hash({**pr, "stages": [{"packages": ["git", "curl"]}]}))
        with self.assertRaises(pre.PrefixError):
            pre.mango_lock_hash({**pr, "base": "php:latest"})

    def test_request_validates_with_real_hash(self):
        pr = package_request()
        pr["base"] = "docker.io/library/" + BASE
        request = make_request(apt={"package_request": pr, "request_sha256": pre.mango_lock_hash(pr)})
        self.assertEqual(pre.validate_request(request)["apt_request_sha256"], request["apt"]["request_sha256"])


class PublicApiTests(unittest.TestCase):
    def test_shares_the_public_toolchain_helpers(self):
        self.assertIs(pre.ResolverError, remote_common.ResolverError)
        self.assertIs(pre.ENV_ALLOWED, composer_lock.ENV_ALLOWED)
        self.assertTrue(issubclass(pre.PrefixError, remote_common.ResolverError))

    def test_cli_runs_as_a_module_and_is_remote_gated(self):
        env = {**os.environ}
        env.pop("MANGO_REMOTE_AUTHOR", None)
        src = str(Path(pre.__file__).resolve().parents[2])
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        helped = subprocess.run([sys.executable, "-m", "mango.authoring.php_prefix", "--help"], env=env, capture_output=True, text=True)
        self.assertEqual(helped.returncode, 0, helped.stderr)
        self.assertIn("mango-prepare-php", helped.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            request = Path(tmp) / "r.json"
            request.write_text("{}")
            blocked = subprocess.run([sys.executable, "-m", "mango.authoring.php_prefix", "prepare", str(request),
                                      "--output", str(Path(tmp) / "o.json")], env=env, capture_output=True, text=True)
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("MANGO_REMOTE_AUTHOR", blocked.stderr)


if __name__ == "__main__":
    unittest.main()
