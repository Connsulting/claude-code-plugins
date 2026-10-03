"""A minimal valid queue-time readiness review for test fixtures.

Queue adds require a readiness review. Tests that are not about the review attach this
minimal one: the issue read (derived from an issue source_ref, else none), no ADRs, the
governing AGENTS.md, one acceptance criterion taken from the task, and no findings.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "plugins" / "bonus-drain" / "skills" / "bonus-drain"
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from bonus_drain.checks import parse_issue_ref  # noqa: E402


def _issue(source_ref: Any) -> str | None:
    parsed = parse_issue_ref(source_ref if isinstance(source_ref, str) else None)
    return None if parsed is None else f"{parsed[0]}#{parsed[1]}"


def minimal_review(
    *, source_ref: Any = None, done_when: Any = None, goal: Any = None,
) -> dict[str, Any]:
    return {
        "issue": _issue(source_ref),
        "adrs": [],
        "instructions": ["AGENTS.md"],
        "acceptance_criteria": [
            {"criterion": done_when or goal or "fixture criterion", "basis": "test fixture"},
        ],
        "findings": [],
    }


def reviewed(values: Mapping[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``values`` carrying a minimal valid readiness review."""

    copy = dict(values)
    if "readiness_review" not in copy:
        copy["readiness_review"] = minimal_review(
            source_ref=copy.get("source_ref"), done_when=copy.get("done_when"), goal=copy.get("goal"),
        )
    return copy


def rereviewed(current: Any, changes: Mapping[str, Any]) -> dict[str, Any]:
    """``changes`` plus a minimal review of the contract they produce.

    A contract edit must carry a fresh review; ``current`` is the task (or its dict)
    before the edit, so the review's issue and criterion match the edited contract.
    """

    base = current.to_dict() if hasattr(current, "to_dict") else dict(current)
    merged = {**base, **changes}
    copy = dict(changes)
    copy.setdefault("readiness_review", minimal_review(
        source_ref=merged.get("source_ref"), done_when=merged.get("done_when"), goal=merged.get("goal"),
    ))
    return copy


def review_json(**overrides: Any) -> str:
    """The minimal review as CLI JSON text; ``source_ref``/``done_when``/``goal`` derive fields."""

    derive = {key: overrides.pop(key) for key in ("source_ref", "done_when", "goal") if key in overrides}
    value = minimal_review(**derive)
    value.update(overrides)
    return json.dumps(value, separators=(",", ":"))


def _flag(argv: Sequence[str], name: str) -> str | None:
    found = None
    for index, item in enumerate(argv):
        if item == name and index + 1 < len(argv):
            found = argv[index + 1]
    return found


def review_args(argv: Sequence[str]) -> tuple[str, ...]:
    """``--readiness-review <json>`` consistent with a CLI add argv (its source ref and goal)."""

    return ("--readiness-review", review_json(
        source_ref=_flag(argv, "--source-ref"),
        done_when=_flag(argv, "--done-when"),
        goal=_flag(argv, "--goal"),
    ))
