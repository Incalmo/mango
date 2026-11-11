# Inspect local trial results

`--agent` accepts `oracle`, `nop` or any agent the pinned Harbor provides.
Arguments after a bare `--` are passed to `harbor run` unchanged, for example
`-- --model provider/model --agent-kwarg key=value --allow-agent-host host`.
The harness adds only the task path, the prebuilt-image environment class and
the job location; credentials and model settings stay with the caller.

`mango ENV run --agent AGENT [-- HARBOR_ARGS...]` runs one trial in a fresh, uniquely named
Harbor job with retries disabled. It does not inspect the newest directory or
reuse a result from a previous invocation. Harbor output goes to stderr; stdout
contains the structured verdict JSON.

The command reads the exact job result and its trial result after Harbor exits.
It exits nonzero for a nonzero Harbor process, missing/malformed results,
unfinished/pending/cancelled/errored trials, trial or step exceptions, or absent
verifier rewards. Harbor returning zero while recording an infrastructure
exception is therefore a failure.

For multi-step tasks, the runner snapshots declared step names and reward
thresholds before launch. Every executed step must have valid rewards and no
exception, in declared order. Missing trailing steps are accepted only for an
intentional Harbor `min_reward` threshold stop; missing intermediate grading
cannot hide behind an aggregate reward. A redundant root reward may be absent
only when the validated step results establish completion. Rewards are reported
without inventing an aggregate.

The JSON contains the job/result paths, observed Harbor exit code, job statistics,
trial and step rewards/exceptions, and `execution_complete`. The same JSON is
saved beside the job under `<workspace>/jobs/<job-name>.verdict.json`, including
when validation fails. Original Harbor logs and results remain under the job.

`execution_complete: true` means execution and grading produced a result without
an infrastructure exception. It does not mean the reward meets the task's pass
condition. A completed oracle with reward zero is still reported as reward zero;
a completed nop with reward zero is not confused with an infrastructure failure.
Read actual rewards and the task-specific pass condition before approving an
oracle, negative control, or qualification gate. The runner always reports
`qualification_evaluated: false`; it never updates qualification records.

Clean separate-grader tasks do not need Dockerfiles or authored Compose files.
Materialization supplies a conventional image-only Compose definition for
Harbor's initial validation, while each grader variant still uses its own
image-only Compose file and image manifest during execution.
