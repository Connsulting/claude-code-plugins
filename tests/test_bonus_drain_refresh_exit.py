"""A failed refresh names the stale accounts, so a new failure is told apart from a known one.

``bonus-drain refresh`` exits 1 whenever any account is stale, which keeps the systemd unit
failed for as long as one account stays broken. That exit must stay loud: a stale account
closes its gate and a human must see it. These contracts add the missing half: every failed
refresh writes one stderr summary that names each stale account and its error, in a stable
order, so the journal and ``systemctl status`` change when a second account breaks or the
known one starts failing differently.
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
import types
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

from tests.readiness_fixture import isolated_cli

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain import cli, usage

NOW = 2_000_000_000
GROK_ERROR = (
    "adapter grok-personal-usage exited 1: bonus-drain-grok-usage: "
    "weekly_percent must be a number"
)
CODEX_ERROR = "adapter codex-personal-usage exited 1: collector timeout"


def _snapshot(provider: str, account: str, error: str | None) -> usage.UsageSnapshot:
    return usage.UsageSnapshot(
        provider, account, NOW,
        {"seven_day": {"used_percent": 10.0, "resets_at": NOW + 3600}},
        fresh=error is None, error=error,
    )


class RefreshExitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cfg = types.SimpleNamespace(database=Path(self.temporary.name) / "queue.db")

    @contextlib.contextmanager
    def _refresh(self, *snapshots: usage.UsageSnapshot) -> Iterator[list[tuple[object, object]]]:
        records = {(item.provider_id, item.account_id): item for item in snapshots}
        payloads: list[tuple[object, object]] = []
        with (
            mock.patch.object(cli, "_load_config", return_value=self.cfg),
            mock.patch.object(usage, "refresh_all", return_value=records),
            mock.patch.object(
                cli, "_json",
                side_effect=lambda value, **kwargs: payloads.append(
                    (value, kwargs.get("stream", sys.stdout)),
                ),
            ),
        ):
            yield payloads

    def _run(self, *snapshots: usage.UsageSnapshot) -> tuple[int, list[object], list[object]]:
        with self._refresh(*snapshots) as payloads:
            code = isolated_cli(["refresh", "--json", "--now", str(NOW)])
        stdout = [value for value, stream in payloads if stream is not sys.stderr]
        stderr = [value for value, stream in payloads if stream is sys.stderr]
        return code, stdout, stderr

    def test_one_stale_account_fails_loudly_and_is_named(self) -> None:
        code, stdout, stderr = self._run(
            _snapshot("claude", "claude-personal", None),
            _snapshot("grok", "grok-personal", GROK_ERROR),
        )

        self.assertEqual(code, 1)
        self.assertEqual(len(stdout), 1)
        self.assertEqual(
            [item["account_id"] for item in stdout[0]["accounts"]],
            ["claude-personal", "grok-personal"],
        )
        self.assertEqual(stderr, [{
            "ok": False,
            "code": "stale_accounts",
            "error": "1 of 2 accounts stale: grok/grok-personal",
            "stale": [
                {"provider_id": "grok", "account_id": "grok-personal", "error": GROK_ERROR},
            ],
        }])

    def test_a_second_stale_account_changes_the_summary(self) -> None:
        _, _, known = self._run(
            _snapshot("claude", "claude-personal", None),
            _snapshot("codex", "codex-personal", None),
            _snapshot("grok", "grok-personal", GROK_ERROR),
        )
        code, _, new = self._run(
            _snapshot("grok", "grok-personal", GROK_ERROR),
            _snapshot("claude", "claude-personal", None),
            _snapshot("codex", "codex-personal", CODEX_ERROR),
        )

        self.assertEqual(code, 1)
        self.assertNotEqual(known, new)
        self.assertEqual(new[0]["error"], "2 of 3 accounts stale: codex/codex-personal, grok/grok-personal")
        self.assertEqual(
            [(item["account_id"], item["error"]) for item in new[0]["stale"]],
            [("codex-personal", CODEX_ERROR), ("grok-personal", GROK_ERROR)],
        )

    def test_a_known_account_failing_differently_changes_the_summary(self) -> None:
        _, _, before = self._run(_snapshot("grok", "grok-personal", GROK_ERROR))
        _, _, after = self._run(
            _snapshot("grok", "grok-personal", "adapter grok-personal-usage exited 1: collector exited 22"),
        )

        self.assertNotEqual(before, after)

    def test_a_stale_account_without_an_error_is_still_named(self) -> None:
        code, _, stderr = self._run(
            usage.UsageSnapshot("grok", "grok-personal", NOW, {}, fresh=False, error=None),
        )

        self.assertEqual(code, 1)
        self.assertEqual(stderr[0]["stale"][0]["error"], "stale")

    def test_an_all_fresh_refresh_succeeds_without_a_summary(self) -> None:
        code, stdout, stderr = self._run(
            _snapshot("claude", "claude-personal", None),
            _snapshot("grok", "grok-personal", None),
        )

        self.assertEqual(code, 0)
        self.assertEqual(len(stdout), 1)
        self.assertEqual(stderr, [])


if __name__ == "__main__":
    unittest.main()
