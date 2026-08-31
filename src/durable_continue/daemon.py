from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from .checker import decide, run_checker
from .store import Store
from .util import now_ts


class DurableContinueDaemon:
    def __init__(
        self,
        *,
        store: Store | None = None,
        tick_seconds: int = 15,
        queue_retry_seconds: int = 60,
        delivery_timeout_seconds: int = 60,
    ) -> None:
        self.store = store or Store()
        self.tick_seconds = tick_seconds
        self.queue_retry_seconds = queue_retry_seconds
        self.delivery_timeout_seconds = delivery_timeout_seconds
        self._stop = False

    def request_stop(self, *_args: object) -> None:
        self._stop = True

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        while not self._stop:
            self.run_once()
            self._sleep_interruptibly(self.tick_seconds)

    def _sleep_interruptibly(self, seconds: int) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop and time.monotonic() < deadline:
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))

    def run_once(
        self,
        *,
        max_checks: int = 100,
        max_queue_workers: int = 100,
    ) -> dict[str, int]:
        recovered = self.store.recover_stale()
        checked = 0
        queued = 0

        for _ in range(max_checks):
            monitor = self.store.claim_due_check()
            if monitor is None:
                break
            self._execute_claimed_check(monitor)
            checked += 1

        for _ in range(max_queue_workers):
            monitor = self.store.claim_due_queue()
            if monitor is None:
                break
            self._spawn_queue_worker(monitor)
            queued += 1

        return {"recovered": recovered, "checked": checked, "queue_workers": queued}

    def _execute_claimed_check(self, monitor: dict[str, object]) -> None:
        observation = run_checker(
            str(monitor["check_command"]),
            cwd=str(monitor["cwd"]),
            timeout_seconds=int(monitor["check_timeout_seconds"]),
        )
        after = now_ts()
        decision = decide(
            observation,
            success_regex=str(monitor["success_regex"]),
            failure_regex=str(monitor["failure_regex"])
            if monitor.get("failure_regex")
            else None,
            deadline_due=after >= float(monitor["deadline_at"]),
        )
        self.store.complete_check(
            str(monitor["id"]),
            str(monitor["claim_token"]),
            decision,
            now=after,
        )

    def _spawn_queue_worker(self, monitor: dict[str, object]) -> None:
        monitor_id = str(monitor["id"])
        claim_token = str(monitor["claim_token"])
        log_path = self.store.logs_path / f"{monitor_id}.queue.log"
        command = [
            sys.executable,
            "-m",
            "durable_continue.cli",
            "_queue-worker",
            monitor_id,
            "--claim-token",
            claim_token,
            "--db",
            str(self.store.path),
            "--retry-seconds",
            str(self.queue_retry_seconds),
            "--delivery-timeout",
            str(self.delivery_timeout_seconds),
        ]
        descriptor = os.open(log_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "ab", buffering=0) as handle:
            try:
                proc = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=handle,
                    start_new_session=True,
                    close_fds=True,
                    env=os.environ.copy(),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                self.store.queue_retry(
                    monitor_id,
                    claim_token,
                    f"failed to spawn queue worker: {type(exc).__name__}: {exc}",
                    retry_seconds=self.queue_retry_seconds,
                )
                return
        if (
            not self.store.set_queue_worker_pid(monitor_id, claim_token, proc.pid)
            and proc.poll() is None
        ):
            # The worker may already have completed, or cancellation may have won.
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
