"""Evaluation-only tests for lib/adapters/pnpm.nix: nothing is built or fetched.

Run with: nix-shell --run 'python -m unittest tests.lib.test_pnpm_cache'
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HASH = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
PREFIX = f"""let pkgs = import (import {ROOT}/lib/pins/nixpkgs.nix) {{ system = "x86_64-linux"; }};
 p = import {ROOT}/lib/adapters/pnpm.nix {{ inherit pkgs; }}; in """


ROOT_ACK = 'acknowledgedUnlisted = [ "package.json" "pnpm-lock.yaml" ]; '


def nix(expr):
    return subprocess.run(
        ["nix-instantiate", "--eval", "--strict", "--expr", PREFIX + expr],
        capture_output=True, text=True, check=False,
    )


class PnpmCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "package.json").write_text("{}")
        (self.root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")

    def cache(self, extra="", pnpm="pkgs.pnpm_10", files='[ "package.json" "pnpm-lock.yaml" ]'):
        return (f"(p.fetchPnpmCache {{ pnpm = {pnpm}; source = {self.root}; hash = \"{HASH}\";"
                f" projectFiles = {files}; {extra} }})")

    def ok(self, expr):
        result = nix(expr)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def rejected(self, expr, text):
        result = nix(expr)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(text, result.stderr)

    def test_instantiates_and_reports_metadata(self):
        out = self.ok(self.cache() + ".mangoPnpm.expectedStoreLayout")
        self.assertEqual(out.strip(), '"v10"')
        self.assertEqual(self.ok(self.cache(pnpm="pkgs.pnpm_11") + ".mangoPnpm.fetcherVersion").strip(), "4")
        self.ok(f"builtins.isString {self.cache()}.drvPath")

    def test_pnpm_version_is_never_substituted(self):
        self.rejected(self.cache('expectedPnpmVersion = "10.17.1";') + ".drvPath", "never substituted")

    def test_unsupported_majors_fail_clearly(self):
        tool = f'(p.fetchPnpmTool {{ version = "12.4.1"; hash = "{HASH}"; }})'
        self.rejected(self.cache(pnpm=tool) + ".drvPath", "Needed capability")
        tool9 = f'(p.fetchPnpmTool {{ version = "9.7.0"; hash = "{HASH}"; }})'
        self.rejected(self.cache(pnpm=tool9) + ".drvPath", "acceptUnvettedMajor")
        self.ok(self.cache("acceptUnvettedMajor = true;", pnpm=tool9) + ".drvPath")

    def test_pnpm_tool_exact_version(self):
        out = self.ok(f'(p.fetchPnpmTool {{ version = "10.17.1"; hash = "{HASH}"; }}).version')
        self.assertEqual(out.strip(), '"10.17.1"')
        self.rejected(f'(p.fetchPnpmTool {{ version = "10"; hash = "{HASH}"; }}).drvPath', "exact release")

    def test_rejects_unlisted_workspace_config(self):
        (self.root / ".npmrc").write_text("shamefully-hoist=true\n")
        self.rejected(self.cache() + ".inputs.drvPath", ".npmrc exists in the source")
        self.ok(self.cache('acknowledgedUnlisted = [ ".npmrc" ];') + ".inputs.drvPath")

    def test_rejects_unlisted_nested_manifest(self):
        (self.root / "packages" / "a").mkdir(parents=True)
        (self.root / "packages" / "a" / "package.json").write_text("{}")
        self.rejected(self.cache() + ".inputs.drvPath", "packages/a/package.json")

    def test_rejects_weakened_flags_and_pnpmfile(self):
        self.rejected(self.cache('pnpmInstallFlags = [ "--no-frozen-lockfile" ];') + ".drvPath", "must not weaken")
        (self.root / ".pnpmfile.cjs").write_text("module.exports = {}\n")
        self.rejected(self.cache(files='[ "package.json" "pnpm-lock.yaml" ".pnpmfile.cjs" ]') + ".drvPath", "pnpmfile")

    def test_requires_lock_and_hash(self):
        self.rejected(self.cache(files='[ "package.json" ]') + ".drvPath", "pnpm-lock.yaml")
        bad = self.cache().replace(HASH, "")
        self.rejected(bad + ".drvPath", "SRI sha256")

    def test_unsafe_paths(self):
        for bad in ("../x", "a/../b", "/abs", "a//b", "./a", "a/", 'a\\\\b', "a$b", "a`b`"):
            with self.subTest(bad=bad):
                self.rejected(self.cache(files=f'[ "package.json" "pnpm-lock.yaml" "{bad}" ]') + ".drvPath", "safe relative")
        self.rejected(self.cache('projectDir = "../x";') + ".drvPath", "projectDir")
        self.rejected(self.cache('acknowledgedUnlisted = [ "../x" ];') + ".drvPath", "safe relative")

    def test_nested_and_prefixed_pnpmfiles_need_ignore_flag(self):
        (self.root / "frontend" / "tools").mkdir(parents=True)
        for name in ("package.json", "pnpm-lock.yaml"):
            (self.root / "frontend" / name).write_text((self.root / name).read_text())
        for pnpmfile in (".pnpmfile.cjs", "pnpmfile.cjs", ".pnpmfile.mjs", "pnpmfile.mjs", "tools/.pnpmfile.cjs"):
            (self.root / "frontend" / pnpmfile).write_text("module.exports = {}\n")
            files = f'[ "package.json" "pnpm-lock.yaml" "{pnpmfile}" ]'
            with self.subTest(pnpmfile=pnpmfile):
                self.rejected(self.cache(ROOT_ACK + 'projectDir = "frontend";', files=files) + ".drvPath", "pnpmfile")
                self.ok(self.cache(ROOT_ACK + 'projectDir = "frontend"; pnpmInstallFlags = [ "--ignore-pnpmfile" ];', files=files)
                        + ".inputs.drvPath")
            (self.root / "frontend" / pnpmfile).unlink()

    def test_unlisted_pnpmfile_in_source_aborts(self):
        (self.root / "frontend").mkdir()
        (self.root / "frontend" / "pnpmfile.mjs").write_text("export default {}\n")
        self.rejected(self.cache() + ".inputs.drvPath", "pnpmfile.mjs exists in the source")

    def test_flag_guards(self):
        for flag in ("--no-frozen-lockfile", "--frozen-lockfile=false", "--lockfile-only", "--no-lockfile",
                     "--lockfile=false", "--ignore-scripts=false", "--no-ignore-scripts", "--registry",
                     "--registry=https://r.example/", "--offline", "--prefer-offline", "--store-dir=/x",
                     "--config.foo=1", "--dir", "--dir=other", "-C", "-Cother", "--prefix=x",
                     "--lockfile-dir=..", "--pnpmfile=x.cjs", "--global-pnpmfile=x.cjs", "--no-ignore-pnpmfile",
                     "--ignore-pnpmfile=false", "-g", "--global", "--workspace-root"):
            with self.subTest(flag=flag):
                self.rejected(self.cache(f'pnpmInstallFlags = [ "{flag}" ];') + ".drvPath", "must not weaken")
        self.ok(self.cache('pnpmInstallFlags = [ "--shamefully-hoist" "--prod" ];') + ".drvPath")

    def test_layout_table(self):
        for version, layout in (("9.7.0", "v3"), ("10.2.0", "v10"), ("11.3.0", "v11")):
            tool = f'(p.fetchPnpmTool {{ version = "{version}"; hash = "{HASH}"; }})'
            with self.subTest(version=version):
                out = self.ok(self.cache("acceptUnvettedMajor = true;", pnpm=tool) + ".mangoPnpm.expectedStoreLayout")
                self.assertEqual(out.strip(), f'"{layout}"')
        self.rejected(self.cache(pnpm="pkgs.pnpm_11", extra="fetcherVersion = 3;") + ".drvPath", "fetcherVersion")


class PnpmInputChecks(unittest.TestCase):
    """Runs the generated input-check script with the shell's pinned Python yq."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "src"
        self.root.mkdir()
        self.write("package.json", "{}")
        self.write("pnpm-lock.yaml", "lockfileVersion: '9.0'\npackages:\n  a@1.0.0:\n    resolution: {integrity: sha512-xx}\n")

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_check(self, files, extra=""):
        listed = " ".join(f'"{f}"' for f in files)
        expr = (PREFIX + f'(p.fetchPnpmCache {{ pnpm = pkgs.pnpm_10; source = {self.root}; hash = "{HASH}";'
                f" projectFiles = [ {listed} ]; {extra} }}).inputs")
        drv = subprocess.run(["nix-instantiate", "--expr", expr], capture_output=True, text=True, check=True).stdout.strip()
        shown = json.loads(subprocess.run(["nix", "derivation", "show", drv], capture_output=True, text=True, check=True).stdout)
        script = next(iter(shown["derivations"].values()))["env"]["buildCommand"]
        # The strict-YAML helper's python env is a store path that is not built locally; run the
        # same script with the PyYAML-capable python of this shell instead.
        script = re.sub(r"/nix/store/[a-z0-9]{32}-python3-[^/\s]*-env/bin/python3", shutil.which("python3"), script)
        out = Path(self.tmp.name) / "out"
        shutil.rmtree(out, ignore_errors=True)
        env = {**os.environ, "out": str(out)}
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env), out

    def test_accepts_registry_lock(self):
        result, out = self.run_check(["package.json", "pnpm-lock.yaml"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((out / "pnpm-lock.yaml").is_file())

    def test_rejects_git_and_unhashed_sources(self):
        self.write("pnpm-lock.yaml", "lockfileVersion: '9.0'\npackages:\n  g@1.0.0:\n    resolution: {type: git, repo: https://x/y, commit: abc}\n")
        result, _ = self.run_check(["package.json", "pnpm-lock.yaml"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not credential-free hashed registry tarballs", result.stderr)

    def test_rejects_credentialed_tarball(self):
        self.write("pnpm-lock.yaml", "lockfileVersion: '9.0'\npackages:\n  a@1.0.0:\n    resolution: {integrity: sha512-xx, tarball: 'https://u:p@h/a.tgz'}\n")
        result, _ = self.run_check(["package.json", "pnpm-lock.yaml"])
        self.assertNotEqual(result.returncode, 0)

    def test_requires_importer_manifests_and_patches(self):
        self.write("pnpm-lock.yaml", "lockfileVersion: '9.0'\nimporters:\n  .: {}\n  pkg/a: {}\n")
        self.write("pnpm-workspace.yaml", "packages: ['pkg/*']\n")
        result, _ = self.run_check(["package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml"])
        self.assertIn("importer pkg/a has no listed package.json", result.stderr)
        self.write("pkg/a/package.json", "{}")
        result, _ = self.run_check(["package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "pkg/a/package.json"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.write("package.json", '{"pnpm": {"patchedDependencies": {"x@1": "patches/x.patch"}}}')
        result, _ = self.run_check(["package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "pkg/a/package.json"])
        self.assertIn("patch patches/x.patch", result.stderr)

    def test_rejects_mismatched_package_manager_and_credentials(self):
        self.write("package.json", '{"packageManager": "pnpm@9.7.0+sha512.abc"}')
        result, _ = self.run_check(["package.json", "pnpm-lock.yaml"])
        self.assertIn("pnpm@9.7.0", result.stderr)
        self.write("package.json", "{}")
        self.write(".npmrc", "//registry.npmjs.org/:_authToken=abc\n")
        result, _ = self.run_check(["package.json", "pnpm-lock.yaml", ".npmrc"])
        self.assertIn("credentials", result.stderr)

    BASE = ["package.json", "pnpm-lock.yaml"]
    LOCK = "lockfileVersion: '9.0'\npackages:\n  a@1.0.0:\n    resolution: {%s}\n"

    def lock(self, resolution):
        self.write("pnpm-lock.yaml", self.LOCK % resolution)

    def fails(self, files, text, **kw):
        result, _ = self.run_check(files, **kw)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(text, result.stderr)

    def test_duplicate_yaml_maps_rejected(self):
        # Last key wins in PyYAML, so the clean second `packages` would hide the git entry.
        self.write("pnpm-lock.yaml", "lockfileVersion: '9.0'\npackages:\n  g@1:\n    resolution: {type: git, repo: x, commit: y}\n"
                   "packages:\n  a@1.0.0:\n    resolution: {integrity: sha512-xx}\n")
        self.fails(self.BASE, "unsafe YAML")
        self.write("pnpm-lock.yaml", "lockfileVersion: '9.0'\npackages:\n  a@1.0.0:\n    resolution: {integrity: sha512-xx, integrity: sha512-yy}\n")
        self.fails(self.BASE, "unsafe YAML")
        self.write("pnpm-lock.yaml", "lockfileVersion: '9.0'\nx: &a {y: 1}\nz: *a\n")
        self.fails(self.BASE, "unsafe YAML")
        self.write("pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
        self.write("pnpm-workspace.yaml", "patchedDependencies: {}\npatchedDependencies: {x: p.patch}\n")
        self.fails(self.BASE + ["pnpm-workspace.yaml"], "unsafe YAML")

    def test_unsafe_importer_and_patch_paths(self):
        for imp in ("../x", "/etc", "a/../../x", "a//b"):
            with self.subTest(importer=imp):
                self.write("pnpm-lock.yaml", f"lockfileVersion: '9.0'\nimporters:\n  .: {{}}\n  '{imp}': {{}}\n")
                self.write("pnpm-workspace.yaml", "packages: ['*']\n")
                self.fails(self.BASE + ["pnpm-workspace.yaml"], "not a safe relative path")
        (self.root / "pnpm-workspace.yaml").unlink()
        self.write("pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
        for patch in ("../outside.patch", "/etc/passwd", "p/../../q", "./p.patch"):
            with self.subTest(patch=patch):
                self.write("package.json", json.dumps({"pnpm": {"patchedDependencies": {"x@1": patch}}}))
                self.fails(self.BASE, "not a safe relative path")
        self.write("pnpm-lock.yaml", "lockfileVersion: '9.0'\npatchedDependencies:\n  x@1: {hash: h, path: ../x.patch}\n")
        self.write("package.json", "{}")
        self.fails(self.BASE, "not a safe relative path")

    def test_listed_patch_accepted(self):
        self.write("package.json", json.dumps({"pnpm": {"patchedDependencies": {"x@1": "patches/x.patch"}}}))
        self.write("patches/x.patch", "--- a\n+++ b\n")
        result, _ = self.run_check(self.BASE + ["patches/x.patch"])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_registry_urls_and_non_registry_tarballs(self):
        for res in ("integrity: sha512-xx, tarball: 'https://u:p@h/a.tgz'",
                    "integrity: sha512-xx, tarball: 'https://h/a.tgz?token=abc'",
                    "integrity: sha512-xx, tarball: 'https://h/a.tgz?X-Amz-Signature=abc'",
                    "integrity: sha512-xx, tarball: 'https://h/a.tgz?SIG=abc'",
                    "integrity: sha512-xx, tarball: 'file:../a.tgz'",
                    "integrity: sha512-xx, tarball: 'git+https://h/a.git'",
                    "integrity: sha512-xx, directory: ../a, type: directory",
                    "tarball: 'https://codeload.github.com/o/r/tar.gz/abc'"):
            with self.subTest(res=res):
                self.lock(res)
                self.fails(self.BASE, "not credential-free hashed registry tarballs")
        self.lock("integrity: sha512-xx, tarball: 'https://registry.npmjs.org/a/-/a-1.0.0.tgz'")
        result, _ = self.run_check(self.BASE)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_config_credential_and_diversion_forms(self):
        for line in ("//registry.npmjs.org/:_authToken=abc", "//h/:_auth=abc", "_auth", "_password=x",
                     "registry=https://user:pw@r.example/", "@s:registry=https://r.example/?token=abc",
                     "registry=${REGISTRY}", "tokenHelper=/bin/x", "token-helper=/bin/x", "username=bob",
                     "email=a@b.c", "store-dir=/tmp/x", "storeDir: /tmp/x", "userconfig=/x", "offline=true",
                     "pnpmfile=./.pnpmfile.cjs", "registry=https://r.example/a?X-Goog-Signature=abc"):
            with self.subTest(line=line):
                self.write(".npmrc", line + "\n")
                result, _ = self.run_check(self.BASE + [".npmrc"])
                self.assertNotEqual(result.returncode, 0, line)
        self.write(".npmrc", "registry=https://registry.npmjs.org/\nshamefully-hoist=true\n")
        result, _ = self.run_check(self.BASE + [".npmrc"])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_nested_config_files_are_screened(self):
        self.write("pnpm-workspace.yaml", "packages: ['pkg/*']\n")
        self.write("pkg/a/package.json", "{}")
        self.write("pkg/a/.npmrc", "//h/:_authToken=abc\n")
        self.write("pnpm-lock.yaml", "lockfileVersion: '9.0'\nimporters:\n  .: {}\n  pkg/a: {}\n")
        self.fails(self.BASE + ["pnpm-workspace.yaml", "pkg/a/package.json", "pkg/a/.npmrc"], "pkg/a/.npmrc")

    def test_workspace_yaml_credentials_and_pnpmfile(self):
        self.write("pnpm-workspace.yaml", "registry: https://u:p@r.example/\n")
        self.fails(self.BASE + ["pnpm-workspace.yaml"], "pnpm-workspace.yaml")
        self.write("pnpm-workspace.yaml", "pnpmfile: tools/.pnpmfile.cjs\n")
        self.fails(self.BASE + ["pnpm-workspace.yaml"], "configures a pnpmfile")

    def test_project_dir_prefix(self):
        self.write("frontend/package.json", "{}")
        self.write("frontend/pnpm-lock.yaml", self.LOCK % "integrity: sha512-xx")
        self.write("frontend/.npmrc", "//h/:_authToken=abc\n")
        files = self.BASE + [".npmrc"]
        self.fails(files, "frontend/.npmrc", extra=ROOT_ACK + 'projectDir = "frontend";')
        (self.root / "frontend" / ".npmrc").write_text("shamefully-hoist=true\n")
        result, out = self.run_check(files, extra=ROOT_ACK + 'projectDir = "frontend";')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((out / "frontend" / "pnpm-lock.yaml").is_file())

    def test_symlinked_input_rejected(self):
        outside = Path(self.tmp.name) / "outside.json"
        outside.write_text("{}")
        (self.root / "package.json").unlink()
        (self.root / "package.json").symlink_to(outside)
        result, _ = self.run_check(self.BASE)
        self.assertNotEqual(result.returncode, 0)

    def test_dev_engines_manager(self):
        self.write("package.json", json.dumps({"devEngines": {"packageManager": {"name": "pnpm", "version": "9.7.0"}}}))
        self.fails(self.BASE, "devEngines.packageManager is pnpm@9.7.0")
        self.write("package.json", json.dumps({"devEngines": {"packageManager": {"name": "npm", "version": "10.0.0"}}}))
        self.fails(self.BASE, "not pnpm")
        self.write("package.json", json.dumps({"devEngines": {"packageManager": [{"name": "pnpm", "version": "^10"}]}}))
        result, _ = self.run_check(self.BASE)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_multi_document_lockfile_versions_checked(self):
        self.write("pnpm-lock.yaml", "lockfileVersion: '9.0'\n---\nlockfileVersion: '12.0'\n")
        self.fails(self.BASE, "newer than pnpm")

    def test_rejects_newer_lockfile(self):
        self.write("pnpm-lock.yaml", "lockfileVersion: '12.0'\n")
        result, _ = self.run_check(["package.json", "pnpm-lock.yaml"])
        self.assertIn("newer than pnpm", result.stderr)


if __name__ == "__main__":
    unittest.main()
