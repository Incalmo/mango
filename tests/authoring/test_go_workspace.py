"""Mocked tests for go_workspace; no Docker, network or Go is ever invoked."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import posixpath
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from mango.authoring import go_lock as lock
from mango.authoring import go_workspace as gw

BASE = "golang@sha256:" + "a" * 64
PROXY = "https://proxy.golang.org"
ROOT = lock.MODCACHE
P = gw.PROJECT


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def h1_of(files: dict[str, bytes]) -> str:
    text = "".join(f"{sha(files[n])}  {n}\n" for n in sorted(files))
    return "h1:" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


class Mod:
    """A synthetic module version: zip + .mod + .info bytes and go.sum lines."""

    def __init__(self, path, version, source=b"package m\n"):
        self.path, self.version = path, version
        self.gomod = f"module {path}\n".encode()
        self.files = {
            f"{path}@{version}/go.mod": self.gomod,
            f"{path}@{version}/a.go": source,
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, data in self.files.items():
                archive.writestr(zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)), data)
        self.zip = buffer.getvalue()
        self.info = json.dumps({"Version": version}).encode()
        self.h1_zip = h1_of(self.files)
        self.h1_mod = h1_of({"go.mod": self.gomod})

    def sum_lines(self):
        return (
            f"{self.path} {self.version} {self.h1_zip}\n"
            f"{self.path} {self.version}/go.mod {self.h1_mod}\n"
        )

    def ext_bytes(self):
        return {"zip": self.zip, "mod": self.gomod, "info": self.info}


def cache_path(mod, ext):
    return f"{ROOT}/{lock.cache_relpath(mod.path, mod.version, ext)}"


class FakeContainer:
    """Plays docker + go for one request and records every call."""

    def __init__(self, selected, **opts):
        self.selected, self.opts = selected, opts
        self.files, self.calls = {}, []

    def __call__(self, base):
        self.base = base
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def image_platform(self):
        return "linux/amd64"

    def copy_in(self, source, destination):
        data = Path(source).read_bytes()
        if self.opts.get("corrupt_copy") and destination.endswith("go.mod"):
            data += b"// x\n"
        self.files[destination] = data

    def calls_of(self, *prefix):
        return [c for c in self.calls if c[0][: len(prefix)] == list(prefix)]

    def exec(self, argv, env=None, workdir=None, check=True):
        argv = list(argv)
        self.calls.append((argv, dict(env or {}), workdir))

        def out(stdout="", stderr="", rc=0):
            return subprocess.CompletedProcess(argv, rc, stdout, stderr)

        if argv[:3] == ["go", "env", "-json"]:
            if "GOVERSION" in argv:
                return out(
                    json.dumps(
                        {
                            "GOVERSION": self.opts.get("goversion", "go1.26.10"),
                            "GOOS": "linux",
                            "GOARCH": "amd64",
                        }
                    )
                )
            return out(
                json.dumps(
                    {
                        "GOWORK": self.opts.get("reported_gowork", env["GOWORK"]),
                        "GOMOD": f"{workdir}/go.mod",
                        "GOFLAGS": "",
                    }
                )
            )
        if argv[:2] == ["go", "version"]:
            return out("go version go1.26.10 linux/amd64\n")
        if argv[0] == "mkdir":
            return out()
        if argv[:4] == ["go", "mod", "edit", "-json"]:
            parsed = lock.parse_go_mod(self.files[f"{workdir}/go.mod"])
            if self.opts.get("edit_lie"):
                parsed["replaces"] = parsed["replaces"][:-1]
            return out(json.dumps(self._edit(parsed["module"], parsed["replaces"])))
        if argv[:4] == ["go", "work", "edit", "-json"]:
            parsed = gw.parse_go_work(self.files[env["GOWORK"]])
            doc = self._edit(None, parsed["replaces"])
            doc["Use"] = [{"DiskPath": u} for u in parsed["use"]]
            return out(json.dumps(doc))
        if argv[:3] == ["go", "mod", "download"]:
            self.download_workdir = workdir
            items = []
            for mod in self.selected:
                for ext, data in mod.ext_bytes().items():
                    self.files[cache_path(mod, ext)] = data
                self.files[cache_path(mod, "ziphash")] = mod.h1_zip.encode()
                items.append(
                    {
                        "Path": mod.path,
                        "Version": mod.version,
                        "Info": cache_path(mod, "info"),
                        "GoMod": cache_path(mod, "mod"),
                        "Zip": cache_path(mod, "zip"),
                        "Dir": f"{ROOT}/{mod.path}@{mod.version}",
                        "Sum": mod.h1_zip,
                        "GoModSum": mod.h1_mod,
                    }
                )
            items += self.opts.get("local_items", [])
            if self.opts.get("mutate_go_sum"):
                self.files[f"{workdir}/go.sum"] = b"extra\n"
            if self.opts.get("create_work_sum"):
                self.files[f"{P}/go.work.sum"] = b""
            return out("\n".join(json.dumps(i, indent=1) for i in items))
        if argv[0] == "find":
            names = {a for a in argv if a.startswith("go.")}
            return out(
                "".join(
                    f"{p}\n"
                    for p in sorted(self.files)
                    if p.startswith(P + "/") and posixpath.basename(p) in names
                )
            )
        if argv[0] == "sha256sum":
            return out(
                "".join(
                    f"{sha(self.files[p])}  {p}\n" for p in argv[2:] if p in self.files
                )
            )
        if argv[0] == "cat":
            return out(self.files[argv[2]].decode())
        raise AssertionError(f"unexpected exec {argv}")

    @staticmethod
    def _edit(module, replaces):
        def pair(tokens):
            return {
                "Path": tokens[0],
                **({"Version": tokens[1]} if len(tokens) > 1 else {}),
            }

        doc = {
            "Replace": [
                {"Old": pair(r["old"].split()), "New": pair(r["new"])} for r in replaces
            ]
        }
        if module:
            doc["Module"] = {"Path": module}
        return doc


class Fetcher:
    def __init__(self, mods, tamper=None):
        self.served = {}
        for mod in mods:
            for ext, data in mod.ext_bytes().items():
                self.served[lock.proxy_url(PROXY, mod.path, mod.version, ext)] = data
        self.served.update(tamper or {})
        self.urls = []

    def __call__(self, url, destination, max_bytes):
        self.urls.append(url)
        if url not in self.served:
            raise lock.ResolverError(f"HTTP 404 fetching {url}", "module-unavailable")
        destination.write_bytes(self.served[url])
        return sha(self.served[url])


DEP = Mod("example.com/dep", "v1.0.0")
OTHER = Mod("example.com/other", "v1.2.0")

CADENCE_ROOT = (
    "module github.com/uber/cadence\n\ngo 1.21\n\nrequire example.com/dep v1.0.0\n"
)
CADENCE_SERVER = (
    "module github.com/uber/cadence/cmd/server\n\ngo 1.21\n\n"
    "replace github.com/uber/cadence => ../..\n"
    "replace github.com/uber/cadence/common/archiver/gcloud => ../../common/archiver/gcloud\n"
)
CADENCE_GCLOUD = (
    "module github.com/uber/cadence/common/archiver/gcloud\n\ngo 1.21\n\n"
    "replace (\n\tgithub.com/uber/cadence => ../../..\n)\n"
)
GO_WORK = "go 1.21\n\nuse (\n\t.\n\t./cmd/server\n\t./common/archiver/gcloud\n)\n"


def mod(directory, go_mod, go_sum=None):
    return {"dir": directory, "go_mod": go_mod, "go_sum": go_sum}


def cadence(**over):
    request = {
        "id": "cadence",
        "base": BASE,
        "goproxy": PROXY,
        "original": {"project_dir": "/src/cadence", "run_dir": ".", "goflags": ""},
        "modules": [
            mod(".", CADENCE_ROOT, DEP.sum_lines()),
            mod("cmd/server", CADENCE_SERVER, None),
            mod("common/archiver/gcloud", CADENCE_GCLOUD, ""),
        ],
        "workspace": {
            "mode": "go.work",
            "dir": ".",
            "go_work": GO_WORK,
            "go_work_sum": None,
        },
    }
    request.update(over)
    return request


def standalone(root_mod, children, root_sum="", **over):
    request = {
        "id": "argo",
        "base": BASE,
        "goproxy": PROXY,
        "original": {"project_dir": "/src/argo", "run_dir": ".", "goflags": ""},
        "modules": [mod(".", root_mod, root_sum)]
        + [mod(d, m, s) for d, m, s in children],
        "workspace": {"mode": "none"},
    }
    request.update(over)
    return request


LOCAL_ROOT = (
    "module example.com/app\n\ngo 1.21\n\nrequire example.com/dep v1.0.0\n"
    "replace example.com/app/sub => ./sub\n"
)
LOCAL_SUB = "module example.com/app/sub\n\ngo 1.21\n\nreplace example.com/app => ../\n"


@contextlib.contextmanager
def remote_env():
    with mock.patch.dict(os.environ, {"MANGO_REMOTE_AUTHOR": "1"}):
        yield


def run(request, selected=(DEP,), fetcher=None, **opts):
    container = FakeContainer(list(selected), **opts)
    fetcher = fetcher or Fetcher(list(selected))
    with remote_env(), mock.patch.object(gw.lock, "PROGRESS", lambda m: None):
        result = gw.resolve(request, fetch=fetcher, container_factory=container)
    return result, container


def fails(test, request, code, **opts):
    container = FakeContainer([DEP], **opts)
    with (
        remote_env(),
        mock.patch.object(gw.lock, "PROGRESS", lambda m: None),
        test.assertRaises(lock.ResolverError) as caught,
    ):
        gw.resolve(request, fetch=Fetcher([DEP]), container_factory=container)
    test.assertEqual(caught.exception.code, code, str(caught.exception))
    return container


class WorkspaceResolve(unittest.TestCase):
    def test_original_goflags_apply_during_authoring_and_replay(self):
        request = cadence()
        request["original"]["goflags"] = "-mod=readonly -tags=custom"
        result, container = run(request)
        _, env, _ = container.calls_of("go", "mod", "download")[0]
        self.assertEqual(env["GOFLAGS"], request["original"]["goflags"])
        self.assertEqual(
            result["offline"]["proxy_mode"]["env"]["GOFLAGS"], env["GOFLAGS"]
        )

    def test_cadence_workspace_preserves_layout_and_selection(self):
        result, container = run(cadence())
        # Original relative layout, byte-identical manifests, absent sums absent.
        self.assertEqual(
            container.files[f"{P}/cmd/server/go.mod"], CADENCE_SERVER.encode()
        )
        self.assertEqual(container.files[f"{P}/go.work"], GO_WORK.encode())
        self.assertNotIn(f"{P}/cmd/server/go.sum", container.files)
        self.assertEqual(container.files[f"{P}/common/archiver/gcloud/go.sum"], b"")
        self.assertNotIn(f"{P}/go.work.sum", container.files)
        # Go ran in the run dir with the ORIGINAL go.work, never GOWORK=off.
        _, env, workdir = container.calls_of("go", "mod", "download")[0]
        self.assertEqual((env["GOWORK"], workdir), (f"{P}/go.work", P))
        self.assertEqual(env["GOTOOLCHAIN"], "local")
        self.assertEqual(env["GOSUMDB"], "sum.golang.org")
        self.assertEqual(result["kind"], gw.KIND)
        self.assertFalse(result["application_compilation_proven"])
        self.assertTrue(result["authoring_only"])
        sel = result["evidence"]["selection"]
        self.assertEqual(sel["use"], [".", "cmd/server", "common/archiver/gcloud"])
        self.assertEqual(
            {(r["source"], r["target_dir"]) for r in sel["local_replacements"]},
            {
                ("cmd/server/go.mod", "."),
                ("cmd/server/go.mod", "common/archiver/gcloud"),
                ("common/archiver/gcloud/go.mod", "."),
            },
        )
        roles = {(m["path"], m["present"]) for m in result["evidence"]["manifests"]}
        self.assertIn(("go.work", True), roles)
        self.assertIn(("go.work.sum", False), roles)
        self.assertIn(("cmd/server/go.sum", False), roles)
        work = next(
            m for m in result["evidence"]["manifests"] if m["path"] == "go.work"
        )
        self.assertEqual(work["sha256"], sha(GO_WORK.encode()))
        # Lock stays consumable: go_lock's artifact shape, plus distinct receipt.
        self.assertEqual(result["lock"]["schema_version"], 1)
        self.assertEqual(len(result["lock"]["artifacts"]), 3)
        self.assertEqual(
            [m["module"] for m in result["evidence"]["modules"]], [DEP.path]
        )

    def test_receipt_has_no_author_host_paths_and_keeps_workspace_guidance(self):
        result, _ = run(
            cadence(
                original={
                    "project_dir": "/src/cadence",
                    "run_dir": "cmd/server",
                    "goflags": "-mod=readonly -tags=foo",
                }
            )
        )
        text = json.dumps(result)
        self.assertNotIn(P, text)
        self.assertNotIn("/tmp/mango-go-deps/project", text)
        offline = result["offline"]
        env = offline["proxy_mode"]["env"]
        self.assertEqual(env["GOWORK"], "${MANGO_GO_PROJECT}/go.work")
        self.assertEqual(env["GOFLAGS"], "-mod=readonly -tags=foo")
        self.assertEqual(env["GOTOOLCHAIN"], "local")
        self.assertEqual(offline["project"]["run_directory"], "/src/cadence/cmd/server")
        self.assertFalse(offline["verified_by_offline_build"])
        self.assertTrue(any("does not prove" in x for x in offline["limitations"]))

    def test_cycles_are_handled_by_identity(self):
        # root <-> sub replace each other; workspace-free.
        request = standalone(LOCAL_ROOT, [("sub", LOCAL_SUB, None)], DEP.sum_lines())
        result, container = run(request)
        self.assertEqual(len(result["evidence"]["selection"]["local_replacements"]), 2)
        _, env, _ = container.calls_of("go", "mod", "download")[0]
        self.assertEqual(env["GOWORK"], "off")  # only because no workspace declared
        self.assertEqual(result["offline"]["proxy_mode"]["env"]["GOWORK"], "off")
        self.assertIsNone(result["offline"]["project"]["go_work"])

    def test_standalone_sum_authority_is_run_dir_only(self):
        # The child's go.sum must not rescue a missing sum in the main module.
        request = standalone(
            LOCAL_ROOT, [("sub", LOCAL_SUB, DEP.sum_lines())], root_sum=""
        )
        fails(self, request, "go-sum-missing")

    def test_workspace_sums_merge_use_modules_and_work_sum_only(self):
        request = cadence()
        request["modules"][0] = mod(".", CADENCE_ROOT, "")
        request["workspace"]["go_work_sum"] = DEP.sum_lines()
        result, container = run(request)
        self.assertIn(f"{P}/go.work.sum", container.files)
        self.assertIn("go.work.sum", result["evidence"]["selection"]["sum_authorities"])

    def test_fixed_version_remote_replacement_still_supported(self):
        root = (
            "module example.com/app\n\ngo 1.21\n\nrequire example.com/old v1.0.0\n"
            "replace example.com/old v1.0.0 => example.com/other v1.2.0\n"
        )
        request = standalone(root, [], OTHER.sum_lines())
        result, _ = run(request, selected=[OTHER])
        self.assertEqual(result["evidence"]["modules"][0]["module"], OTHER.path)
        self.assertEqual(
            result["evidence"]["selection"]["remote_replacements"][0]["new"],
            "example.com/other v1.2.0",
        )

    def test_local_main_modules_in_download_output_are_identified_not_guessed(self):
        local = [
            {"Path": "github.com/uber/cadence", "Dir": P},
            {"Path": "github.com/uber/cadence/cmd/server", "Dir": f"{P}/cmd/server"},
        ]
        result, _ = run(cadence(), local_items=local)
        reported = result["evidence"]["selection"]["local_modules_reported_by_go"]
        self.assertEqual([m["dir"] for m in reported], [".", "cmd/server"])
        for bad in (
            {"Path": "github.com/unknown/mod", "Dir": f"{P}/x"},
            {"Path": "github.com/uber/cadence", "Dir": "/elsewhere"},
            {"Path": "github.com/uber/cadence"},
        ):
            fails(self, cadence(), "go-output-unsupported", local_items=[bad])


class Rejections(unittest.TestCase):
    def test_harbor_replace_to_dir_without_go_mod_fails_closed(self):
        harbor = (
            "module github.com/goharbor/harbor/src\n\ngo 1.26\n\n"
            "replace github.com/goharbor/harbor => ../\n"
        )
        request = standalone(harbor, [])
        request["original"]["run_dir"] = "src"
        request["modules"] = [mod("src", harbor, "")]
        fails(self, request, "go-local-missing")

    def test_bad_local_targets(self):
        cases = {
            "go-local-escape": "replace example.com/x => ../outside\n",
            "go-local-absolute": "replace example.com/x => /abs/path\n",
            "go-local-missing": "replace example.com/x => ./nowhere\n",
            "go-local-module-mismatch": "replace example.com/wrong => ./sub\n",
            "go-mod-replace": "replace example.com/x => ./sub v1.0.0\n",
        }
        for code, line in cases.items():
            with self.subTest(code):
                root = f"module example.com/app\n\ngo 1.21\n\n{line}"
                request = standalone(
                    root, [("sub", "module example.com/x/sub\n", None)]
                )
                fails(self, request, code)
        windows = standalone(
            "module example.com/app\n\nreplace example.com/x => ..\\x\n", []
        )
        fails(self, windows, "go-local-absolute")

    def test_unreferenced_conflicting_and_undeclared_modules(self):
        plain = "module example.com/app\n\ngo 1.21\n"
        fails(
            self,
            standalone(plain, [("extra", "module example.com/extra\n", None)]),
            "module-unreferenced",
        )
        request = cadence()
        request["modules"].append(mod("dup", "module github.com/uber/cadence\n", None))
        fails(self, request, "module-conflict")
        request = cadence()
        request["workspace"]["go_work"] = GO_WORK + "use ./missing\n"
        fails(self, request, "go-work-use-missing")
        request = cadence()
        request["workspace"]["go_work"] = GO_WORK + "use ../up\n"
        fails(self, request, "go-local-escape")
        request = cadence()
        request["workspace"]["go_work"] = GO_WORK + "use /abs\n"
        fails(self, request, "go-work-use")

    def test_workspace_run_dir_and_mode_validation(self):
        request = cadence()
        request["workspace"]["go_work"] = "go 1.21\n\nuse .\nuse ./cmd/server\n"
        request["original"]["run_dir"] = "common/archiver/gcloud"
        fails(self, request, "run-dir-not-in-workspace")
        request = cadence()
        request["workspace"]["mode"] = "auto"
        fails(self, request, "unsupported-mode")
        request = cadence()
        request["workspace"]["go_work"] = "go 1.21\nfrobnicate x\nuse .\n"
        fails(self, request, "invalid-go-work")

    def test_go_work_replace_is_validated_like_go_mod(self):
        request = cadence()
        request["workspace"]["go_work"] = GO_WORK + "replace example.com/x => ../esc\n"
        fails(self, request, "go-local-escape")

    def test_schema_strictness(self):
        for change, code in [
            (lambda r: r["modules"][0].pop("go_sum"), "invalid-request"),
            (lambda r: r.pop("workspace"), "invalid-request"),
            (lambda r: r["original"].update(project_dir="rel/dir"), "invalid-request"),
            (lambda r: r["original"].update(run_dir="missing"), "run-dir-missing"),
            (lambda r: r["original"].update(goflags="-mod=mod"), "unsupported-goflags"),
            (
                lambda r: r["original"].update(goflags="-modfile=x.mod"),
                "unsupported-goflags",
            ),
            (lambda r: r["modules"][0].update(dir="../x"), "invalid-request"),
            (lambda r: r.update(source_path="/x"), "invalid-request"),
            (lambda r: r.update(gosumdb="off"), "invalid-request"),
        ]:
            with self.subTest(code=code):
                request = cadence()
                change(request)
                with self.assertRaises(lock.ResolverError) as caught:
                    gw.validate_request(request)
                self.assertEqual(caught.exception.code, code)

    def test_gate(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(lock.ResolverError) as caught:
                gw.resolve(cadence(), container_factory=FakeContainer([]))
        self.assertEqual(caught.exception.code, "gate")


class Integrity(unittest.TestCase):
    def test_go_mutating_a_sum_or_creating_work_sum_fails(self):
        request = cadence()
        request["modules"][0] = mod(".", CADENCE_ROOT, DEP.sum_lines())
        fails(self, request, "go-mod-mutated", create_work_sum=True)
        standalone_request = standalone(
            LOCAL_ROOT, [("sub", LOCAL_SUB, None)], DEP.sum_lines()
        )
        fails(self, standalone_request, "go-mod-mutated", mutate_go_sum=True)

    def test_copy_corruption_is_detected(self):
        fails(self, cadence(), "source-mutated", corrupt_copy=True)

    def test_request_is_not_mutated(self):
        request = cadence()
        before = json.dumps(request, sort_keys=True)
        run(request)
        self.assertEqual(json.dumps(request, sort_keys=True), before)

    def test_checksum_mismatches(self):
        tampered = Fetcher(
            [DEP],
            tamper={lock.proxy_url(PROXY, DEP.path, DEP.version, "zip"): b"PK-bad"},
        )
        with remote_env(), self.assertRaises(lock.ResolverError) as caught:
            gw.resolve(
                cadence(),
                fetch=tampered,
                container_factory=FakeContainer([DEP]),
            )
        self.assertEqual(caught.exception.code, "proxy-bytes-mismatch")
        request = cadence()
        wrong = DEP.sum_lines().replace(DEP.h1_zip, "h1:" + "A" * 43 + "=")
        request["modules"][0] = mod(".", CADENCE_ROOT, wrong)
        fails(self, request, "go-sum-mismatch")

    def test_go_parse_must_agree_with_resolver_parse(self):
        fails(self, cadence(), "go-parse-mismatch", edit_lie=True)

    def test_go_must_select_the_declared_workspace(self):
        fails(self, cadence(), "workspace-selection-mismatch", reported_gowork="off")
        standalone_request = standalone(
            LOCAL_ROOT, [("sub", LOCAL_SUB, None)], DEP.sum_lines()
        )
        fails(
            self,
            standalone_request,
            "workspace-selection-mismatch",
            reported_gowork=f"{P}/go.work",
        )


class BaseToolchain(unittest.TestCase):
    def test_go_work_and_go_mod_directives_newer_than_base_are_rejected(self):
        request = cadence()
        request["workspace"]["go_work"] = GO_WORK.replace("go 1.21", "go 1.27")
        container = fails(self, request, "toolchain-newer-than-base")
        self.assertEqual(container.calls_of("go", "mod", "download"), [])
        request = cadence()
        request["modules"][1]["go_mod"] += "toolchain go1.30.1\n"
        fails(self, request, "toolchain-newer-than-base")

    def test_probe_runs_before_staging_with_gowork_off(self):
        _, container = run(cadence())
        argv, env, _ = container.calls_of("go", "env", "-json")[0]
        self.assertIn("GOVERSION", argv)
        self.assertEqual(env["GOWORK"], "off")
        self.assertEqual(env["GOTOOLCHAIN"], "local")


class Cli(unittest.TestCase):
    def test_checkpoint_and_reuse(self):
        with tempfile.TemporaryDirectory() as tmp, remote_env():
            requests, output = Path(tmp) / "r.json", Path(tmp) / "o.json"
            requests.write_text(json.dumps({"requests": [cadence()]}))
            ok = {"id": "cadence", "kind": gw.KIND}
            with mock.patch.object(gw, "resolve", return_value=dict(ok)) as resolver:
                self.assertEqual(gw.main([str(requests), "--output", str(output)]), 0)
                self.assertEqual(gw.main([str(requests), "--output", str(output)]), 0)
            self.assertEqual(resolver.call_count, 1)
            saved = json.loads(output.read_text())["results"]["cadence"]
            self.assertEqual(saved["status"], "completed")

    def test_failure_is_recorded_with_reason_code(self):
        with tempfile.TemporaryDirectory() as tmp, remote_env():
            requests, output = Path(tmp) / "r.json", Path(tmp) / "o.json"
            bad = cadence()
            bad["workspace"] = {"mode": "auto"}
            requests.write_text(json.dumps({"requests": [bad]}))
            self.assertEqual(gw.main([str(requests), "--output", str(output)]), 1)
            saved = json.loads(output.read_text())["results"]["cadence"]
            self.assertEqual(saved["reason_code"], "unsupported-mode")


if __name__ == "__main__":
    unittest.main()
