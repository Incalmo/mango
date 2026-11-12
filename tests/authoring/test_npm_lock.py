"""Mocked tests for remote_node_deps; no Docker, network or npm is ever invoked.

Tarballs are synthetic bytes; the container is a scripted fake or subprocess is mocked.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import itertools
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mango.authoring import npm_lock as deps

BASE = "node@sha256:" + "a" * 64
NPM = "10.8.2"


def sri(data: bytes, algo: str = "sha512") -> str:
    return f"{algo}-" + base64.b64encode(hashlib.new(algo, data).digest()).decode()


TARBALLS = {
    "left-pad": b"tgz-left-pad-1.3.0",
    "typescript": b"tgz-typescript-5.4.5",
    "fsevents": b"tgz-fsevents-2.3.3",
    "@scope/lib": b"tgz-scope-lib-2.0.0",
}


def url_for(name: str, version: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return f"https://registry.npmjs.org/{name}/-/{base}-{version}.tgz"


def entry(name: str, version: str, **extra) -> dict:
    return {
        "version": version,
        "resolved": url_for(name, version),
        "integrity": sri(TARBALLS[name]),
        **extra,
    }


def make_lock() -> dict:
    return {
        "name": "app",
        "lockfileVersion": 3,
        "requires": True,
        "packages": {
            "": {
                "name": "app",
                "version": "1.0.0",
                "dependencies": {"left-pad": "1.3.0"},
            },
            "node_modules/left-pad": entry("left-pad", "1.3.0", hasInstallScript=True),
            "node_modules/@scope/lib": entry("@scope/lib", "2.0.0"),
            "node_modules/typescript": entry("typescript", "5.4.5", dev=True),
            "node_modules/fsevents": entry(
                "fsevents", "2.3.3", optional=True, os=["darwin"]
            ),
            "node_modules/left-pad/node_modules/bundled": {
                "version": "0.0.1",
                "inBundle": True,
            },
        },
    }


MANIFEST = {
    "name": "app",
    "version": "1.0.0",
    "scripts": {"postinstall": "node x.js", "build": "tsc"},
}


def hidden_for(lock: dict, omit_dev: bool) -> dict:
    packages = {}
    for key, value in lock["packages"].items():
        if (
            key == ""
            or value.get("os") == ["darwin"]
            or (omit_dev and value.get("dev"))
        ):
            continue
        packages[key] = dict(value)
    return {"name": "app", "lockfileVersion": 3, "requires": True, "packages": packages}


def request(**over) -> dict:
    lock = over.pop("lock", make_lock())
    manifest = over.pop("manifest", MANIFEST)
    req = {
        "id": "app",
        "base": BASE,
        "manager": {"name": "npm", "version": NPM},
        "command": "ci",
        "flags": ["--omit=dev", "--no-audit"],
        "files": {
            "package_json": json.dumps(manifest),
            "package_lock": json.dumps(lock),
        },
    }
    req.update(over)
    return req


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeContainer:
    """Scripted stand-in for remote_node_deps.Container; records every call."""

    instances: list[FakeContainer] = []
    hidden: dict | None = None
    hidden_replay: dict | None = None
    npm_version = NPM
    platform = "linux/amd64"
    node_platform = "linux/x64"
    registry = deps.REGISTRY
    mutate_lock_on_ci = False
    fail_cache_add = False
    generated_lock: bytes | None = None  # what a lockless `npm install` writes
    replay_lock: bytes | None = None  # lock bytes after the offline install
    drop_lock_on_replay = False
    mutate_manifest_on_install = False

    def __init__(self, base: str) -> None:
        self.base = base
        self.files: dict[str, bytes] = {}
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.current_hidden: dict = {}
        self.disconnected = False
        FakeContainer.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def image_id(self) -> str:
        return self.base

    def image_platform(self) -> str:
        return self.platform

    def disconnect_networks(self) -> list[str]:
        self.disconnected = True
        return ["bridge"]

    def copy_in(self, source: Path, destination: str) -> None:
        self.files[destination] = Path(source).read_bytes()

    def exec(self, argv, env=None, workdir=None, check=True, timeout=None):
        self.calls.append(list(argv))
        self.envs.append(dict(env or {}))
        out = ""
        code = 0
        if argv[:2] == ["node", "-p"]:
            out = self.node_platform
        elif argv == ["node", "--version"]:
            out = "v20.12.2"
        elif argv == ["npm", "--version"]:
            out = self.npm_version
        elif argv[:3] == ["npm", "config", "get"]:
            out = self.registry
        elif argv[:2] == ["node", "-e"] and "createHash" in argv[2]:
            out = "".join(f"{sha(self.files[p])}  {p}\n" for p in argv[3:])
        elif argv[:2] == ["node", "-e"] and "base64" in argv[2]:
            if argv[3] in self.files:
                out = base64.b64encode(self.files[argv[3]]).decode()
            else:
                code = 1
        elif argv[:2] == ["rm", "-rf"]:
            self.removed_modules = True
        elif argv[:2] == ["node", "-e"]:
            out = json.dumps(self.current_hidden)
        elif argv[:2] in (["npm", "ci"], ["npm", "install"]):
            offline = "--offline" in argv
            lock_path = f"{deps.PROJECT}/package-lock.json"
            if argv[1] == "install":
                if offline and not getattr(self, "removed_modules", False):
                    raise AssertionError("replay must clear node_modules first")
                if offline:
                    if self.drop_lock_on_replay:
                        self.files.pop(lock_path, None)
                    elif self.replay_lock is not None:
                        self.files[lock_path] = self.replay_lock
                elif self.generated_lock is not None:
                    self.files[lock_path] = self.generated_lock
                if self.mutate_manifest_on_install:
                    self.files[f"{deps.PROJECT}/package.json"] += b" "
            if offline and self.disconnected is False:
                raise AssertionError("offline replay ran with the network attached")
            self.current_hidden = (
                self.hidden_replay
                if offline and self.hidden_replay is not None
                else self.hidden
            )
            if self.mutate_lock_on_ci:
                path = next(p for p in self.files if p.endswith("package-lock.json"))
                self.files[path] += b" "
        elif argv[:3] == ["npm", "cache", "add"] and self.fail_cache_add:
            raise deps.ResolverError("ENOTCACHED", "offline-cache-miss")
        return subprocess.CompletedProcess(argv, code, out, "")


def fake_fetch(calls: list | None = None, corrupt: set | None = None):
    index = {
        url_for(n, v): n
        for n, v in (
            ("left-pad", "1.3.0"),
            ("@scope/lib", "2.0.0"),
            ("typescript", "5.4.5"),
            ("fsevents", "2.3.3"),
        )
    }

    def fetch(url, destination, max_bytes):
        if calls is not None:
            calls.append(url)
        name = index[url]
        data = TARBALLS[name] + (b"!" if corrupt and name in corrupt else b"")
        destination.write_bytes(data)

    return fetch


@contextlib.contextmanager
def remote(**attrs):
    FakeContainer.instances = []
    saved = {k: getattr(FakeContainer, k) for k in attrs}
    FakeContainer.hidden = attrs.pop("hidden", None)
    for key, value in attrs.items():
        setattr(FakeContainer, key, value)
    with mock.patch.dict(os.environ, {"MANGO_REMOTE_AUTHOR": "1"}):
        try:
            yield
        finally:
            for key, value in saved.items():
                setattr(FakeContainer, key, value)
            FakeContainer.hidden = None
            FakeContainer.hidden_replay = None


def resolve(req, **kwargs):
    return deps.resolve(
        req, fetch=kwargs.pop("fetch", fake_fetch()), container_factory=FakeContainer
    )


def reason(test: unittest.TestCase, req, code: str, **kwargs) -> deps.ResolverError:
    with test.assertRaises(deps.ResolverError) as caught:
        resolve(req, **kwargs)
    test.assertEqual(caught.exception.code, code, str(caught.exception))
    return caught.exception


# --------------------------------------------------------------------------
class IntegrityTests(unittest.TestCase):
    def test_parse_requires_strong_hash(self) -> None:
        with self.assertRaises(deps.ResolverError) as caught:
            deps.parse_integrity(sri(b"x", "sha1"))
        self.assertEqual(caught.exception.code, "weak-integrity")
        self.assertIn("sha512", deps.parse_integrity(sri(b"x")))

    def test_parse_rejects_garbage_and_wrong_length(self) -> None:
        for bad in ("", "md5-abcd", "sha512-AAAA", "sha512-!!!", None, 5):
            with self.assertRaises(deps.ResolverError, msg=repr(bad)):
                deps.parse_integrity(bad)

    def test_verify_matches_and_mismatch(self) -> None:
        data = b"payload"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.tgz"
            path.write_bytes(data)
            digests = deps.digest_file(path)
            self.assertEqual(digests["size"], 7)
            deps.verify_digests(sri(data), digests)
            deps.verify_digests(f"{sri(data, 'sha1')} {sri(data, 'sha256')}", digests)
            for wrong in (sri(b"other"), f"{sri(data)} {sri(b'other', 'sha256')}"):
                with self.assertRaises(deps.ResolverError) as caught:
                    deps.verify_digests(wrong, digests)
                self.assertEqual(caught.exception.code, "integrity-mismatch")

    def test_url_rules(self) -> None:
        good = "https://registry.npmjs.org/a/-/a-1.0.0.tgz"
        self.assertEqual(deps.check_tarball_url(good), good)
        for bad in (
            "http://registry.npmjs.org/a/-/a-1.0.0.tgz",
            "https://user:pw@registry.npmjs.org/a/-/a-1.0.0.tgz",
            "https://registry.npmjs.org/a/-/a-1.0.0.tgz?token=abc",
            "https://registry.npmjs.org/a/-/a-1.0.0.tgz#sha1-x",
            "git+ssh://git@github.com/a/b.git#abc",
            "https://codeload.github.com/a/b/tar.gz/abc.tar.gz",
            "file:../a",
            "https://registry.npmjs.org/a",
            None,
        ):
            with self.assertRaises(deps.ResolverError, msg=str(bad)):
                deps.check_tarball_url(bad)


class LockAnalysisTests(unittest.TestCase):
    def analyze(self, lock=None, manifest=MANIFEST):
        sources = {
            "package_json": json.dumps(manifest).encode(),
            "package_lock": json.dumps(lock or make_lock()).encode(),
            "lock_name": "package-lock.json",
        }
        return deps.analyze_lock(sources, deps.DEFAULT_LIMITS)

    def code(self, lock=None, manifest=MANIFEST) -> str:
        with self.assertRaises(deps.ResolverError) as caught:
            self.analyze(lock, manifest)
        return caught.exception.code

    def test_accepts_and_reports_unresolved_lifecycle(self) -> None:
        result = self.analyze()
        self.assertEqual(len(result["entries"]), 4)
        self.assertEqual(
            result["bundled"], ["node_modules/left-pad/node_modules/bundled"]
        )
        self.assertEqual(result["lifecycle_packages"][0]["name"], "left-pad")
        self.assertEqual(result["root_lifecycle"], ["postinstall"])

    def test_rejects_workspaces(self) -> None:
        self.assertEqual(
            self.code(manifest={**MANIFEST, "workspaces": ["packages/*"]}), "workspaces"
        )
        lock = make_lock()
        lock["packages"]["packages/a"] = {"version": "1.0.0"}
        self.assertEqual(self.code(lock), "workspaces")
        lock = make_lock()
        lock["packages"]["node_modules/a"] = {"resolved": "packages/a", "link": True}
        self.assertEqual(self.code(lock), "workspaces")

    def test_rejects_git_file_and_http_sources(self) -> None:
        for resolved in (
            "git+ssh://git@github.com/o/r.git#" + "a" * 40,
            "file:../local",
            "http://registry.npmjs.org/a/-/a-1.0.0.tgz",
        ):
            lock = make_lock()
            lock["packages"]["node_modules/left-pad"]["resolved"] = resolved
            self.assertIn(self.code(lock), {"unsupported-source", "insecure-url"})

    def test_rejects_old_lock_missing_integrity_and_sha1(self) -> None:
        lock = make_lock()
        lock["lockfileVersion"] = 1
        self.assertEqual(self.code(lock), "lock-version")
        lock = make_lock()
        del lock["packages"]["node_modules/left-pad"]["integrity"]
        self.assertEqual(self.code(lock), "bad-integrity")
        lock = make_lock()
        lock["packages"]["node_modules/left-pad"]["integrity"] = sri(b"x", "sha1")
        self.assertEqual(self.code(lock), "weak-integrity")

    def test_rejects_invalid_json(self) -> None:
        sources = {
            "package_json": b"{",
            "package_lock": b"{}",
            "lock_name": "package-lock.json",
        }
        with self.assertRaises(deps.ResolverError):
            deps.analyze_lock(sources, deps.DEFAULT_LIMITS)


class RequestTests(unittest.TestCase):
    def code(self, **over) -> str:
        with self.assertRaises(deps.ResolverError) as caught:
            deps.validate_request(request(**over))
        return caught.exception.code

    def test_valid_normalizes(self) -> None:
        normal = deps.validate_request(request())
        self.assertEqual(normal["_flags"]["omit"], ["dev"])
        self.assertTrue(normal["replay"])

    def test_pnpm_and_yarn_fail_closed(self) -> None:
        for name in ("pnpm", "yarn"):
            self.assertEqual(
                self.code(manager={"name": name, "version": "9.0.0"}),
                "unsupported-manager",
            )

    def test_command_and_flag_allowlist(self) -> None:
        self.assertEqual(self.code(command="install"), "unsupported-command")
        for flag in (
            "--force",
            "--registry=https://evil.example",
            "--omit",
            "--os=darwin",
            "--prefix=x",
            "--production",
        ):
            self.assertEqual(self.code(flags=[flag]), "unsupported-flag", flag)
        for flag in ("--workspaces", "-w"):
            self.assertEqual(self.code(flags=[flag]), "workspaces")
        self.assertEqual(self.code(flags=["--omit=everything"]), "unsupported-flag")

    def test_base_platform_env_and_limits(self) -> None:
        self.assertEqual(self.code(base="node:20"), "invalid-base")
        self.assertEqual(self.code(platform="linux/arm64"), "base-platform")
        self.assertEqual(self.code(env={"NPM_TOKEN": "x"}), "invalid-request")
        self.assertEqual(
            self.code(manager={"name": "npm", "version": "^10"}), "invalid-request"
        )
        self.assertEqual(self.code(limits={"nope": 1}), "invalid-request")
        self.assertEqual(self.code(timeout_seconds=10**6), "invalid-request")
        self.assertEqual(self.code(unknown=1), "invalid-request")

    def test_source_choice_is_exclusive(self) -> None:
        self.assertEqual(self.code(source_path="/srv/p"), "invalid-request")
        req = request(source_path="/srv/p/../x")
        del req["files"]
        with self.assertRaises(deps.ResolverError):
            deps.validate_request(req)

    def test_source_path_loading_is_readonly_and_rejects_npmrc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "web"
            project.mkdir()
            (project / "package.json").write_text(json.dumps(MANIFEST))
            (project / "package-lock.json").write_text(json.dumps(make_lock()))
            req = request(source_path=tmp, project_dir="web")
            del req["files"]
            normal = deps.validate_request(req)
            sources = deps.load_sources(normal)
            self.assertEqual(sources["lock_name"], "package-lock.json")
            self.assertEqual(
                deps.source_digests(sources)["package-lock.json"],
                sha((project / "package-lock.json").read_bytes()),
            )
            (project / ".npmrc").write_text("registry=https://private.example/\n")
            with self.assertRaises(deps.ResolverError) as caught:
                deps.load_sources(normal)
            self.assertEqual(caught.exception.code, "project-config")

    def test_gate_requires_remote_author(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(deps.ResolverError) as caught:
                deps.resolve(request(), container_factory=FakeContainer)
            self.assertEqual(caught.exception.code, "not-remote-author")
            with self.assertRaises(deps.ResolverError):
                deps.Container(BASE).__enter__()


class ResolveTests(unittest.TestCase):
    def test_prepared_image_is_exact_and_provenance_preserved(self):
        prepared = {
            "id": "sha256:" + "b" * 64,
            "base": BASE,
            "apt_request_sha256": "c" * 64,
        }
        lock = make_lock()
        with remote(hidden=hidden_for(lock, omit_dev=True)):
            result = resolve(request(lock=lock, prepared_image=prepared))
        self.assertEqual(FakeContainer.instances[-1].base, prepared["id"])
        self.assertEqual(result["package_request"]["base"], BASE)
        self.assertEqual(result["package_request"]["prepared_image"], prepared)
        with (
            remote(),
            mock.patch.object(
                FakeContainer, "image_id", return_value="sha256:" + "d" * 64
            ),
        ):
            reason(self, request(prepared_image=prepared), "prepared-image-mismatch")

    def test_prepared_image_rejects_tag_or_wrong_original_base(self):
        prepared = {"id": "ubuntu:latest", "base": BASE, "apt_request_sha256": "c" * 64}
        with self.assertRaises(deps.ResolverError):
            deps.validate_request(request(prepared_image=prepared))
        prepared.update(id="sha256:" + "b" * 64, base="other@sha256:" + "d" * 64)
        with self.assertRaises(deps.ResolverError):
            deps.validate_request(request(prepared_image=prepared))

    def test_success_preserves_lock_versions_and_hashes(self) -> None:
        lock = make_lock()
        with remote(hidden=hidden_for(lock, omit_dev=True)):
            fetched: list[str] = []
            result = resolve(request(lock=lock), fetch=fake_fetch(fetched))
        names = {i["name"]: i for i in result["inventory"]}
        self.assertEqual(
            set(names), {"left-pad", "@scope/lib"}
        )  # dev + darwin-only skipped
        self.assertEqual(
            sorted(fetched),
            sorted(
                url_for(n, v)
                for n, v in (("left-pad", "1.3.0"), ("@scope/lib", "2.0.0"))
            ),
        )
        for name, item in names.items():
            self.assertEqual(item["sha256"], sha(TARBALLS[name]))
            self.assertEqual(item["integrity"], sri(TARBALLS[name]))
            self.assertEqual(item["size"], len(TARBALLS[name]))
            self.assertTrue(
                item["url"].startswith("https://registry.npmjs.org/")
            )  # original public URL
            self.assertNotIn("?", item["url"])
        self.assertEqual(names["left-pad"]["version"], "1.3.0")
        artifacts = result["lock"]["artifacts"]
        self.assertEqual(
            {a["sha256"] for a in artifacts}, {sha(TARBALLS[n]) for n in names}
        )
        self.assertTrue(
            all(
                a["path"].startswith("npm-tarballs/") and a["path"].endswith(".tgz")
                for a in artifacts
            )
        )
        self.assertNotIn("local_file", json.dumps(result))
        evidence = result["evidence"]
        self.assertEqual(evidence["installed_packages"], 2)
        self.assertEqual(evidence["bundled_packages"], 1)
        self.assertEqual(evidence["skipped"]["platform_restricted"], ["fsevents@2.3.3"])
        self.assertEqual(evidence["skipped"]["dev_only"], 1)
        self.assertEqual(evidence["tools"]["npm"], NPM)
        self.assertTrue(evidence["lock_unchanged"])

    def test_container_commands_are_faithful_and_fetch_only_ignores_scripts(
        self,
    ) -> None:
        lock = make_lock()
        with remote(hidden=hidden_for(lock, omit_dev=True)):
            result = resolve(request(lock=lock))
        container = FakeContainer.instances[0]
        fetch_ci, replay_ci = [c for c in container.calls if c[:2] == ["npm", "ci"]]
        self.assertIn("--ignore-scripts", fetch_ci)
        self.assertNotIn("--offline", fetch_ci)
        self.assertIn("--omit=dev", fetch_ci)  # original flag preserved
        self.assertNotIn("--legacy-peer-deps", fetch_ci)  # nothing invented
        self.assertIn("--offline", replay_ci)
        self.assertTrue(container.disconnected)
        for call in container.calls:  # never upgrades or regenerates
            self.assertNotIn("update", call)
            self.assertNotIn("--package-lock-only", call)
            self.assertNotEqual(call[:2], ["npm", "install"])
        self.assertEqual(
            next(e for e in container.envs if e)["npm_config_userconfig"],
            "/nonexistent/mango-npmrc",
        )
        config_env = next(e for e in container.envs if e)
        self.assertNotEqual(
            config_env["npm_config_userconfig"], config_env["npm_config_globalconfig"]
        )
        install = result["offline"]["install"]["command"]
        self.assertNotIn(
            "--ignore-scripts", install
        )  # translated build runs lifecycle offline
        self.assertEqual(install[:2], ["npm", "ci"])
        self.assertIn("--omit=dev", install)
        self.assertIn("--offline", install)
        self.assertTrue(result["offline"]["verified_by_replay"])
        self.assertFalse(result["offline"]["verified_by_offline_build"])
        self.assertEqual(
            [f for f in container.files if f.endswith(".tgz")].count(
                f"{deps.TARBALLS}/0.tgz"
            ),
            1,
        )
        kinds = {u["kind"] for u in result["unresolved"]}
        self.assertEqual(
            kinds, {"lifecycle-scripts-not-run-in-fetch", "non-registry-network"}
        )

    def test_devdependencies_kept_without_omit(self) -> None:
        lock = make_lock()
        with remote(hidden=hidden_for(lock, omit_dev=False)):
            result = resolve(request(lock=lock, flags=["--no-audit"]))
        self.assertIn("typescript", {i["name"] for i in result["inventory"]})

    def test_node_env_production_means_dev_omitted(self) -> None:
        lock = make_lock()
        with remote(
            hidden=hidden_for(lock, omit_dev=False)
        ):  # npm wrongly installed dev
            reason(
                self,
                request(lock=lock, flags=[], env={"NODE_ENV": "production"}),
                "lock-drift",
            )

    def test_dev_hash_mismatch_is_rejected_before_inventory(self) -> None:
        lock = make_lock()
        with remote(hidden=hidden_for(lock, omit_dev=True)):
            reason(
                self,
                request(lock=lock),
                "integrity-mismatch",
                fetch=fake_fetch(corrupt={"left-pad"}),
            )

    def test_lock_mutation_in_container_detected(self) -> None:
        lock = make_lock()
        with remote(hidden=hidden_for(lock, omit_dev=True), mutate_lock_on_ci=True):
            reason(self, request(lock=lock), "lock-mutated")

    def test_source_path_mutation_detected(self) -> None:
        lock = make_lock()
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "package.json").write_text(json.dumps(MANIFEST))
            lock_path = project / "package-lock.json"
            lock_path.write_text(json.dumps(lock))
            req = request(lock=lock, source_path=tmp)
            del req["files"]

            def mutating_fetch(url, destination, max_bytes):
                lock_path.write_text(json.dumps(lock) + "\n")
                fake_fetch()(url, destination, max_bytes)

            with remote(hidden=hidden_for(lock, omit_dev=True)):
                reason(self, req, "lock-mutated", fetch=mutating_fetch)

    def test_installed_version_drift_rejected(self) -> None:
        lock = make_lock()
        hidden = hidden_for(lock, omit_dev=True)
        hidden["packages"]["node_modules/left-pad"]["version"] = (
            "1.3.1"  # silent upgrade
        )
        with remote(hidden=hidden):
            reason(self, request(lock=lock), "lock-drift")
        hidden = hidden_for(lock, omit_dev=True)
        hidden["packages"]["node_modules/extra"] = entry("left-pad", "1.3.0")
        with remote(hidden=hidden):
            reason(self, request(lock=lock), "lock-drift")
        hidden = hidden_for(lock, omit_dev=True)
        hidden["packages"]["node_modules/left-pad"]["resolved"] = (
            "https://evil.example/x.tgz"
        )
        with remote(hidden=hidden):
            reason(self, request(lock=lock), "lock-drift")

    def test_missing_hidden_lock_fails_closed(self) -> None:
        with remote(hidden={}):
            reason(self, request(), "hidden-lock")

    def test_manager_and_platform_checks(self) -> None:
        lock = make_lock()
        hidden = hidden_for(lock, omit_dev=True)
        with remote(hidden=hidden, npm_version="10.9.0"):
            reason(self, request(lock=lock), "manager-version-mismatch")
        with remote(hidden=hidden, platform="linux/arm64"):
            reason(self, request(lock=lock), "base-platform")
        with remote(hidden=hidden, node_platform="linux/arm64"):
            reason(self, request(lock=lock), "base-platform")
        with remote(hidden=hidden, registry="https://private.example/"):
            reason(self, request(lock=lock), "registry-config")

    def test_unsupported_lock_never_reaches_container(self) -> None:
        lock = make_lock()
        lock["packages"][""]["workspaces"] = ["a"]
        with remote(hidden={}):
            reason(self, request(lock=lock), "workspaces")
        self.assertEqual(FakeContainer.instances, [])

    def test_replay_failure_is_recorded_not_claimed_verified(self) -> None:
        lock = make_lock()
        with remote(hidden=hidden_for(lock, omit_dev=True), fail_cache_add=True):
            result = resolve(request(lock=lock))
        self.assertFalse(result["offline"]["verified_by_replay"])
        self.assertEqual(
            result["offline"]["replay_error"]["reason_code"], "offline-cache-miss"
        )
        self.assertEqual(
            len(result["inventory"]), 2
        )  # byte-verified inventory still valid

    def test_replay_tree_mismatch_not_verified(self) -> None:
        lock = make_lock()
        replay = hidden_for(lock, omit_dev=True)
        del replay["packages"]["node_modules/@scope/lib"]
        with remote(hidden=hidden_for(lock, omit_dev=True)):
            FakeContainer.hidden_replay = replay
            result = resolve(request(lock=lock))
        self.assertFalse(result["offline"]["verified_by_replay"])
        self.assertEqual(
            result["offline"]["replay_error"]["reason_code"], "replay-mismatch"
        )

    def test_replay_can_be_disabled_and_is_then_unverified(self) -> None:
        lock = make_lock()
        with remote(hidden=hidden_for(lock, omit_dev=True)):
            result = resolve(request(lock=lock, replay=False))
        self.assertFalse(result["offline"]["verified_by_replay"])
        self.assertFalse(FakeContainer.instances[0].disconnected)

    def test_duplicate_url_downloaded_once_and_conflict_rejected(self) -> None:
        lock = make_lock()
        lock["packages"]["node_modules/z/node_modules/left-pad"] = dict(
            lock["packages"]["node_modules/left-pad"]
        )
        hidden = hidden_for(lock, omit_dev=True)
        fetched: list[str] = []
        with remote(hidden=hidden):
            result = resolve(request(lock=lock), fetch=fake_fetch(fetched))
        self.assertEqual(fetched.count(url_for("left-pad", "1.3.0")), 1)
        pad = next(i for i in result["inventory"] if i["name"] == "left-pad")
        self.assertEqual(len(pad["lock_keys"]), 2)
        lock["packages"]["node_modules/z/node_modules/left-pad"]["integrity"] = sri(
            b"different"
        )
        with remote(hidden=hidden_for(lock, omit_dev=True)):
            reason(self, request(lock=lock), "lock-drift")

    def test_size_limit(self) -> None:
        lock = make_lock()
        with remote(hidden=hidden_for(lock, omit_dev=True)):
            reason(
                self,
                request(lock=lock, limits={"max_tarball_bytes": 4}),
                "limit-exceeded",
            )

    def test_tarball_path_safe_and_unique(self) -> None:
        path = deps.tarball_path(
            "npm-tarballs/k", "@scope/we ird", "1.0.0+b", "ab" * 32
        )
        self.assertEqual(path, "npm-tarballs/k/_scope_we_ird-1.0.0+b-abababababab.tgz")
        self.assertNotIn("..", path)


class DockerCommandTests(unittest.TestCase):
    def test_container_bounds_and_command_shape(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            mock.patch.dict(os.environ, {"MANGO_REMOTE_AUTHOR": "1"}),
            mock.patch.object(subprocess, "run", return_value=completed) as run,
        ):
            with deps.Container(BASE) as container:
                container.exec(
                    ["npm", "--version"], env={"A": "b"}, workdir="/w", timeout=5
                )
        first = run.call_args_list[0].args[0]
        pairs = dict(itertools.pairwise(first))
        self.assertEqual(pairs["--cpus"], "1.5")
        self.assertEqual(pairs["--memory"], "4g")
        self.assertEqual(pairs["--platform"], "linux/amd64")
        self.assertIn("--cap-drop", first)
        self.assertEqual(first[-3:], ["sleep", BASE, "infinity"])
        exec_call = run.call_args_list[1]
        self.assertEqual(exec_call.args[0][:3], ["docker", "exec", "-e"])
        self.assertEqual(exec_call.kwargs["timeout"], 5)
        self.assertEqual(run.call_args_list[-1].args[0][:3], ["docker", "rm", "-f"])

    def test_timeout_and_failure_are_redacted_resolver_errors(self) -> None:
        with (
            mock.patch.object(
                subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 1)
            ),
            self.assertRaises(deps.ResolverError) as caught,
        ):
            deps.run(["docker", "exec", "x", "npm", "ci"], timeout=1)
        self.assertEqual(caught.exception.code, "timeout")
        failed = subprocess.CompletedProcess(
            [],
            1,
            "",
            "npm ERR! code EINTEGRITY https://u:p@registry.npmjs.org/a.tgz?sig=secret",
        )
        with mock.patch.object(subprocess, "run", return_value=failed):
            with self.assertRaises(deps.ResolverError) as caught:
                deps.run(["docker", "exec", "x", "npm", "ci"])
        self.assertEqual(caught.exception.code, "integrity-mismatch")
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("u:p", str(caught.exception))

    def test_classify(self) -> None:
        self.assertEqual(
            deps.classify_npm_failure("npm ERR! code ERESOLVE")[0], "peer-conflict"
        )
        self.assertEqual(deps.classify_npm_failure("whatever")[0], "npm-failed")


INSTALL_MANIFEST = {
    "name": "app",
    "version": "1.0.0",
    "dependencies": {"left-pad": "1.3.0"},
    "devDependencies": {"typescript": "5.4.5"},
    "scripts": {"build": "tsc"},
}


def generated_lock_bytes(**root) -> bytes:
    lock = make_lock()
    lock["packages"][""] = {
        "name": "app",
        "version": "1.0.0",
        "dependencies": {"left-pad": "1.3.0"},
        "devDependencies": {"typescript": "5.4.5"},
        **root,
    }
    return json.dumps(lock, indent=2).encode() + b"\n"


def install_request(**over) -> dict:
    req = {
        "id": "vite",
        "base": BASE,
        "manager": {"name": "npm", "version": NPM},
        "command": "install",
        "flags": [],
        "files": {"package_json": json.dumps(INSTALL_MANIFEST)},
    }
    req.update(over)
    return req


def install_hidden() -> dict:
    return hidden_for(json.loads(generated_lock_bytes()), omit_dev=False)


class LocklessInstallTests(unittest.TestCase):
    def test_generated_lock_is_captured_and_selection_matches(self) -> None:
        generated = generated_lock_bytes()
        with remote(hidden=install_hidden(), generated_lock=generated):
            fetched: list[str] = []
            result = resolve(install_request(), fetch=fake_fetch(fetched))
        container = FakeContainer.instances[0]
        installs = [c for c in container.calls if c[:2] == ["npm", "install"]]
        self.assertEqual(len(installs), 2)  # fetch + replay, never `npm ci`
        fetch_cmd, replay_cmd = installs
        self.assertEqual(
            [a for a in fetch_cmd if a.startswith("--") and a != "--cache"],
            ["--ignore-scripts", "--no-audit", "--no-fund"],
        )
        self.assertNotIn("--offline", fetch_cmd)
        self.assertIn("--offline", replay_cmd)
        self.assertIn("--ignore-scripts", replay_cmd)
        self.assertTrue(container.disconnected)
        for call in container.calls:
            self.assertNotEqual(call[:2], ["npm", "ci"])
            self.assertNotIn("--package-lock-only", call)
            self.assertNotIn("update", call)
            self.assertNotIn("latest", " ".join(call))
        # the original package.json is what npm saw, and no lock was supplied
        self.assertEqual(
            container.files[f"{deps.PROJECT}/package.json"],
            json.dumps(INSTALL_MANIFEST).encode(),
        )
        # exact selection: the inventory is exactly what npm's own lock selected
        lock = json.loads(generated)
        want = {
            (k.rsplit("node_modules/", 1)[-1], e["version"])
            for k, e in lock["packages"].items()
            if k and e.get("resolved") and e.get("os") != ["darwin"]
        }
        self.assertEqual({(i["name"], i["version"]) for i in result["inventory"]}, want)
        self.assertEqual(sorted(fetched), sorted(i["url"] for i in result["inventory"]))
        # the generated lock is output byte-for-byte as hashed metadata
        meta = result["generated_lock"]
        self.assertEqual(base64.b64decode(meta["content_base64"]), generated)
        self.assertEqual(meta["sha256"], sha(generated))
        self.assertEqual(meta["size"], len(generated))
        self.assertEqual(meta["lock_version"], 3)
        self.assertIn("npm install --offline", meta["consumption"])
        install = result["offline"]["install"]["command"]
        self.assertEqual(install[:2], ["npm", "install"])
        self.assertIn("--offline", install)
        self.assertNotIn("--ignore-scripts", install)
        self.assertEqual(result["package_request"]["command"], "install")
        evidence = result["evidence"]
        self.assertEqual(
            evidence["source_sha256"],
            {"package.json": sha(json.dumps(INSTALL_MANIFEST).encode())},
        )
        self.assertEqual(evidence["lock_origin"], "generated")
        self.assertNotIn("lock_unchanged", evidence)
        # lifecycle packages stay visible and the original build is not claimed
        kinds = {u["kind"]: u for u in result["unresolved"]}
        self.assertEqual(
            [
                p["name"]
                for p in kinds["lifecycle-scripts-not-run-in-fetch"]["packages"]
            ],
            ["left-pad"],
        )
        self.assertIn("original-build-not-run", kinds)
        self.assertTrue(result["offline"]["verified_by_replay"])
        deps.require_verified({**result, "status": "completed"})

    def test_package_json_mutation_by_install_fails(self) -> None:
        with remote(
            hidden=install_hidden(),
            generated_lock=generated_lock_bytes(),
            mutate_manifest_on_install=True,
        ):
            reason(self, install_request(), "lock-mutated")

    def test_lock_not_generated_fails_closed(self) -> None:
        with remote(hidden=install_hidden(), generated_lock=None):
            reason(self, install_request(), "lock-not-generated")

    def test_generated_lock_is_validated(self) -> None:
        old = json.loads(generated_lock_bytes())
        old["lockfileVersion"] = 1
        with remote(hidden=install_hidden(), generated_lock=json.dumps(old).encode()):
            reason(self, install_request(), "lock-version")
        with remote(
            hidden=install_hidden(),
            generated_lock=generated_lock_bytes(dependencies={"left-pad": "9.9.9"}),
        ):
            reason(self, install_request(), "lock-root")
        bad = json.loads(generated_lock_bytes())
        bad["packages"]["node_modules/left-pad"]["resolved"] = "git+ssh://x/y.git#abc"
        with remote(hidden=install_hidden(), generated_lock=json.dumps(bad).encode()):
            reason(self, install_request(), "unsupported-source")

    def test_installed_tree_must_match_generated_lock(self) -> None:
        hidden = install_hidden()
        hidden["packages"]["node_modules/left-pad"]["version"] = "1.3.1"
        with remote(hidden=hidden, generated_lock=generated_lock_bytes()):
            reason(self, install_request(), "lock-drift")

    def test_replay_lock_mutation_is_unverified_and_gated(self) -> None:
        generated = generated_lock_bytes()
        for attrs in (
            {"replay_lock": generated + b" "},
            {"drop_lock_on_replay": True},
        ):
            with remote(hidden=install_hidden(), generated_lock=generated, **attrs):
                result = resolve(install_request())
            result["status"] = "completed"  # what the CLI records today
            self.assertFalse(result["offline"]["verified_by_replay"])
            self.assertFalse(result["verified_by_replay"])
            self.assertEqual(
                result["offline"]["replay_error"]["reason_code"], "replay-mismatch"
            )
            with self.assertRaises(deps.ResolverError) as caught:
                deps.require_verified(result)
            self.assertEqual(caught.exception.code, "not-verified")

    def test_replay_tree_mismatch_is_unverified(self) -> None:
        replay = install_hidden()
        del replay["packages"]["node_modules/@scope/lib"]
        with remote(hidden=install_hidden(), generated_lock=generated_lock_bytes()):
            FakeContainer.hidden_replay = replay
            result = resolve(install_request())
        self.assertFalse(result["offline"]["verified_by_replay"])
        with self.assertRaises(deps.ResolverError):
            deps.require_verified({**result, "status": "completed"})

    def test_unreplayed_or_failed_results_are_not_verified(self) -> None:
        with remote(hidden=install_hidden(), generated_lock=generated_lock_bytes()):
            result = resolve(install_request(replay=False))
        self.assertFalse(result["verified_by_replay"])
        for bad in (
            {**result, "status": "completed"},
            {"status": "failed"},
            None,
            {"status": "completed", "offline": {"verified_by_replay": True}},
        ):
            with self.assertRaises(deps.ResolverError):
                deps.require_verified(bad)

    def test_existing_lock_install_stays_rejected(self) -> None:
        for req in (
            install_request(
                files={
                    "package_json": json.dumps(INSTALL_MANIFEST),
                    "package_lock": json.dumps(make_lock()),
                }
            ),
            request(command="install"),
        ):
            with self.assertRaises(deps.ResolverError) as caught:
                deps.validate_request(req)
            self.assertEqual(caught.exception.code, "unsupported-command")
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "package.json").write_text(json.dumps(INSTALL_MANIFEST))
            Path(tmp, "npm-shrinkwrap.json").write_text("{}")
            req = install_request(source_path=tmp)
            del req["files"]
            with self.assertRaises(deps.ResolverError) as caught:
                deps.load_sources(deps.validate_request(req))
            self.assertEqual(caught.exception.code, "unsupported-command")

    def test_ci_still_requires_a_lock(self) -> None:
        req = request()
        del req["files"]["package_lock"]
        with self.assertRaises(deps.ResolverError) as caught:
            deps.validate_request(req)
        self.assertEqual(caught.exception.code, "invalid-request")

    def test_global_and_arbitrary_flags_rejected(self) -> None:
        for flags in (
            ["-g"],
            ["--global"],
            ["--save"],
            ["--force"],
            ["--package-lock-only"],
            ["--registry=https://evil.example"],
            ["--prefix=/x"],
        ):
            with self.assertRaises(deps.ResolverError, msg=str(flags)) as caught:
                deps.validate_request(install_request(flags=flags))
            self.assertEqual(caught.exception.code, "unsupported-flag")
        with self.assertRaises(deps.ResolverError):
            deps.validate_request(
                install_request(files={"package_json": "{}", "x": ""})
            )

    def test_workspaces_and_unsupported_never_reach_container(self) -> None:
        manifest = {**INSTALL_MANIFEST, "workspaces": ["packages/*"]}
        with remote(hidden={}):
            reason(
                self,
                install_request(files={"package_json": json.dumps(manifest)}),
                "workspaces",
            )
            reason(
                self,
                install_request(manager={"name": "yarn", "version": "1.22.0"}),
                "unsupported-manager",
            )
        self.assertEqual(FakeContainer.instances, [])

    def test_source_path_lockless_digests_and_lock_appearing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "package.json").write_text(json.dumps(INSTALL_MANIFEST))
            req = install_request(source_path=tmp)
            del req["files"]
            normal = deps.validate_request(req)
            self.assertEqual(
                list(deps.source_digests(deps.load_sources(normal))), ["package.json"]
            )

            def appearing(url, destination, max_bytes):
                Path(tmp, "package-lock.json").write_text("{}")
                fake_fetch()(url, destination, max_bytes)

            with remote(hidden=install_hidden(), generated_lock=generated_lock_bytes()):
                reason(self, req, "lock-mutated", fetch=appearing)

    def test_cli_require_verified_exit_code(self) -> None:
        for attrs, expected in (({}, 0), ({"replay_lock": b"changed"}, 1)):
            with (
                tempfile.TemporaryDirectory() as tmp,
                remote(
                    hidden=install_hidden(),
                    generated_lock=generated_lock_bytes(),
                    **attrs,
                ),
            ):
                requests, out = Path(tmp, "r.json"), Path(tmp, "o.json")
                requests.write_text(json.dumps({"requests": [install_request()]}))
                real = deps.resolve
                with (
                    mock.patch.object(
                        deps,
                        "resolve",
                        side_effect=lambda r: real(
                            r, fetch=fake_fetch(), container_factory=FakeContainer
                        ),
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    plain = deps.main([str(requests), "--output", str(out)])
                    self.assertEqual(plain, 0)  # "completed" alone says nothing
                    strict = deps.main(
                        [str(requests), "--output", str(out), "--require-verified"]
                    )
                self.assertEqual(strict, expected)


class CliTests(unittest.TestCase):
    def test_checkpoint_resume_and_failure_record(self) -> None:
        lock = make_lock()
        good, bad = (
            request(lock=lock),
            request(id="bad", manager={"name": "pnpm", "version": "9.0.0"}),
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            remote(hidden=hidden_for(lock, omit_dev=True)),
        ):
            requests = Path(tmp) / "r.json"
            out = Path(tmp) / "out.json"
            requests.write_text(json.dumps({"requests": [good, bad]}))
            real = deps.resolve
            with (
                mock.patch.object(
                    deps,
                    "resolve",
                    side_effect=lambda r: real(
                        r, fetch=fake_fetch(), container_factory=FakeContainer
                    ),
                ) as spy,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = deps.main([str(requests), "--output", str(out)])
                self.assertEqual(code, 1)
                data = json.loads(out.read_text())["results"]
                self.assertEqual(data["app"]["status"], "completed", data["app"])
                self.assertEqual(data["bad"]["reason_code"], "unsupported-manager")
                calls = spy.call_count
                deps.main([str(requests), "--output", str(out)])
                self.assertEqual(
                    spy.call_count, calls
                )  # unchanged requests are not rerun


if __name__ == "__main__":
    unittest.main()
