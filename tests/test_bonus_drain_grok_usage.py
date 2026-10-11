"""Direct-collection tests for the Grok weekly usage collector.

The live collector talks to xAI only when a temp HOME contains auth.json. These
tests cover the disk-log fallback with no credentials, including SuperGrok Heavy.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = (
    REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain" / "grok-usage.sh"
)

EVENT_TS = "2026-08-21T16:35:37Z"
NOW = int(datetime(2026, 8, 21, 16, 40, tzinfo=timezone.utc).timestamp())
RESET = int(datetime(2026, 8, 22, 0, 0, tzinfo=timezone.utc).timestamp())


def _event(tier: str, percent) -> str:
    config = {
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-08-15T00:00:00+00:00",
            "end": "2026-08-22T00:00:00+00:00",
        }
    }
    if percent is not None:
        config["creditUsagePercent"] = percent
    return json.dumps(
        {
            "ts": EVENT_TS,
            "msg": "billing: fetched credits config",
            "ctx": {"subscriptionTier": tier, "config": config},
        },
        separators=(",", ":"),
    )


class GrokUsageCollectorTests(unittest.TestCase):
    def collect(self, event: str) -> dict:
        self.assertTrue(COLLECTOR.is_file(), f"collector missing: {COLLECTOR}")
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            log = home / "unified.jsonl"
            log.write_text(event + "\n", encoding="utf-8")
            env = {
                "HOME": str(home),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "GROK_USAGE_LOG": str(log),
                "GROK_USAGE_NOW_EPOCH": str(NOW),
                "GROK_USAGE_MAX_AGE": "3600",
            }
            completed = subprocess.run(
                [str(COLLECTOR), "--direct-collect"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(
                completed.returncode,
                0,
                f"collector failed: {completed.stderr}",
            )
            return json.loads(completed.stdout)

    def test_plus_weekly_percent_is_still_accepted(self) -> None:
        reading = self.collect(_event("SuperGrok Plus", 37.5))
        self.assertEqual(reading["tier"], "SuperGrok Plus")
        self.assertEqual(reading["weekly_percent"], 37.5)
        self.assertEqual(reading["weekly_reset"], RESET)

    def test_heavy_weekly_percent_is_accepted(self) -> None:
        reading = self.collect(_event("SuperGrok Heavy", 1.0))
        self.assertEqual(reading["tier"], "SuperGrok Heavy")
        self.assertEqual(reading["weekly_percent"], 1.0)
        self.assertEqual(reading["weekly_reset"], RESET)

    def test_free_tier_is_not_treated_as_capacity(self) -> None:
        reading = self.collect(_event("Free", 12.0))
        self.assertEqual(reading["tier"], "Free")
        self.assertIsNone(reading["weekly_percent"])
        self.assertIsNone(reading["weekly_reset"])

    def test_heavy_missing_percent_stays_unknown(self) -> None:
        reading = self.collect(_event("SuperGrok Heavy", None))
        self.assertEqual(reading["tier"], "SuperGrok Heavy")
        self.assertIsNone(reading["weekly_percent"])
        self.assertEqual(reading["weekly_reset"], RESET)

    def test_missing_percent_reports_the_provider_period_start(self) -> None:
        reading = self.collect(_event("SuperGrok Heavy", None))
        self.assertIsNone(reading["weekly_percent"])
        self.assertEqual(reading["weekly_period_start"], PERIOD_START)

    def test_free_tier_reports_no_period_start(self) -> None:
        reading = self.collect(_event("Free", None))
        self.assertIsNone(reading["weekly_period_start"])


NORMALIZER = (
    REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain" / "bin"
    / "bonus-drain-grok-usage"
)
PERIOD_START = int(datetime(2026, 8, 15, 0, 0, tzinfo=timezone.utc).timestamp())


class GrokUsageNormalizerTests(unittest.TestCase):
    """The normalizer turns one collector reading into the Bonus Drain usage contract."""

    def normalize(self, reading: dict, *extra: str) -> subprocess.CompletedProcess:
        self.assertTrue(NORMALIZER.is_file(), f"normalizer missing: {NORMALIZER}")
        with tempfile.TemporaryDirectory() as raw_root:
            collector = Path(raw_root) / "collector"
            collector.write_text(
                "#!/bin/sh\ncat <<'JSON'\n" + json.dumps(reading) + "\nJSON\n",
                encoding="utf-8",
            )
            collector.chmod(0o700)
            return subprocess.run(
                [
                    "python3", str(NORMALIZER),
                    "--provider", "grok", "--account", "grok-personal",
                    "--collector", str(collector), "--limit", "grok-personal-weekly",
                    "--max-age", "3600", "--timeout", "10", "--now", str(NOW),
                    *extra,
                ],
                check=False,
                capture_output=True,
                text=True,
            )

    @staticmethod
    def reading(percent=None, *, period_start=PERIOD_START, reset=RESET) -> dict:
        return {
            "tier": "SuperGrok Heavy",
            "weekly_percent": percent,
            "weekly_reset": reset,
            "weekly_period_start": period_start,
            "source_ts": EVENT_TS,
        }

    def limit(self, completed: subprocess.CompletedProcess) -> dict:
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)["limits"]["grok-personal-weekly"]

    def test_absent_percent_stays_fail_closed_by_default(self) -> None:
        completed = self.normalize(self.reading())
        self.assertEqual(completed.returncode, 1)
        self.assertIn("weekly_percent must be a number", completed.stderr)

    def test_opt_in_reports_the_first_provider_step_flagged_unreported(self) -> None:
        limit = self.limit(self.normalize(self.reading(), "--unreported-percent-probe"))
        self.assertEqual(
            limit,
            {"used_percent": 1.0, "resets_at": RESET, "unreported_since": PERIOD_START},
        )

    def test_opt_in_never_replaces_a_reported_percent(self) -> None:
        limit = self.limit(self.normalize(self.reading(37.0), "--unreported-percent-probe"))
        self.assertEqual(limit, {"used_percent": 37.0, "resets_at": RESET})

    def test_opt_in_without_a_period_start_stays_closed(self) -> None:
        completed = self.normalize(
            self.reading(period_start=None), "--unreported-percent-probe",
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("weekly_period_start", completed.stderr)

    def test_opt_in_rejects_a_period_that_is_not_exactly_one_week(self) -> None:
        completed = self.normalize(
            self.reading(period_start=PERIOD_START + 3600), "--unreported-percent-probe",
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("one weekly period", completed.stderr)

    def test_opt_in_rejects_a_period_that_has_not_started(self) -> None:
        completed = self.normalize(
            self.reading(period_start=NOW + 60, reset=NOW + 60 + 604_800),
            "--unreported-percent-probe",
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("has not started", completed.stderr)


if __name__ == "__main__":
    unittest.main()
