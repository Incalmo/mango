"""A successful Harbor CLI process alone is not a completed trial."""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
from mango.harbor.verdict import inspect_job, run_trial


class TrialVerdictTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.job = self.work / "jobs/test"
        (self.work / "runtime").mkdir(parents=True)
        (self.work / "runtime/task.toml").write_text("[environment]\n")

    def write_result(self, job, agent="oracle", reward=1, exception=None, **changes):
        (job / "trial").mkdir(parents=True)
        result = {
            "finished_at": "2026-11-11T00:00:00Z",
            "n_total_trials": 1,
            "stats": {
                "n_completed_trials": 1,
                "n_errored_trials": int(exception is not None),
                "n_running_trials": 0,
                "n_pending_trials": 0,
                "n_cancelled_trials": 0,
            },
        }
        result.update(changes)
        (job / "result.json").write_text(json.dumps(result))
        (job / "trial/result.json").write_text(
            json.dumps(
                {
                    "trial_name": "trial",
                    "finished_at": "2026-11-11T00:00:00Z",
                    "agent_info": {"name": agent},
                    "exception_info": exception,
                    "verifier_result": {"rewards": {"reward": reward}}
                    if reward is not None
                    else None,
                }
            )
        )

    def invoke(self, agent, create, harbor_args=()):
        self.commands = []

        def harbor(command, **kwargs):
            self.commands.append(command)
            self.assertEqual(command[:2], ["harbor", "run"])
            self.assertEqual(command[command.index("--n-attempts") + 1], "1")
            self.assertEqual(command[command.index("--max-retries") + 1], "0")
            job = (
                Path(command[command.index("--jobs-dir") + 1])
                / command[command.index("--job-name") + 1]
            )
            create(job)
            return subprocess.CompletedProcess(command, 0)

        with (
            patch("mango.harbor.verdict.subprocess.run", side_effect=harbor),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return run_trial(self.root, self.work, REPO, agent, list(harbor_args))

    def test_zero_exit_infrastructure_exception_fails_and_retains_verdict(self):
        with self.assertRaisesRegex(ValueError, "did not complete successfully"):
            self.invoke(
                "oracle",
                lambda job: self.write_result(
                    job,
                    reward=None,
                    exception={
                        "exception_type": "FileNotFoundError",
                        "exception_message": "missing grader definition",
                    },
                ),
            )
        verdict = json.loads(
            next((self.work / "jobs").glob("*.verdict.json")).read_text()
        )
        self.assertFalse(verdict["execution_complete"])
        self.assertEqual(verdict["harbor_exit_code"], 0)
        self.assertTrue(any("Trial exception" in e for e in verdict["errors"]))

    def test_any_agent_with_passthrough_harbor_arguments(self):
        extra = ["--model", "provider/model", "--agent-kwarg", "reasoning=high"]
        result = self.invoke(
            "terminus-2",
            lambda job: self.write_result(job, agent="terminus-2", reward=0.5),
            harbor_args=extra,
        )
        (command,) = self.commands
        self.assertEqual(command[command.index("--agent") + 1], "terminus-2")
        self.assertEqual(command[-len(extra):], extra)
        self.assertEqual(result["trials"][0]["rewards"], {"reward": 0.5})

    def test_missing_result_does_not_reuse_an_old_success(self):
        self.write_result(self.job)
        with self.assertRaisesRegex(ValueError, "did not complete successfully"):
            self.invoke("oracle", lambda job: None)

    def test_successful_oracle_and_nop_report_actual_rewards(self):
        for agent, reward in [("oracle", 1), ("nop", 0), ("oracle", 0)]:
            result = self.invoke(
                agent, lambda job: self.write_result(job, agent=agent, reward=reward)
            )
            self.assertTrue(result["execution_complete"])
            self.assertFalse(result["qualification_evaluated"])
            self.assertEqual(result["trials"][0]["rewards"], {"reward": reward})
            self.assertTrue(Path(result["result_path"]).exists())

    def test_incomplete_and_missing_trial_are_rejected(self):
        self.write_result(self.job, finished_at=None)
        self.assertFalse(inspect_job(self.job, "oracle", 0)["execution_complete"])
        (self.job / "trial/result.json").unlink()
        self.assertFalse(inspect_job(self.job, "oracle", 0)["execution_complete"])

    def test_trial_exception_cannot_hide_behind_clean_job_counts(self):
        self.write_result(self.job)
        path = self.job / "trial/result.json"
        trial = json.loads(path.read_text())
        trial["step_results"] = [
            {"step_name": "grade", "exception_info": {"exception_type": "RuntimeError"}}
        ]
        path.write_text(json.dumps(trial))
        self.assertTrue(
            any(
                "Step exception" in e
                for e in inspect_job(self.job, "oracle", 0)["errors"]
            )
        )

    def test_pinned_harbor_multistep_models_and_missing_root_aggregate(self):
        from types import SimpleNamespace

        from harbor.models.trial.result import StepResult
        from harbor.models.verifier.result import VerifierResult
        from harbor.trial.trial import Trial

        steps = [
            StepResult(
                step_name=name,
                verifier_result=VerifierResult(rewards={"reward": reward}),
            )
            for name, reward in [("alpha", 1), ("beta", 0)]
        ]
        # Use the pinned Harbor aggregator/model output, not an invented wire shape.
        aggregate = Trial._aggregate_step_rewards(
            SimpleNamespace(result=SimpleNamespace(step_results=steps))
        )
        self.assertEqual(aggregate.rewards, {"reward": 0.5})
        self.write_result(self.job)
        path = self.job / "trial/result.json"
        trial = json.loads(path.read_text())
        trial["step_results"] = [step.model_dump(mode="json") for step in steps]
        expected = [{"name": "alpha"}, {"name": "beta"}]
        for root in [aggregate.model_dump(mode="json"), None]:
            trial["verifier_result"] = root
            path.write_text(json.dumps(trial))
            self.assertTrue(
                inspect_job(self.job, "oracle", 0, expected)["execution_complete"]
            )
        trial["step_results"][0]["verifier_result"] = None
        path.write_text(json.dumps(trial))
        self.assertFalse(
            inspect_job(self.job, "oracle", 0, expected)["execution_complete"]
        )

    def test_missing_steps_only_allowed_for_declared_reward_threshold_stop(self):
        self.write_result(self.job)
        path = self.job / "trial/result.json"
        trial = json.loads(path.read_text())
        trial["verifier_result"] = None
        trial["step_results"] = [
            {"step_name": "alpha", "verifier_result": {"rewards": {"reward": 0}}}
        ]
        path.write_text(json.dumps(trial))
        expected = [{"name": "alpha"}, {"name": "beta"}]
        self.assertFalse(
            inspect_job(self.job, "oracle", 0, expected)["execution_complete"]
        )
        expected[0]["min_reward"] = 1
        report = inspect_job(self.job, "oracle", 0, expected)
        self.assertTrue(report["execution_complete"])
        self.assertEqual(
            report["trials"][0]["step_completion"], "reward_threshold_stop"
        )
        expected[0]["name"] = "other"
        self.assertFalse(
            inspect_job(self.job, "oracle", 0, expected)["execution_complete"]
        )

    def test_nonzero_process_and_missing_rewards_fail(self):
        self.write_result(self.job, reward=None)
        result = inspect_job(self.job, "oracle", 1)
        self.assertFalse(result["execution_complete"])
        self.assertTrue(any("verifier rewards" in e for e in result["errors"]))
        self.assertTrue(any("process exited" in e for e in result["errors"]))


if __name__ == "__main__":
    unittest.main()
