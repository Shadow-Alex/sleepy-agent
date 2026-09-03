from __future__ import annotations

import signal
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
    ) -> None:
        self.store = store or Store()
        self.tick_seconds = tick_seconds
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
    ) -> dict[str, int]:
        recovered = self.store.recover_stale()
        checked = 0

        for _ in range(max_checks):
            monitor = self.store.claim_due_check()
            if monitor is None:
                break
            self._execute_claimed_check(monitor)
            checked += 1

        return {"recovered": recovered, "checked": checked}

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
