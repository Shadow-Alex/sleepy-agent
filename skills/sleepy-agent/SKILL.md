---
name: sleepy-agent
description: Use on macOS Codex Desktop after an already-started local or remote task will outlive the active turn and progress is blocked only on waiting. Register one cheap read-only checker, regex conditions, and a hard timeout; then stop polling. The same task later receives the exact user message `continue` through the Desktop-owned dispatcher. Requires the full Sleepy Agent runtime; a skill-only install cannot deliver.
license: MIT
---

# Sleepy Agent

Use this only after the long-running task has survived a bounded startup guard.

1. Check long enough to catch immediate launch, path, dependency, permission, SSH, empty-log, and resource failures.
2. Do not invent a job abstraction merely because the task is long-running.
3. Build one cheap, read-only checker from an existing PID, log, marker file, scheduler or relay ID, process query, or SSH status command.
4. Register it:

```bash
durable-continue register \
  --check '<command>' \
  --success '<regex>' \
  [--failure '<regex>'] \
  --timeout <duration> \
  [--interval <duration>]
```

5. After registration succeeds, stop polling and end the current turn normally.
6. Do not create a Scheduled Task for the same wait.
7. Do not create or fork another task.
8. The original task will later receive the exact user message `continue`.

The checker must not mutate, retry, or resubmit the long-running task. A job ID is not required. Delivery is performed only by the dispatcher process launched inside Codex Desktop; never start a separate Codex server or automate the Desktop UI. If `durable-continue` is unavailable or registration reports that Desktop delivery is unavailable, stop and explain that the full repository installer is required.
