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


if __name__ == "__main__":
    unittest.main()
