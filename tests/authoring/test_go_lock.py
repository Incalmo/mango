"""Mocked tests for remote_go_deps; no Docker, network or Go is ever invoked.

Cache files are synthetic zips/mods built in memory or in temp directories.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from mango.authoring import go_lock as deps

BASE = "golang@sha256:" + "a" * 64
PROXY = "https://proxy.golang.org"
ROOT = deps.MODCACHE


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def h1_of(files: dict[str, bytes]) -> str:
    """Test-side h1 (separate from the module's) for building go.sum fixtures."""
    text = "".join(f"{sha(files[n])}  {n}\n" for n in sorted(files))
    return "h1:" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


class Mod:
    """A synthetic module version: zip + .mod + .info bytes and its go.sum lines."""

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
        self.info = json.dumps(
            {"Version": version, "Time": "2020-01-01T00:00:00Z"}
        ).encode()
        self.h1_zip = h1_of(self.files)
        self.h1_mod = h1_of({"go.mod": self.gomod})

    def sum_lines(self, zip_line=True):
        lines = [f"{self.path} {self.version}/go.mod {self.h1_mod}\n"]
        if zip_line:
            lines.insert(0, f"{self.path} {self.version} {self.h1_zip}\n")
        return "".join(lines)

    def ext_bytes(self):
        return {"zip": self.zip, "mod": self.gomod, "info": self.info}


def cache_path(mod, ext):
    return f"{ROOT}/{deps.cache_relpath(mod.path, mod.version, ext)}"


class FakeContainer:
    """Plays docker + go for one request; records every call."""

    def __init__(self, selected, **opts):
        self.selected = selected  # modules `go mod download` reports
        self.opts = opts
        self.files, self.calls = {}, []
        self.go_env_used = None

    def __call__(self, base):
        self.base = base
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def image_platform(self):
        return self.opts.get("platform", "linux/amd64")

    def copy_in(self, source, destination):
        self.files[destination] = Path(source).read_bytes()

    def exec(self, argv, env=None, workdir=None, check=True):
        self.calls.append((list(argv), dict(env or {}), workdir))

        def out(stdout="", stderr="", rc=0):
            return subprocess.CompletedProcess(argv, rc, stdout, stderr)

        if argv[:2] == ["go", "env"]:
            self.go_env_used = env
            if self.opts.get("no_go"):
                return out(rc=127, stderr="go: not found")
            return out(
                json.dumps(
                    {
                        "GOVERSION": self.opts.get("goversion", "go1.26.10"),
                        "GOOS": "linux",
                        "GOARCH": self.opts.get("goarch", "amd64"),
                    }
                )
            )
        if argv[:2] == ["go", "version"]:
            return out("go version go1.26.10 linux/amd64\n")
        if argv[0] == "mkdir":
            return out()
        if argv[:3] == ["go", "mod", "download"]:
            self.download_args = argv
            if "download_error" in self.opts:
                return out(rc=1, stderr=self.opts["download_error"])
            items = []
            chosen = self.selected
            if isinstance(chosen, dict):
                chosen = chosen[workdir]
            for mod in chosen:
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
            if self.opts.get("mutate_go_sum"):
                self.files[f"{workdir}/go.sum"] += b"extra h1:added\n"
            return out("\n".join(json.dumps(i, indent=1) for i in items))
        if argv[0] == "sha256sum":
            return out(
                "".join(
                    f"{sha(self.files[p])}  {p}\n" for p in argv[2:] if p in self.files
                )
            )
        if argv[0] == "cat":
            return out(self.files[argv[2]].decode())
        raise AssertionError(f"unexpected exec {argv}")


class Fetcher:
    """Plays the proxy: serves the module bytes (optionally tampered) and records URLs."""

    def __init__(self, mods, tamper=None, extra=None, missing=()):
        self.served, self.urls = {}, []
        for mod in mods:
            for ext, data in mod.ext_bytes().items():
                self.served[deps.proxy_url(PROXY, mod.path, mod.version, ext)] = data
        self.served.update(extra or {})
        self.served.update(tamper or {})
        self.missing = set(missing)
        self.limits = []

    def __call__(self, url, destination, max_bytes):
        self.urls.append(url)
        self.limits.append(max_bytes)
        if url in self.missing or url not in self.served:
            raise deps.ResolverError(f"HTTP 404 fetching {url}", "module-unavailable")
        data = self.served[url]
        if len(data) > max_bytes:
            raise deps.ResolverError("download exceeds limit", "limit-exceeded")
        destination.write_bytes(data)
        return sha(data)


def request(go_mod="module example.com/app\n\ngo 1.21\n", go_sum="", **over):
    base = {
        "id": "app",
        "base": BASE,
        "goproxy": PROXY,
        "modules": [{"dir": ".", "go_mod": go_mod, "go_sum": go_sum}],
    }
    base.update(over)
    return base


@contextlib.contextmanager
def remote_env():
    with mock.patch.dict(os.environ, {"MANGO_REMOTE_AUTHOR": "1"}):
        yield


A = Mod("github.com/Azure/azure-sdk", "v1.2.3", b"package azure\n")  # upper-case path
B = Mod("example.org/b", "v0.0.0-20200101000000-abcdef123456", b"package b\n")


def go_mod_for(*mods):
    body = "".join(f"\t{m.path} {m.version}\n" for m in mods)
    return f"module example.com/app\n\ngo 1.21\n\nrequire (\n{body})\n"


def run_resolve(mods=(A, B), selected=None, container_opts=None, fetcher=None, **over):
    container = FakeContainer(
        list(selected if selected is not None else mods), **(container_opts or {})
    )
    sums = "".join(m.sum_lines() for m in mods)
    req = request(go_mod_for(*mods), sums, **over)
    fetch = fetcher or Fetcher(mods)
    with remote_env():
        result = deps.resolve(req, fetch=fetch, container_factory=container)
    return result, container, fetch


# --------------------------------------------------------------------------
class DirHash(unittest.TestCase):
    def test_real_world_gosum_vector(self):
        # golang.org/x/text v0.3.0/go.mod line from public go.sum files.
        self.assertEqual(
            deps.h1_gomod(b"module golang.org/x/text\n"),
            "h1:NqM8EUOU14njkJ3fqMW+pc6Ldnwhi/IjpwHt7yyuwOQ=",
        )

    def test_zip_golden_computed_with_shell_tools(self):
        mod = Mod("example.com/m", "v1.0.0")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.zip"
            path.write_bytes(mod.zip)
            got = deps.h1_zip(path, mod.path, mod.version, deps.DEFAULT_LIMITS)
        # Produced independently with sha256sum/printf/base64 from the file lines.
        self.assertEqual(got, "h1:SXy42eQ/QbleX7vtuAPFnaM3tNuQnfeo7rulG/p7iOw=")

    def test_h1_is_not_the_file_sha256(self):
        mod = Mod("example.com/m", "v1.0.0")
        self.assertNotEqual(mod.h1_zip[3:], sha(mod.zip))
        self.assertNotEqual(base64.b64decode(mod.h1_zip[3:]).hex(), sha(mod.zip))

    def _zip(self, entries):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, data in entries:
                archive.writestr(name, data)
        return buffer.getvalue()

    def test_bad_zips_rejected(self):
        cases = {
            "outside prefix": [("evil/x.go", b"x")],
            "traversal": [("m@v1.0.0/../x.go", b"x")],
            "directory entry": [("m@v1.0.0/", b"")],
            "duplicate": [("m@v1.0.0/a", b"1"), ("m@v1.0.0/a", b"2")],
        }
        for label, entries in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "m.zip"
                path.write_bytes(self._zip(entries))
                with self.assertRaises(deps.ResolverError) as cm:
                    deps.h1_zip(path, "m", "v1.0.0", deps.DEFAULT_LIMITS)
                self.assertEqual(cm.exception.code, "bad-zip")

    def test_not_a_zip_and_bomb_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.zip"
            path.write_bytes(b"not a zip")
            with self.assertRaises(deps.ResolverError):
                deps.h1_zip(path, "m", "v1.0.0", deps.DEFAULT_LIMITS)
            path.write_bytes(self._zip([("m@v1.0.0/big", b"0" * 5000)]))
            with self.assertRaises(deps.ResolverError) as cm:
                deps.h1_zip(
                    path,
                    "m",
                    "v1.0.0",
                    {**deps.DEFAULT_LIMITS, "max_zip_unpacked_bytes": 100},
                )
            self.assertEqual(cm.exception.code, "limit-exceeded")


class Naming(unittest.TestCase):
    def test_escape_uppercase(self):
        self.assertEqual(
            deps.escape("github.com/Azure/Go-X"), "github.com/!azure/!go-!x"
        )
        self.assertEqual(deps.escape("v1.0.0-RC1"), "v1.0.0-!r!c1")

    def test_unsafe_names_rejected(self):
        for path, version in [
            ("../evil", "v1.0.0"),
            ("a//b", "v1.0.0"),
            ("a/./b", "v1.0.0"),
            ("a b", "v1.0.0"),
            ("example.com/m", "latest"),
            ("example.com/m", "v1.0.0/../x"),
        ]:
            with self.subTest(path=path, version=version):
                with self.assertRaises(deps.ResolverError):
                    deps.check_module(path, version)

    def test_version_ordering(self):
        k = deps.version_key
        self.assertLess(k("1.21"), k("1.21rc1"))
        self.assertLess(k("1.21rc1"), k("1.21.0"))
        self.assertLess(k("1.21.0"), k("1.21.1"))
        self.assertLess(k("1.9.9"), k("1.21"))
        self.assertEqual(deps.local_go_version("go1.26.10"), "1.26.10")
        with self.assertRaises(deps.ResolverError):
            deps.local_go_version("devel go1.27-abc")


class GoModScreening(unittest.TestCase):
    def screen(self, text, version="1.26.10"):
        deps.screen_go_mod(deps.parse_go_mod(text.encode()), version, "go.mod")

    def code(self, text, version="1.26.10"):
        with self.assertRaises(deps.ResolverError) as cm:
            self.screen(text, version)
        return cm.exception.code

    def test_local_replace_single_and_block(self):
        self.assertEqual(
            self.code("module m\nreplace example.com/x => ./local\n"),
            "go-mod-replace-local",
        )
        self.assertEqual(
            self.code("module m\nreplace (\n\texample.com/x v1.0.0 => ../x\n)\n"),
            "go-mod-replace-local",
        )

    def test_versioned_module_replacement_accepted(self):
        self.screen("module m\nreplace example.com/x => example.com/y v1.0.0\n")
        self.screen(
            "module m\nreplace (\nexample.com/x v1.0.0 => example.com/y v1.2.3\n)\n"
        )

    def test_replacement_still_requires_safe_exact_module_version(self):
        for target in (
            "example.com/y latest",
            "https://example.com/y v1.0.0",
            "example.com/../y v1.0.0",
            "example.com/y v1.0.0 extra",
        ):
            with self.subTest(target=target), self.assertRaises(deps.ResolverError):
                self.screen("module m\nreplace example.com/x => " + target + "\n")

    def test_commented_replace_is_not_a_replace(self):
        self.screen("module m\n// replace example.com/x => ./local\ngo 1.21\n")

    def test_toolchain_newer_than_base(self):
        self.assertEqual(self.code("module m\ngo 1.27\n"), "toolchain-newer-than-base")
        self.assertEqual(
            self.code("module m\ngo 1.21\ntoolchain go1.27.1\n"),
            "toolchain-newer-than-base",
        )
        self.screen("module m\ngo 1.26\ntoolchain go1.26.10\n")
        self.screen("module m\ngo 1.21\ntoolchain default\n")

    def test_parse_requires(self):
        parsed = deps.parse_go_mod(
            b'module m\ngo 1.21\nrequire a.com/x v1.0.0 // indirect\nrequire (\n"b.com/y" v2.0.0\n)\n'
        )
        self.assertEqual(
            parsed["requires"], [("a.com/x", "v1.0.0"), ("b.com/y", "v2.0.0")]
        )

    def test_go_sum_parse(self):
        good = A.sum_lines().encode()
        sums = deps.parse_go_sum(good)
        self.assertEqual(sums[(A.path, A.version, False)], {A.h1_zip})
        self.assertEqual(sums[(A.path, A.version, True)], {A.h1_mod})
        with self.assertRaises(deps.ResolverError):
            deps.parse_go_sum(b"only two\n")
        with self.assertRaises(deps.ResolverError):
            deps.parse_go_sum(b"a v1.0.0 h1:short\n")


class RequestValidation(unittest.TestCase):
    def bad(self, **over):
        with self.assertRaises(deps.ResolverError) as cm:
            deps.validate_request(request(**over))
        return cm.exception.code

    def test_valid(self):
        normal = deps.validate_request(request())
        self.assertEqual(normal["platform"], "linux/amd64")
        self.assertEqual(normal["limits"], deps.DEFAULT_LIMITS)

    def test_base_must_be_digest(self):
        self.assertEqual(self.bad(base="golang:1.26"), "invalid-base")
        self.assertEqual(self.bad(base="golang@sha256:abc"), "invalid-base")

    def test_platform(self):
        self.assertEqual(self.bad(platform="linux/arm64"), "base-platform")

    def test_goproxy_rules(self):
        for value in [
            "http://proxy.golang.org",
            "https://proxy.golang.org,direct",
            "https://proxy.golang.org|https://b.example.com",
            "direct",
            "off",
            "https://user:token@proxy.example.com",
            "https://proxy.example.com/?token=abc",
            "https://127.0.0.1:3000",
            "https://localhost",
            "https://goproxy.internal",
            "https://corp-proxy",
            "file:///tmp/x",
            "",
        ]:
            with self.subTest(value=value):
                self.assertEqual(self.bad(goproxy=value), "invalid-goproxy")
        self.assertEqual(deps.check_goproxy("https://proxy.golang.org/"), PROXY)

    def test_source_selection(self):
        req = request()
        req["source_path"] = "/srv/x"
        self.assertEqual(self.bad(source_path="/srv/x"), "invalid-request")
        bad = request()
        del bad["modules"]
        for path in ("relative/x", "/srv/../etc", "/"):
            bad["source_path"] = path
            with self.assertRaises(deps.ResolverError):
                deps.validate_request(bad)

    def test_unknown_keys_scope_limits(self):
        self.assertEqual(self.bad(upgrade=True), "invalid-request")
        self.assertEqual(self.bad(scope="updates"), "invalid-request")
        self.assertEqual(self.bad(gosumdb="off"), "invalid-request")
        self.assertEqual(
            self.bad(limits={"max_total_bytes": 1 << 60}), "invalid-request"
        )
        self.assertEqual(self.bad(limits={"bogus": 1}), "invalid-request")

    def test_dirs(self):
        self.assertEqual(
            self.bad(modules=[{"dir": "../x", "go_mod": "module m\n"}]),
            "invalid-request",
        )
        self.assertEqual(
            self.bad(
                modules=[
                    {"dir": "a", "go_mod": "module m\n"},
                    {"dir": "a", "go_mod": "module n\n"},
                ]
            ),
            "invalid-request",
        )


class Resolve(unittest.TestCase):
    def test_gate_blocks_everything(self):
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch("subprocess.run") as run,
            mock.patch("urllib.request.OpenerDirector.open") as opener,
        ):
            os.environ.pop("MANGO_REMOTE_AUTHOR", None)
            with self.assertRaises(deps.ResolverError) as cm:
                deps.resolve(request())
            self.assertEqual(cm.exception.code, "gate")
            with self.assertRaises(deps.ResolverError):
                deps.Container(BASE).__enter__()
        run.assert_not_called()
        opener.assert_not_called()

    def test_selection_and_hashes_preserved(self):
        result, container, fetch = run_resolve()
        artifacts = {a["path"]: a for a in result["lock"]["artifacts"]}
        prefix = result["cache"]["prefix"]
        self.assertEqual(result["lock"]["schema_version"], 1)
        # Exactly three files per module Go selected, nothing more, nothing less.
        self.assertEqual(len(artifacts), 6)
        a_zip = artifacts[
            f"{prefix}/cache/download/github.com/!azure/azure-sdk/@v/v1.2.3.zip"
        ]
        self.assertEqual(a_zip["sha256"], sha(A.zip))  # plain file SHA-256
        self.assertNotEqual(a_zip["sha256"], A.h1_zip)
        self.assertEqual(
            a_zip["urls"], [f"{PROXY}/github.com/!azure/azure-sdk/@v/v1.2.3.zip"]
        )
        b_mod = artifacts[
            f"{prefix}/cache/download/example.org/b/@v/v0.0.0-20200101000000-abcdef123456.mod"
        ]
        self.assertEqual(b_mod["sha256"], sha(B.gomod))
        # Every locked hash equals the bytes the proxy URL actually served.
        for artifact in artifacts.values():
            self.assertEqual(artifact["sha256"], sha(fetch.served[artifact["urls"][0]]))
        self.assertEqual(
            set(artifacts), {a["path"] for a in result["lock"]["artifacts"]}
        )
        self.assertEqual(
            sorted((m["module"], m["version"]) for m in result["evidence"]["modules"]),
            sorted([(A.path, A.version), (B.path, B.version)]),
        )

    def test_metadata_ziphash_matches_go_sum(self):
        result, _, _ = run_resolve()
        metadata = {m["path"]: m for m in result["cache"]["metadata"]}
        entry = metadata[
            f"{result['cache']['prefix']}/cache/download/github.com/!azure/azure-sdk/@v/v1.2.3.ziphash"
        ]
        self.assertEqual(entry["content"], A.h1_zip)
        self.assertEqual(entry["sha256"], sha(A.h1_zip.encode()))

    def test_go_invocation_is_hermetic_and_not_an_upgrade(self):
        _, container, _ = run_resolve()
        download = [c for c in container.calls if c[0][:3] == ["go", "mod", "download"]]
        self.assertEqual(len(download), 1)
        argv, env, _ = download[0]
        self.assertEqual(argv, ["go", "mod", "download", "-json"])
        self.assertEqual(env["GOTOOLCHAIN"], "local")
        self.assertEqual(env["GOPROXY"], PROXY)
        self.assertNotIn("direct", env["GOPROXY"])
        for key in ("GOFLAGS", "GOPRIVATE", "GONOSUMDB", "GONOPROXY", "GOINSECURE"):
            self.assertEqual(env[key], "")
        self.assertEqual(env["GOSUMDB"], "sum.golang.org")
        self.assertEqual((env["GOENV"], env["GOWORK"]), ("off", "off"))
        self.assertFalse(any("get" in c[0][:2] for c in container.calls))

    def test_scope_all(self):
        _, container, _ = run_resolve(scope="all")
        self.assertEqual(
            container.download_args, ["go", "mod", "download", "-json", "all"]
        )

    def test_originals_untouched_and_reported(self):
        go_mod = go_mod_for(A)
        result, container, _ = run_resolve(mods=(A,))
        digests = result["offline"]["source_files_sha256"]["."]
        self.assertEqual(digests["go.mod"], sha(go_mod.encode()))
        self.assertEqual(digests["go.sum"], sha(A.sum_lines().encode()))
        self.assertEqual(container.files[f"{deps.WORK}/src/0/go.mod"], go_mod.encode())

    def test_replacement_bytes_use_actual_module_sums_and_original_manifest(self):
        manifest = (
            "module example.com/app\n\ngo 1.21\nrequire example.com/original v1.0.0\n"
            f"replace example.com/original => {A.path} {A.version}\n"
        )
        container = FakeContainer([A])
        candidate = request(manifest, A.sum_lines())
        with remote_env():
            result = deps.resolve(
                candidate, fetch=Fetcher([A]), container_factory=container
            )
        self.assertEqual(
            container.files[f"{deps.WORK}/src/0/go.mod"], manifest.encode()
        )
        self.assertEqual(
            result["offline"]["source_files_sha256"]["."]["go.mod"],
            sha(manifest.encode()),
        )
        self.assertEqual(result["evidence"]["modules"][0]["module"], A.path)
        bad = request(
            manifest, A.sum_lines().replace(A.h1_zip, h1_of({"bad": b"bytes"}))
        )
        with remote_env(), self.assertRaises(deps.ResolverError):
            deps.resolve(bad, fetch=Fetcher([A]), container_factory=FakeContainer([A]))

    def test_mutation_in_container_detected(self):
        with self.assertRaises(deps.ResolverError) as cm:
            run_resolve(container_opts={"mutate_go_sum": True})
        self.assertEqual(cm.exception.code, "go-mod-mutated")

    def test_offline_settings(self):
        result, _, _ = run_resolve()
        offline = result["offline"]
        proxy = offline["proxy_mode"]["env"]
        self.assertEqual(proxy["GOPROXY"], "file://${MANGO_GO_CACHE}/cache/download")
        self.assertEqual(proxy["GOSUMDB"], "off")
        self.assertEqual(proxy["GOFLAGS"], "-mod=readonly")
        self.assertEqual(proxy["GOTOOLCHAIN"], "local")
        self.assertNotIn("direct", proxy["GOPROXY"])
        mc = offline["modcache_mode"]["env"]
        self.assertEqual(
            (mc["GOPROXY"], mc["GOMODCACHE"]), ("off", "${MANGO_GO_CACHE}")
        )
        self.assertFalse(offline["verified_by_offline_build"])

    def test_wrong_bytes_from_inferred_url_rejected(self):
        evil = A.zip + b"x"
        url = deps.proxy_url(PROXY, A.path, A.version, "zip")
        with self.assertRaises(deps.ResolverError) as cm:
            run_resolve(fetcher=Fetcher((A, B), tamper={url: evil}))
        self.assertEqual(cm.exception.code, "proxy-bytes-mismatch")

    def test_info_mismatch_also_rejected(self):
        url = deps.proxy_url(PROXY, B.path, B.version, "info")
        with self.assertRaises(deps.ResolverError) as cm:
            run_resolve(fetcher=Fetcher((A, B), tamper={url: b"{}"}))
        self.assertEqual(cm.exception.code, "proxy-bytes-mismatch")

    def test_go_sum_hash_mismatch(self):
        wrong = Mod(A.path, A.version, b"package different\n")
        container = FakeContainer([wrong])  # go & proxy agree with each other...
        req = request(go_mod_for(A), A.sum_lines())  # ...but go.sum is for the original
        with remote_env(), self.assertRaises(deps.ResolverError) as cm:
            deps.resolve(req, fetch=Fetcher([wrong]), container_factory=container)
        self.assertEqual(cm.exception.code, "go-sum-mismatch")

    def test_go_sum_entry_missing(self):
        container = FakeContainer([A])
        req = request(go_mod_for(A), "")
        with remote_env(), self.assertRaises(deps.ResolverError) as cm:
            deps.resolve(req, fetch=Fetcher([A]), container_factory=container)
        self.assertEqual(cm.exception.code, "go-sum-missing")

    def test_go_reported_sum_disagreeing(self):
        container = FakeContainer([A])
        orig = container.exec

        def exec(argv, *a, **k):
            result = orig(argv, *a, **k)
            if argv[:3] == ["go", "mod", "download"]:
                result.stdout = result.stdout.replace(A.h1_zip, h1_of({"x": b"y"}))
            return result

        container.exec = exec
        req = request(go_mod_for(A), A.sum_lines())
        with remote_env(), self.assertRaises(deps.ResolverError) as cm:
            deps.resolve(req, fetch=Fetcher([A]), container_factory=container)
        self.assertEqual(cm.exception.code, "go-sum-mismatch")

    def test_unexpected_cache_layout_rejected(self):
        container = FakeContainer([A])
        orig = container.exec

        def exec(argv, *a, **k):
            result = orig(argv, *a, **k)
            if argv[:3] == ["go", "mod", "download"]:
                result.stdout = result.stdout.replace("!azure", "azure")
            return result

        container.exec = exec
        req = request(go_mod_for(A), A.sum_lines())
        with remote_env(), self.assertRaises(deps.ResolverError) as cm:
            deps.resolve(req, fetch=Fetcher([A]), container_factory=container)
        self.assertEqual(cm.exception.code, "cache-mismatch")

    def test_go_failures_are_classified(self):
        cases = {
            "module-unavailable": "go: example.com/p@v1.0.0: reading https://proxy.golang.org/example.com/p/@v/v1.0.0.mod: 404 Not Found",
            "toolchain-newer-than-base": "go: go.mod requires go >= 1.30 (running go 1.26.10; GOTOOLCHAIN=local)",
            "go-sum-mismatch": "SECURITY ERROR\nchecksum mismatch",
            "go-sum-missing": "missing go.sum entry for module",
            "go-mod-needs-update": "go: updates to go.mod needed",
            "network": "dial tcp: lookup proxy.golang.org: no such host",
            "go-download-failed": "something odd",
        }
        for code, stderr in cases.items():
            with self.subTest(code):
                with self.assertRaises(deps.ResolverError) as cm:
                    run_resolve(mods=(A,), container_opts={"download_error": stderr})
                self.assertEqual(cm.exception.code, code)

    def test_error_text_redacts_credentials(self):
        stderr = (
            "reading https://user:secret@proxy.example.com/x?token=abc: 404 Not Found"
        )
        with self.assertRaises(deps.ResolverError) as cm:
            run_resolve(mods=(A,), container_opts={"download_error": stderr})
        self.assertNotIn("secret", str(cm.exception))
        self.assertNotIn("token=abc", str(cm.exception))

    def test_replace_fails_before_any_container(self):
        factory = mock.Mock()
        req = request("module m\ngo 1.21\nreplace example.com/x => ./x\n")
        with remote_env(), self.assertRaises(deps.ResolverError) as cm:
            deps.resolve(req, container_factory=factory)
        self.assertEqual(cm.exception.code, "go-mod-replace-local")
        factory.assert_not_called()

    def test_newer_go_than_base_no_download_attempted(self):
        container = FakeContainer([A], goversion="go1.21.0")
        req = request("module m\ngo 1.23\n", "")
        with remote_env(), self.assertRaises(deps.ResolverError) as cm:
            deps.resolve(req, fetch=Fetcher([]), container_factory=container)
        self.assertEqual(cm.exception.code, "toolchain-newer-than-base")
        self.assertFalse(
            any(c[0][:3] == ["go", "mod", "download"] for c in container.calls)
        )

    def test_platform_and_missing_go(self):
        for opts, code in [
            ({"platform": "linux/arm64"}, "base-platform"),
            ({"goarch": "arm64"}, "base-platform"),
            ({"no_go": True}, "go-missing"),
        ]:
            with self.subTest(code=code), self.assertRaises(deps.ResolverError) as cm:
                run_resolve(mods=(A,), container_opts=opts)
            self.assertEqual(cm.exception.code, code)

    def test_download_limits(self):
        with self.assertRaises(deps.ResolverError) as cm:
            run_resolve(mods=(A,), limits={"max_zip_bytes": 10})
        self.assertEqual(cm.exception.code, "limit-exceeded")
        with self.assertRaises(deps.ResolverError) as cm:
            run_resolve(mods=(A, B), limits={"max_artifacts": 2})
        self.assertEqual(cm.exception.code, "limit-exceeded")
        with self.assertRaises(deps.ResolverError) as cm:
            run_resolve(mods=(A, B), limits={"max_total_bytes": 300})
        self.assertEqual(cm.exception.code, "limit-exceeded")

    def test_fetch_gets_a_bounded_budget(self):
        _, _, fetch = run_resolve(mods=(A,), limits={"max_zip_bytes": 1 << 20})
        by_url = dict(zip(fetch.urls, fetch.limits))
        self.assertEqual(
            by_url[deps.proxy_url(PROXY, A.path, A.version, "zip")], 1 << 20
        )
        self.assertEqual(
            by_url[deps.proxy_url(PROXY, A.path, A.version, "info")], 1 << 20
        )
        self.assertEqual(
            by_url[deps.proxy_url(PROXY, A.path, A.version, "mod")], 16 << 20
        )

    def test_graph_mod_files_locked_and_verified(self):
        extra = Mod("example.net/only-graph", "v1.0.0")
        sums = A.sum_lines() + extra.sum_lines(zip_line=False)
        container = FakeContainer([A])
        req = request(go_mod_for(A), sums)
        fetch = Fetcher([A, extra])
        with remote_env():
            result = deps.resolve(req, fetch=fetch, container_factory=container)
        paths = [a["path"] for a in result["lock"]["artifacts"]]
        graph = [p for p in paths if "only-graph" in p]
        self.assertEqual(len(graph), 1)
        self.assertTrue(graph[0].endswith("only-graph/@v/v1.0.0.mod"))
        self.assertFalse(any(p.endswith("only-graph/@v/v1.0.0.zip") for p in paths))
        self.assertEqual(result["evidence"]["graph_mod_files"], 1)

    def test_graph_mod_wrong_bytes_rejected(self):
        extra = Mod("example.net/only-graph", "v1.0.0")
        url = deps.proxy_url(PROXY, extra.path, extra.version, "mod")
        container = FakeContainer([A])
        req = request(go_mod_for(A), A.sum_lines() + extra.sum_lines(zip_line=False))
        with remote_env(), self.assertRaises(deps.ResolverError) as cm:
            deps.resolve(
                req,
                fetch=Fetcher([A], extra={url: b"module example.net/evil\n"}),
                container_factory=container,
            )
        self.assertEqual(cm.exception.code, "go-sum-mismatch")

    def test_graph_mod_unavailable_fails_closed_unless_allowed(self):
        extra = Mod("example.net/gone", "v1.0.0")
        sums = A.sum_lines() + extra.sum_lines(zip_line=False)
        for allow in (False, True):
            container = FakeContainer([A])
            req = request(go_mod_for(A), sums, allow_missing_graph_mods=allow)
            with remote_env():
                if allow:
                    result = deps.resolve(
                        req, fetch=Fetcher([A]), container_factory=container
                    )
                    self.assertEqual(
                        result["evidence"]["missing_graph_mods"],
                        ["example.net/gone@v1.0.0"],
                    )
                else:
                    with self.assertRaises(deps.ResolverError) as cm:
                        deps.resolve(
                            req, fetch=Fetcher([A]), container_factory=container
                        )
                    self.assertEqual(cm.exception.code, "graph-mod-unavailable")

    def test_graph_mod_files_can_be_disabled(self):
        extra = Mod("example.net/only-graph", "v1.0.0")
        container = FakeContainer([A])
        req = request(
            go_mod_for(A),
            A.sum_lines() + extra.sum_lines(zip_line=False),
            graph_mod_files=False,
        )
        with remote_env():
            result = deps.resolve(req, fetch=Fetcher([A]), container_factory=container)
        self.assertEqual(len(result["lock"]["artifacts"]), 3)

    def test_multi_module_dedupes_shared_versions(self):
        container = FakeContainer([A])
        mods = [
            {"dir": ".", "go_mod": go_mod_for(A), "go_sum": A.sum_lines()},
            {"dir": "pkg/apis", "go_mod": go_mod_for(A), "go_sum": A.sum_lines()},
        ]
        with remote_env():
            result = deps.resolve(
                request(modules=mods), fetch=Fetcher([A]), container_factory=container
            )
        self.assertEqual(len(result["lock"]["artifacts"]), 3)
        self.assertEqual(
            [c[2] for c in container.calls if c[0][:3] == ["go", "mod", "download"]],
            [f"{deps.WORK}/src/0", f"{deps.WORK}/src/1"],
        )


class SourcePath(unittest.TestCase):
    def make(self, tmp, go_mod=None, go_sum=None):
        root = Path(tmp)
        (root / "go.mod").write_text(go_mod if go_mod is not None else go_mod_for(A))
        (root / "go.sum").write_text(go_sum if go_sum is not None else A.sum_lines())
        return root

    def req(self, root, **over):
        return {
            "id": "src",
            "base": BASE,
            "goproxy": PROXY,
            "source_path": str(root),
            **over,
        }

    def test_reads_exact_bytes_and_leaves_files_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(tmp)
            before = {n: (root / n).read_bytes() for n in ("go.mod", "go.sum")}
            mtimes = {n: (root / n).stat().st_mtime_ns for n in before}
            with remote_env():
                result = deps.resolve(
                    self.req(root),
                    fetch=Fetcher([A]),
                    container_factory=FakeContainer([A]),
                )
            self.assertEqual(before, {n: (root / n).read_bytes() for n in before})
            self.assertEqual(mtimes, {n: (root / n).stat().st_mtime_ns for n in before})
            self.assertEqual(
                result["offline"]["source_files_sha256"]["."]["go.mod"],
                sha(before["go.mod"]),
            )

    def test_source_changed_during_run_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(tmp)
            container = FakeContainer([A])
            orig = container.exec

            def exec(argv, *a, **k):
                result = orig(argv, *a, **k)
                if argv[:3] == ["go", "mod", "download"]:
                    (root / "go.mod").write_text(go_mod_for(A) + "// edited\n")
                return result

            container.exec = exec
            with remote_env(), self.assertRaises(deps.ResolverError) as cm:
                deps.resolve(
                    self.req(root), fetch=Fetcher([A]), container_factory=container
                )
            self.assertEqual(cm.exception.code, "source-mutated")

    def test_go_work_and_missing_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(tmp)
            (root / "go.work").write_text("go 1.21\n")
            with remote_env(), self.assertRaises(deps.ResolverError) as cm:
                deps.resolve(self.req(root), container_factory=mock.Mock())
            self.assertEqual(cm.exception.code, "go-work")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(deps.ResolverError) as cm:
                deps.load_sources(deps.validate_request(self.req(Path(tmp))))
            self.assertEqual(cm.exception.code, "source-missing")
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(tmp)
            (root / "go.sum").unlink()
            (root / "go.sum").symlink_to(root / "go.mod")
            with self.assertRaises(deps.ResolverError) as cm:
                deps.load_sources(deps.validate_request(self.req(root)))
            self.assertEqual(cm.exception.code, "source-missing")

    def test_oversize_go_mod_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(tmp, go_mod="module m\n" + "// x\n" * 10)
            req = self.req(root, limits={"max_mod_bytes": 8})
            with self.assertRaises(deps.ResolverError) as cm:
                deps.load_sources(deps.validate_request(req))
            self.assertEqual(cm.exception.code, "limit-exceeded")

    def test_nested_module_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(tmp)
            sub = root / "pkg" / "apis"
            sub.mkdir(parents=True)
            (sub / "go.mod").write_text(go_mod_for(B))
            (sub / "go.sum").write_text(B.sum_lines())
            container = FakeContainer(
                {f"{deps.WORK}/src/0": [A], f"{deps.WORK}/src/1": [B]}
            )
            with remote_env():
                result = deps.resolve(
                    self.req(root, module_dirs=[".", "pkg/apis"]),
                    fetch=Fetcher([A, B]),
                    container_factory=container,
                )
            self.assertEqual(
                sorted(result["offline"]["source_files_sha256"]), [".", "pkg/apis"]
            )


class Cli(unittest.TestCase):
    def run_cli(self, requests, output, *flags, resolver=None):
        req_file = output.parent / "requests.json"
        req_file.write_text(json.dumps({"requests": requests}))
        with (
            remote_env(),
            mock.patch.object(deps, "resolve", resolver),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return deps.main([str(req_file), "--output", str(output), *flags])

    def test_requires_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            req_file = Path(tmp) / "r.json"
            req_file.write_text('{"requests": []}')
            env = {k: v for k, v in os.environ.items() if k != "MANGO_REMOTE_AUTHOR"}
            with (
                mock.patch.dict(os.environ, env, clear=True),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                deps.main([str(req_file), "--output", str(Path(tmp) / "o.json")])

    def test_checkpoint_resume_and_retry(self):
        calls = []

        def resolver(req, *a, **k):
            calls.append(req["id"])
            if req["id"] == "bad" and len(calls) < 3:
                raise deps.ResolverError(
                    "module not served https://u:p@h/x?t=1", "module-unavailable"
                )
            return {"id": req["id"], "lock": {"schema_version": 1, "artifacts": []}}

        reqs = [request(id="ok"), request(id="bad")]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.json"
            self.assertEqual(self.run_cli(reqs, out, resolver=resolver), 1)
            saved = json.loads(out.read_text())["results"]
            self.assertEqual(saved["ok"]["status"], "completed")
            self.assertEqual(saved["bad"]["status"], "failed")
            self.assertEqual(saved["bad"]["reason_code"], "module-unavailable")
            self.assertNotIn("u:p", saved["bad"]["error"])
            self.assertNotIn("t=1", saved["bad"]["error"])
            # rerun without flag: nothing re-executed (resume)
            self.run_cli(reqs, out, resolver=resolver)
            self.assertEqual(calls, ["ok", "bad"])
            # --retry-failures re-runs only the failed request
            self.assertEqual(
                self.run_cli(reqs, out, "--retry-failures", resolver=resolver), 0
            )
            self.assertEqual(calls, ["ok", "bad", "bad"])
            self.assertEqual(calls.count("ok"), 1)
            self.assertEqual(
                json.loads(out.read_text())["results"]["bad"]["status"], "completed"
            )

    def test_changed_request_or_source_reruns(self):
        calls = []

        def resolver(req, *a, **k):
            calls.append(req["id"])
            return {"id": req["id"], "lock": {"schema_version": 1, "artifacts": []}}

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as src:
            root = Path(src)
            (root / "go.mod").write_text(go_mod_for(A))
            (root / "go.sum").write_text(A.sum_lines())
            out = Path(tmp) / "out.json"
            req = {"id": "s", "base": BASE, "goproxy": PROXY, "source_path": str(root)}
            self.run_cli([req], out, resolver=resolver)
            self.run_cli([req], out, resolver=resolver)
            self.assertEqual(calls, ["s"])
            (root / "go.sum").write_text(A.sum_lines() + B.sum_lines())
            self.run_cli([req], out, resolver=resolver)
            self.assertEqual(
                calls, ["s", "s"]
            )  # source bytes changed -> stale result dropped

    def test_unexpected_exception_checkpointed(self):
        def resolver(req, *a, **k):
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.json"
            self.assertEqual(self.run_cli([request(id="x")], out, resolver=resolver), 1)
            self.assertEqual(
                json.loads(out.read_text())["results"]["x"]["reason_code"], "unexpected"
            )


class DockerCommands(unittest.TestCase):
    def test_container_is_amd64_root_pinned_base(self):
        with remote_env(), mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            container = deps.Container(BASE)
            container.__enter__()
            container.exec(
                ["go", "version"], env={"GOTOOLCHAIN": "local"}, workdir="/x"
            )
        first = run.call_args_list[0].args[0]
        self.assertEqual(first[:2], ["docker", "run"])
        self.assertEqual(first[first.index("--platform") + 1], "linux/amd64")
        self.assertIn(BASE, first)
        second = run.call_args_list[1].args[0]
        self.assertEqual(second[:2], ["docker", "exec"])
        self.assertIn("GOTOOLCHAIN=local", second)
        self.assertEqual(second[second.index("-w") + 1], "/x")

    def test_download_enforces_cap_and_https(self):
        class Response(io.BytesIO):
            headers = {}

            def geturl(self):
                return "https://proxy.golang.org/x"

            def __enter__(self):
                return self

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "f"
            with mock.patch.object(
                deps._OPENER, "open", return_value=Response(b"x" * 100)
            ):
                with self.assertRaises(deps.ResolverError) as cm:
                    deps.download("https://proxy.golang.org/x", dest, 10)
                self.assertEqual(cm.exception.code, "limit-exceeded")
                self.assertFalse(dest.exists())
            with mock.patch.object(deps._OPENER, "open", return_value=Response(b"abc")):
                self.assertEqual(
                    deps.download("https://proxy.golang.org/x", dest, 10), sha(b"abc")
                )


if __name__ == "__main__":
    unittest.main()
