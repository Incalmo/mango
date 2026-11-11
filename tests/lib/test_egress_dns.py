"""Resolver retries retain exact policy targets and emit no partial updates."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2]
FAKE_COMMAND = r"""
import json, os, sys
from pathlib import Path

log = Path(os.environ["RESOLVER_LOG"])
calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with log.open("a") as stream:
    stream.write(json.dumps([name, args]) + "\n")
scenario = os.environ["RESOLVER_SCENARIO"]
if name == "sleep":
    sys.exit(0)
if name == "timeout":
    assert args[:3] == ["10", "getent", "ahosts"], args
    if scenario == "timeout-first" and not any(c[0] == "timeout" for c in calls):
        print("10.99.99.99 STREAM partial.invalid")
        sys.exit(124)
    os.execvp(args[1], args[1:])
assert name == "getent" and args[0] == "ahosts", (name, args)
previous = sum(c == ["getent", args] for c in calls)
if scenario == "exhausted" or (scenario == "later-failure" and args[1] == "second.example"):
    print("10.99.99.99 STREAM partial.invalid")
    sys.exit(2)
if scenario == "failure-first" and previous == 0:
    print("10.99.99.99 STREAM partial.invalid")
    sys.exit(2)
if scenario == "empty" or (scenario == "empty-first" and previous == 0):
    sys.exit(0)
print("10.23.4.5 STREAM " + args[1])
print("10.23.4.5 DGRAM " + args[1])
"""


class EgressDns(unittest.TestCase):
    def resolve(self, scenario, targets="first.example:443\n", mode="sets"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowlist = root / "allowlist.txt"
            allowlist.write_text(targets)
            script = root / "private-endpoints"
            # Relocate only the fixed data path; execute the production script.
            script.write_text(
                (HARNESS / "lib/images/egress/private-endpoints")
                .read_text()
                .replace(
                    "ALLOWLIST=/opt/egress-sidecar/allowlist.txt",
                    f"ALLOWLIST='{allowlist}'",
                )
            )
            for name in ["timeout", "getent", "sleep"]:
                executable = root / name
                executable.write_text(f"#!{sys.executable}\n" + FAKE_COMMAND)
                executable.chmod(0o755)
            log = root / "calls.jsonl"
            result = subprocess.run(
                ["sh", str(script), mode],
                env=dict(
                    os.environ,
                    PATH=str(root) + os.pathsep + os.environ["PATH"],
                    RESOLVER_LOG=str(log),
                    RESOLVER_SCENARIO=scenario,
                ),
                capture_output=True,
                text=True,
                timeout=10,
            )
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(allowlist.read_text(), targets)
            return result, calls

    def test_success_does_not_retry_or_expand_targets(self):
        result, calls = self.resolve(
            "success", "first.example:443\n*.example\n10.0.0.0/8\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            calls,
            [
                ["timeout", ["10", "getent", "ahosts", "first.example"]],
                ["getent", ["ahosts", "first.example"]],
            ],
        )
        self.assertIn("10.23.4.5 . 443 timeout 90s", result.stdout)

    def test_transient_failure_timeout_and_empty_answer_recover(self):
        for scenario in ["failure-first", "timeout-first", "empty-first"]:
            with self.subTest(scenario=scenario):
                result, calls = self.resolve(scenario, mode="update")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(sum(c[0] == "timeout" for c in calls), 2)
                self.assertEqual(
                    [c for c in calls if c[0] == "sleep"], [["sleep", ["1"]]]
                )
                self.assertIn("10.23.4.5 . 443 timeout 90s", result.stdout)
                self.assertNotIn("10.99.99.99", result.stdout)
                self.assertEqual(result.stdout.count("flush set"), 4)

    def test_exhaustion_is_bounded_and_fail_closed(self):
        for scenario, message in [
            ("exhausted", "Cannot resolve allowlist endpoint: first.example"),
            ("empty", "No addresses for allowlist endpoint: first.example"),
        ]:
            with self.subTest(scenario=scenario):
                result, calls = self.resolve(scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr.strip(), message)
                self.assertEqual(sum(c[0] == "timeout" for c in calls), 3)
                self.assertEqual(sum(c[0] == "getent" for c in calls), 3)
                self.assertEqual(
                    [c for c in calls if c[0] == "sleep"], [["sleep", ["1"]]] * 2
                )

    def test_later_host_failure_emits_no_partial_policy(self):
        for mode in ["sets", "update"]:
            with self.subTest(mode=mode):
                result, calls = self.resolve(
                    "later-failure", "first.example:443\nsecond.example\n", mode
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn(
                    "Cannot resolve allowlist endpoint: second.example", result.stderr
                )
                self.assertEqual(
                    [c[1] for c in calls if c[0] == "getent"],
                    [["ahosts", "first.example"]] + [["ahosts", "second.example"]] * 3,
                )


if __name__ == "__main__":
    unittest.main()
