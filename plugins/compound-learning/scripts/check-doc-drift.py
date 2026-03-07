#!/usr/bin/env python3
"""Detect doc drift between compound-learning README and plugin files."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

PLUGIN_RELATIVE_PREFIX = "plugins/compound-learning/"
IGNORED_REFERENCE_PREFIXES = ("~", "/", "http://", "https://", "${", "$HOME", "[repo]/", ".claude/")


def _sorted(values: Iterable[str]) -> List[str]:
    return sorted(set(values))


def discover_commands(plugin_root: Path) -> Set[str]:
    commands_dir = plugin_root / "commands"
    if not commands_dir.exists():
        return set()
    return {path.stem for path in commands_dir.glob("*.md") if path.is_file()}


def discover_agents(plugin_root: Path) -> Set[str]:
    agents_dir = plugin_root / "agents"
    if not agents_dir.exists():
        return set()
    return {path.stem for path in agents_dir.glob("*.md") if path.is_file()}


def discover_skills(plugin_root: Path) -> Set[str]:
    skills_dir = plugin_root / "skills"
    if not skills_dir.exists():
        return set()

    discovered: Set[str] = set()
    for skill_dir in skills_dir.iterdir():
        if skill_dir.is_dir() and (skill_dir / "SKILL.md").is_file():
            discovered.add(skill_dir.name)
    return discovered


def discover_hook_events(plugin_root: Path) -> Tuple[Set[str], str | None]:
    hooks_config = plugin_root / "hooks" / "hooks.json"
    if not hooks_config.is_file():
        return set(), None

    try:
        payload = json.loads(hooks_config.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        error = (
            f"invalid JSON in hooks/hooks.json: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})"
        )
        return set(), error

    if not isinstance(payload, dict):
        return set(), "invalid hooks/hooks.json: top-level value must be an object"

    hooks = payload.get("hooks", {})
    if not isinstance(hooks, dict):
        return set(), "invalid hooks/hooks.json: 'hooks' must be an object"
    return set(hooks.keys()), None


def extract_documented_components(readme_text: str) -> Dict[str, Set[str]]:
    documented = {
        "commands": set(),
        "agents": set(),
        "skills": set(),
        "hooks": set(),
    }

    section_markers = {
        "- **Commands:**": "commands",
        "- **Agents:**": "agents",
        "- **Skills:**": "skills",
        "- **Hooks:**": "hooks",
    }

    in_components = False
    current_section: str | None = None
    for raw_line in readme_text.splitlines():
        stripped = raw_line.strip()
        if stripped == "### Components":
            in_components = True
            current_section = None
            continue
        if not in_components:
            continue
        if stripped.startswith("### ") and stripped != "### Components":
            break

        if stripped in section_markers:
            current_section = section_markers[stripped]
            continue

        if stripped.startswith("- **") and stripped.endswith("**:"):
            current_section = None
            continue

        if current_section is None or not stripped.startswith("- "):
            continue

        if current_section == "commands":
            matches = re.findall(r"`/([a-z0-9][a-z0-9-]*)`", stripped)
            if not matches:
                matches = re.findall(r"/([a-z0-9][a-z0-9-]*)", stripped)
            documented["commands"].update(matches)
            continue

        if current_section in {"agents", "skills"}:
            documented[current_section].update(
                re.findall(r"`([a-z0-9][a-z0-9-]*)`", stripped)
            )
            continue

        if current_section == "hooks":
            code_matches = re.findall(r"`([A-Za-z][A-Za-z0-9_-]*)`", stripped)
            bold_matches = re.findall(r"\*\*([A-Za-z][A-Za-z0-9_-]*)\*\*", stripped)
            documented["hooks"].update(code_matches)
            documented["hooks"].update(bold_matches)

    return documented


def extract_backticked_local_references(readme_text: str) -> List[str]:
    references: Set[str] = set()

    for raw_ref in re.findall(r"`([^`\n]+)`", readme_text):
        ref = raw_ref.strip().rstrip(".,:;)")
        if not ref or " " in ref:
            continue

        normalized = ref.replace("\\", "/")
        if normalized.startswith("./"):
            normalized = normalized[2:]

        if normalized.endswith("/") or "/" not in normalized:
            continue
        if normalized.startswith(IGNORED_REFERENCE_PREFIXES):
            continue
        if "*" in normalized:
            continue

        references.add(normalized)

    return _sorted(references)


def resolve_local_reference(ref: str, plugin_root: Path, repo_root: Path) -> Path | None:
    candidates: List[Path] = []

    if ref.startswith(PLUGIN_RELATIVE_PREFIX):
        candidates.append(repo_root / ref)
        trimmed = ref[len(PLUGIN_RELATIVE_PREFIX) :]
        candidates.append(plugin_root / trimmed)
    else:
        candidates.append(plugin_root / ref)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def compare_component(
    title: str,
    documented: Set[str],
    actual: Set[str],
) -> List[str]:
    lines: List[str] = []
    documented_only = _sorted(documented - actual)
    undocumented = _sorted(actual - documented)

    if documented_only or undocumented:
        lines.append(f"{title}:")
    if documented_only:
        lines.append(f"  documented but missing from filesystem: {', '.join(documented_only)}")
    if undocumented:
        lines.append(f"  present in filesystem but undocumented: {', '.join(undocumented)}")

    return lines


def run_check(plugin_root: Path, readme_path: Path) -> int:
    if not readme_path.is_file():
        print(f"README not found: {readme_path}")
        return 1

    readme_text = readme_path.read_text(encoding="utf-8")
    documented = extract_documented_components(readme_text)
    hook_events, hook_error = discover_hook_events(plugin_root)
    actual = {
        "commands": discover_commands(plugin_root),
        "agents": discover_agents(plugin_root),
        "skills": discover_skills(plugin_root),
        "hooks": hook_events,
    }

    repo_root = plugin_root.parent.parent if plugin_root.parent.parent else plugin_root
    missing_references: List[str] = []
    for ref in extract_backticked_local_references(readme_text):
        if resolve_local_reference(ref, plugin_root, repo_root) is None:
            missing_references.append(ref)

    report_lines: List[str] = []
    report_lines.extend(compare_component("Commands", documented["commands"], actual["commands"]))
    report_lines.extend(compare_component("Agents", documented["agents"], actual["agents"]))
    report_lines.extend(compare_component("Skills", documented["skills"], actual["skills"]))
    report_lines.extend(compare_component("Hook events", documented["hooks"], actual["hooks"]))
    if hook_error:
        report_lines.append("Hook configuration:")
        report_lines.append(f"  {hook_error}")

    if missing_references:
        report_lines.append("Local file references:")
        report_lines.append(
            "  referenced in README but missing from filesystem: "
            + ", ".join(_sorted(missing_references))
        )

    if report_lines:
        print(f"Documentation drift detected for {readme_path}:")
        for line in report_lines:
            print(f"- {line}" if not line.startswith("  ") else line)
        return 1

    print(f"No documentation drift detected for {readme_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Check README/component drift for compound-learning plugin.")
    parser.add_argument(
        "--plugin-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Path to plugin root (defaults to this script's plugin directory).",
    )
    parser.add_argument(
        "--readme",
        type=Path,
        default=None,
        help="Optional explicit README path (defaults to <plugin-root>/README.md).",
    )

    args = parser.parse_args()
    plugin_root = args.plugin_root.resolve()
    readme_path = args.readme.resolve() if args.readme else (plugin_root / "README.md").resolve()
    return run_check(plugin_root, readme_path)


if __name__ == "__main__":
    sys.exit(main())
