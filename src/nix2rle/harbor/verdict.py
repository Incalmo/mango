"""Check the exact Harbor job's completion separately from its CLI exit status."""

import json
import math
import os
import subprocess
import sys
import uuid
from pathlib import Path


def _read(path):
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected result object: {path}")
    return value


def _valid_rewards(rewards):
    return (
        isinstance(rewards, dict)
        and bool(rewards)
        and all(
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            for value in rewards.values()
        )
    )


def _check_steps(entry, expected, errors, path):
    from harbor.trial.multi_step import MultiStepTrial

    steps = entry["steps"]
    if expected is None:
        if steps:
            errors.append(
                f"Expected task steps are required to validate multi-step results: {path}"
            )
        return False
    names = [step["name"] for step in steps]
    declared = [step["name"] for step in expected]
    if names != declared[: len(names)] or len(names) > len(declared):
        errors.append(f"Step results do not match the declared order: {path}")
        return False
    valid = bool(steps) and all(_valid_rewards(step["rewards"]) for step in steps)
    for step in steps:
        if not _valid_rewards(step["rewards"]):
            errors.append(
                f"Missing or invalid step verifier rewards: {path}: {step['name']}"
            )
    stopped = False
    if valid:
        for index, step in enumerate(steps):
            threshold = expected[index].get("min_reward")
            if (
                threshold is not None
                and MultiStepTrial._min_reward_failure(step["rewards"], threshold)
                is not None
            ):
                if index != len(steps) - 1:
                    errors.append(
                        f"Unexpected steps after Harbor reward-threshold stop: {path}"
                    )
                else:
                    stopped = True
    if len(steps) != len(expected) and not stopped:
        errors.append(f"Missing declared step results: {path}")
        valid = False
    entry["step_completion"] = (
        "reward_threshold_stop" if stopped else "all_declared_steps"
    )
    return valid


def inspect_job(
    job: Path, agent: str, returncode: int | None, expected_steps=None
) -> dict:

    result_path = job / "result.json"
    report = {
        "job_directory": str(job),
        "result_path": str(result_path),
        "agent": agent,
        "expected_steps": expected_steps,
        "harbor_exit_code": returncode,
        "execution_complete": False,
        "qualification_evaluated": False,
        "errors": [],
        "stats": None,
        "trials": [],
    }
    errors = report["errors"]
    if returncode != 0:
        errors.append(f"Harbor process exited {returncode}")
    try:
        result = _read(result_path)
        report["stats"] = result.get("stats")
        if not result.get("finished_at") or result.get("n_total_trials") != 1:
            errors.append("Job is unfinished or does not contain exactly one trial")
        stats = result.get("stats")
        required = {
            "n_completed_trials": 1,
            "n_errored_trials": 0,
            "n_running_trials": 0,
            "n_pending_trials": 0,
            "n_cancelled_trials": 0,
        }
        if not isinstance(stats, dict) or any(
            stats.get(k) != v for k, v in required.items()
        ):
            errors.append("Job reports incomplete, cancelled or errored trials")
    except (OSError, ValueError) as error:
        errors.append(f"Missing or invalid job result: {error}")
    paths = sorted(job.glob("*/result.json"))
    if len(paths) != 1:
        errors.append(f"Expected one trial result, found {len(paths)}")
    for path in paths:
        try:
            trial = _read(path)
            if not trial.get("finished_at"):
                errors.append(f"Unfinished trial: {path}")
            if (trial.get("agent_info") or {}).get("name") != agent:
                errors.append(f"Trial agent does not match {agent}: {path}")
            entry = {
                "result_path": str(path),
                "trial_name": trial.get("trial_name"),
                "rewards": (trial.get("verifier_result") or {}).get("rewards"),
                "exception": trial.get("exception_info"),
                "steps": [],
            }
            report["trials"].append(entry)
            if entry["exception"] is not None:
                errors.append(f"Trial exception: {path}: {entry['exception']}")
            for step in trial.get("step_results") or []:
                entry["steps"].append(
                    {
                        "name": step.get("step_name"),
                        "rewards": (step.get("verifier_result") or {}).get("rewards"),
                        "exception": step.get("exception_info"),
                    }
                )
                if step.get("exception_info") is not None:
                    errors.append(
                        f"Step exception: {path}: {step.get('step_name')}: {step['exception_info']}"
                    )
            valid_steps = _check_steps(entry, expected_steps, errors, path)
            rewards = entry["rewards"]
            if not _valid_rewards(rewards):
                # Some result producers omit the redundant root aggregate. Only
                # accept that when every expected/executed step is proven complete.
                if rewards is not None or not valid_steps:
                    errors.append(f"Missing or invalid verifier rewards: {path}")
                else:
                    entry["reward_source"] = "step_results"
        except (OSError, ValueError, TypeError, AttributeError) as error:
            errors.append(f"Missing or invalid trial result: {path}: {error}")
    report["execution_complete"] = not errors
    return report


def run_trial(root: Path, work: Path, harness: Path, agent: str):
    from harbor.models.task.config import TaskConfig

    task = TaskConfig.model_validate_toml((work / "runtime/task.toml").read_text())
    expected_steps = [
        {"name": step.name, "min_reward": step.min_reward} for step in task.steps or []
    ]
    name = f"incalmo-{agent}-{uuid.uuid4().hex}"
    jobs = work / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    job = jobs / name
    if job.exists():
        raise ValueError(f"Refusing to reuse existing Harbor job: {job}")
    command = [
        "harbor",
        "run",
        "--path",
        str(work / "runtime"),
        "--environment-import-path",
        "nix2rle.harbor.environment:PreloadedDockerEnvironment",
        "--agent",
        agent,
        "--n-concurrent",
        "1",
        "--n-attempts",
        "1",
        "--max-retries",
        "0",
        "--jobs-dir",
        str(jobs),
        "--job-name",
        name,
    ]
    launch_error = None
    try:
        result = subprocess.run(
            command,
            cwd=root,
            check=False,
            stdout=sys.stderr,
            env=dict(os.environ, PYTHONPATH=str(harness / "src")),
        )
        returncode = result.returncode
    except OSError as error:
        returncode = None
        launch_error = str(error)
    report = inspect_job(job, agent, returncode, expected_steps)
    if launch_error is not None:
        report["errors"].append(f"Could not launch Harbor: {launch_error}")
    report["command"] = command
    report["verdict_path"] = str(jobs / f"{name}.verdict.json")
    rendered = json.dumps(report, indent=2) + "\n"
    Path(report["verdict_path"]).write_text(rendered)
    print(rendered, end="")
    if not report["execution_complete"]:
        raise ValueError(
            f"Harbor trial did not complete successfully; see {report['verdict_path']}"
        )
    return report
