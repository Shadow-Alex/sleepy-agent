# Durable Continue

**Stop polling. Let the agent sleep.**

Durable Continue saves tokens by eliminating busy waiting. It lets Codex hand a
long experiment, download, build, test suite, or data job to a tiny local
daemon, end the current model turn, and continue in the exact same task when
the work succeeds, fails, or times out.

**Install once. Use Codex normally.**

```text
You:   Download this 80 GB dataset, verify its checksum, then preprocess it.
Codex: Starts the download and registers a read-only completion check.
       The download is running. I’ll continue here automatically when it
       finishes, fails, or times out.

       [No model polling while the local daemon waits]

Codex: Receives “continue” in the same task, verifies the download, and starts
       preprocessing.
```

## What it is for

- Large downloads followed by verification or processing
- Model training, experiments, backtests, and parameter sweeps
- Long builds, container builds, and full test suites
- Data pipelines, exports, and remote jobs with authoritative completion state

Durable Continue is deliberately one small primitive. It is not a task queue,
scheduler, dashboard, multi-agent orchestrator, remote fleet manager, quota
bypass, or approval bypass.

## Install

The first release is macOS-first and Codex-first. It requires Python 3.10 or
newer and a Codex executable for which `codex queue --help` succeeds.

```bash
git clone https://github.com/Shadow-Alex/durable-continue.git
cd durable-continue
./scripts/install_local.sh
```

No `sudo` is required. The installer places the runtime and state under
`~/.codex/durable-continue`, installs the Skill under
`~/.codex/skills/durable-continue`, creates `~/.local/bin/durable-continue`,
and starts one user LaunchAgent.

### skills.sh

The Skill can also be discovered and installed through the open Agent Skills
ecosystem:

```bash
npx skills add Shadow-Alex/durable-continue \
  --skill durable-continue --agent codex --global --yes
```

The skills.sh command installs the agent instructions only. The local daemon is
still required, so the full installer above is the recommended installation.

## Use

Ask Codex for the real task in ordinary language. Do not invoke the Skill or
run maintenance commands during normal use.

```text
Run the full historical backtest and summarize the result when it finishes.

Build the production image, then run the smoke tests.

Submit this remote training job and evaluate the best checkpoint when it is
done.
```

When Codex is blocked only on a genuine long wait, the Skill registers one
cheap read-only checker and ends the turn. A local LaunchAgent evaluates that
checker without model calls. On success, failure, or hard timeout, it queues
the exact message `continue` to the original Codex task. Codex then rechecks the
authoritative state and continues your objective.

Short commands, interactive prompts, indefinite services, and waits that still
leave useful work to do are intentionally excluded.

## Diagnostics and uninstall

These commands are for maintenance and troubleshooting:

```bash
durable-continue doctor
durable-continue list
durable-continue status <monitor-id>
durable-continue cancel <monitor-id>
./scripts/uninstall_local.sh
```

Uninstalling removes the daemon, CLI, and Skill while preserving durable state.
Pass `--purge` to the uninstall script to remove the owned state directory too.

## Reliability and privacy

- The daemon runs locally as the current user and opens no network listener.
- There is no telemetry and no transcript collection.
- Checkers are read-only, time-bounded, and have bounded stored output.
- The exact Codex task ID is captured at registration; there is no cwd-based
  guessing, new-task fallback, fork, or steering.
- Wake delivery is crash-safe and duplicate-resistant. An ambiguous transport
  failure can be retried, so strict exactly-once delivery is not claimed.
- Durable Continue does not broaden sandbox or approval permissions.

Durable Continue is an independent open-source project. It is not affiliated
with or endorsed by OpenAI.

## License

[MIT](LICENSE)
