import json
import subprocess
import sys
import textwrap
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "check-doc-drift.py"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


def _run_checker(plugin_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--plugin-root", str(plugin_root)],
        check=False,
        capture_output=True,
        text=True,
    )


def _build_minimal_plugin(
    plugin_root: Path,
    readme_text: str,
    *,
    include_plugin_config: bool = True,
) -> None:
    _write(plugin_root / "README.md", readme_text)
    _write(plugin_root / "commands" / "compound.md", "# command\n")
    _write(plugin_root / "agents" / "learning-writer.md", "# agent\n")
    _write(plugin_root / "skills" / "index-learnings" / "SKILL.md", "# skill\n")
    _write(plugin_root / "scripts" / "search-learnings.py", "print('ok')\n")
    if include_plugin_config:
        _write(plugin_root / ".claude-plugin" / "config.json", "{}\n")
    _write(
        plugin_root / "hooks" / "hooks.json",
        json.dumps({"hooks": {"SessionStart": []}}, indent=2) + "\n",
    )


def test_doc_drift_checker_baseline_passes():
    proc = _run_checker(PLUGIN_ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "No documentation drift detected" in proc.stdout


def test_doc_drift_checker_detects_component_mismatch(tmp_path):
    plugin_root = tmp_path / "compound-learning"
    _build_minimal_plugin(
        plugin_root,
        """
        # Compound Learning Plugin

        See `scripts/search-learnings.py`.

        ## Architecture

        ### Components

        - **Commands:**
          - `/ghost`: missing command

        - **Agents:**
          - `learning-writer`: writes learnings

        - **Skills:**
          - `index-learnings`: indexes learnings

        - **Hooks:**
          - `SessionStart`: setup
        """,
    )

    proc = _run_checker(plugin_root)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "Commands:" in output
    assert "ghost" in output
    assert "compound" in output


def test_doc_drift_checker_detects_missing_local_reference(tmp_path):
    plugin_root = tmp_path / "compound-learning"
    _build_minimal_plugin(
        plugin_root,
        """
        # Compound Learning Plugin

        Existing script: `scripts/search-learnings.py`
        Missing script: `scripts/missing-script.py`
        Missing config: `.claude-plugin/missing-config.json`

        ## Architecture

        ### Components

        - **Commands:**
          - `/compound`: extracts learnings

        - **Agents:**
          - `learning-writer`: writes learnings

        - **Skills:**
          - `index-learnings`: indexes learnings

        - **Hooks:**
          - `SessionStart`: setup
        """,
    )

    proc = _run_checker(plugin_root)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "Local file references:" in output
    assert ".claude-plugin/missing-config.json" in output
    assert "scripts/missing-script.py" in output


def test_doc_drift_checker_checks_dot_claude_plugin_references(tmp_path):
    plugin_root = tmp_path / "compound-learning"
    _build_minimal_plugin(
        plugin_root,
        """
        # Compound Learning Plugin

        Config path: `.claude-plugin/config.json`

        ## Architecture

        ### Components

        - **Commands:**
          - `/compound`: extracts learnings

        - **Agents:**
          - `learning-writer`: writes learnings

        - **Skills:**
          - `index-learnings`: indexes learnings

        - **Hooks:**
          - `SessionStart`: setup
        """,
        include_plugin_config=False,
    )

    proc = _run_checker(plugin_root)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "Local file references:" in output
    assert ".claude-plugin/config.json" in output


def test_doc_drift_checker_handles_malformed_hooks_json(tmp_path):
    plugin_root = tmp_path / "compound-learning"
    _build_minimal_plugin(
        plugin_root,
        """
        # Compound Learning Plugin

        Existing script: `scripts/search-learnings.py`

        ## Architecture

        ### Components

        - **Commands:**
          - `/compound`: extracts learnings

        - **Agents:**
          - `learning-writer`: writes learnings

        - **Skills:**
          - `index-learnings`: indexes learnings

        - **Hooks:**
          - `SessionStart`: setup
        """,
    )
    _write(plugin_root / "hooks" / "hooks.json", "{\n  \"hooks\": {\n")

    proc = _run_checker(plugin_root)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "Hook configuration:" in output
    assert "invalid JSON in hooks/hooks.json" in output
    assert "Traceback" not in output
