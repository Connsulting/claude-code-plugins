"""An automatic recovery that repeats its source's blocker on an unchanged contract holds.

The first automatic verification retry earns its place: across the queue's history it turned
2 of 12 verification_needed failures into done. A second automatic retry after the same
reason code on the same contract never has (0 of 5), and every such failure this week
(curie-v0123-4167, curie-4367) differed from the one before only in the worker's wording,
because a signature the worker omits is derived from free-text detail. These contracts hold
that second retry as no_progress whatever the wording, while leaving the first verification
retry, retryable work, and a change of reason code alone. Operator requeue still releases the
hold, as test_bonus_drain_override_hold covers for every no_progress hold.
"""

from __future__ import annotations

import unittest

from tests.test_bonus_dependency_recovery import (
    NOW,
    RecoveryCase,
    as_dict,
    reason,
    rows,
)


class NoProgressOnUnchangedContract(RecoveryCase):
    def chain(self, task_id: str, code: str, signatures: tuple[str, ...]) -> None:
        """Fail a normal attempt, then one automatic recovery per remaining signature."""
        self.add(task_id)
        self.add(f"{task_id}-child", depends_on=[task_id])
        attempt = self.claim(task_id)
        self.terminal(task_id, attempt, "failed", reason(code, signatures[0]))
        now = NOW
        for signature in signatures[1:]:
            self.queue.reconcile_recoveries(now_epoch=now, dry_run=False)
            now += 1_800
            retry = self.claim(task_id, automatic=True, now=now)
            self.terminal(task_id, retry, "failed", reason(code, signature), now=now)
        self.now = now

    def automatic_attempts(self, task_id: str) -> int:
        return len(rows(
            self.queue,
            "SELECT * FROM task_attempts WHERE task_id=? AND origin='automatic'",
            (task_id,),
        ))

    def recovery(self, task_id: str) -> dict[str, object]:
        return as_dict(self.queue.reconcile_recoveries(now_epoch=self.now, dry_run=False)[0])

    def test_reworded_repeat_of_a_verification_blocker_holds_before_a_third_attempt(self) -> None:
        self.chain("schema-window", "verification_needed", (
            "verification_needed:candidate:preserved:mandatory:schema:window",
            "verification_needed:schema:window:blocker:persists:on:current:main",
        ))

        held = self.recovery("schema-window")

        self.assertEqual((held["state"], held["reason_code"]), ("held", "no_progress"))
        self.assertIsNone(held["not_before"])
        self.assertEqual(self.automatic_attempts("schema-window"), 1)

    def test_first_verification_retry_is_still_scheduled(self) -> None:
        self.chain("first-retry", "verification_needed", ("verification_needed:fixture",))

        first = self.recovery("first-retry")

        self.assertEqual(first["state"], "backoff")
        self.assertIsNotNone(first["not_before"])

    def test_reworded_retryable_failure_keeps_its_remaining_budget(self) -> None:
        self.chain("flaky-network", "retryable", (
            "retryable:registry:timeout",
            "retryable:docker:hub:pull:rate:limited",
        ))

        second = self.recovery("flaky-network")

        self.assertEqual(second["state"], "backoff")

    def test_a_different_reason_code_is_progress(self) -> None:
        self.add("moved-on")
        self.add("moved-on-child", depends_on=["moved-on"])
        attempt = self.claim("moved-on")
        self.terminal("moved-on", attempt, "failed", reason("retryable", "retryable:ci:runner"))
        self.queue.reconcile_recoveries(now_epoch=NOW, dry_run=False)
        retry = self.claim("moved-on", automatic=True, now=NOW + 300)
        self.terminal(
            "moved-on", retry, "failed",
            reason("verification_needed", "verification_needed:baseline:hang"), now=NOW + 300,
        )
        self.now = NOW + 300

        second = self.recovery("moved-on")

        self.assertEqual(second["state"], "backoff")


if __name__ == "__main__":
    unittest.main()
