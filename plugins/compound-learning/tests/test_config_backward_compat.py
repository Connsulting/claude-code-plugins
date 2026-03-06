# ruff: noqa: E402
import json
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import lib.db as db


THRESHOLD_ENV_VARS = (
    "LEARNINGS_DISTANCE_THRESHOLD",
    "LEARNINGS_HIGH_CONFIDENCE_THRESHOLD",
    "LEARNINGS_POSSIBLY_RELEVANT_THRESHOLD",
    "LEARNINGS_KEYWORD_BOOST_WEIGHT",
)


def _write_config(tmp_path: Path, payload: dict) -> None:
    config_dir = tmp_path / ".claude-plugin"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def _reset_threshold_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var_name in THRESHOLD_ENV_VARS:
        monkeypatch.delenv(var_name, raising=False)


def test_legacy_file_distance_threshold_maps_to_tiered_thresholds(tmp_path, monkeypatch):
    _reset_threshold_env(monkeypatch)
    _write_config(
        tmp_path,
        {
            "learnings": {
                "distanceThreshold": 0.61,
            }
        },
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))

    config = db.load_config()

    assert config["learnings"]["highConfidenceThreshold"] == pytest.approx(0.61)
    assert config["learnings"]["possiblyRelevantThreshold"] == pytest.approx(0.61)
    assert config["learnings"]["keywordBoostWeight"] == pytest.approx(0.65)


def test_new_file_threshold_keys_override_legacy_file_key(tmp_path, monkeypatch):
    _reset_threshold_env(monkeypatch)
    _write_config(
        tmp_path,
        {
            "learnings": {
                "distanceThreshold": 0.72,
                "highConfidenceThreshold": 0.33,
                "possiblyRelevantThreshold": 0.48,
                "keywordBoostWeight": 0.77,
            }
        },
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))

    config = db.load_config()

    assert config["learnings"]["highConfidenceThreshold"] == pytest.approx(0.33)
    assert config["learnings"]["possiblyRelevantThreshold"] == pytest.approx(0.48)
    assert config["learnings"]["keywordBoostWeight"] == pytest.approx(0.77)


def test_legacy_env_distance_threshold_falls_back_when_new_keys_absent(tmp_path, monkeypatch):
    _reset_threshold_env(monkeypatch)
    _write_config(
        tmp_path,
        {
            "learnings": {
                "globalDir": "${HOME}/.projects/learnings",
            }
        },
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.setenv("LEARNINGS_DISTANCE_THRESHOLD", "0.58")

    config = db.load_config()

    assert config["learnings"]["highConfidenceThreshold"] == pytest.approx(0.58)
    assert config["learnings"]["possiblyRelevantThreshold"] == pytest.approx(0.58)


def test_new_env_thresholds_override_legacy_env_and_file_values(tmp_path, monkeypatch):
    _reset_threshold_env(monkeypatch)
    _write_config(
        tmp_path,
        {
            "learnings": {
                "distanceThreshold": 0.66,
                "highConfidenceThreshold": 0.31,
                "possiblyRelevantThreshold": 0.45,
                "keywordBoostWeight": 0.62,
            }
        },
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.setenv("LEARNINGS_DISTANCE_THRESHOLD", "0.69")
    monkeypatch.setenv("LEARNINGS_HIGH_CONFIDENCE_THRESHOLD", "0.27")
    monkeypatch.setenv("LEARNINGS_POSSIBLY_RELEVANT_THRESHOLD", "0.41")
    monkeypatch.setenv("LEARNINGS_KEYWORD_BOOST_WEIGHT", "0.84")

    config = db.load_config()

    assert config["learnings"]["highConfidenceThreshold"] == pytest.approx(0.27)
    assert config["learnings"]["possiblyRelevantThreshold"] == pytest.approx(0.41)
    assert config["learnings"]["keywordBoostWeight"] == pytest.approx(0.84)


def test_legacy_env_distance_threshold_overrides_legacy_file_value(tmp_path, monkeypatch):
    _reset_threshold_env(monkeypatch)
    _write_config(
        tmp_path,
        {
            "learnings": {
                "distanceThreshold": 0.64,
            }
        },
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.setenv("LEARNINGS_DISTANCE_THRESHOLD", "0.52")

    config = db.load_config()

    assert config["learnings"]["highConfidenceThreshold"] == pytest.approx(0.52)
    assert config["learnings"]["possiblyRelevantThreshold"] == pytest.approx(0.52)


def test_misordered_thresholds_are_aligned_with_warning(tmp_path, monkeypatch, capsys):
    _reset_threshold_env(monkeypatch)
    _write_config(
        tmp_path,
        {
            "learnings": {
                "highConfidenceThreshold": 0.50,
                "possiblyRelevantThreshold": 0.45,
            }
        },
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))

    config = db.load_config()
    captured = capsys.readouterr()

    assert config["learnings"]["highConfidenceThreshold"] == pytest.approx(0.50)
    assert config["learnings"]["possiblyRelevantThreshold"] == pytest.approx(0.50)
    assert "possiblyRelevantThreshold is lower" in captured.err


def test_invalid_new_file_thresholds_fall_back_to_legacy_distance(tmp_path, monkeypatch, capsys):
    _reset_threshold_env(monkeypatch)
    _write_config(
        tmp_path,
        {
            "learnings": {
                "distanceThreshold": 0.63,
                "highConfidenceThreshold": "bad",
                "possiblyRelevantThreshold": 2.0,
            }
        },
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))

    config = db.load_config()
    captured = capsys.readouterr()

    assert config["learnings"]["highConfidenceThreshold"] == pytest.approx(0.63)
    assert config["learnings"]["possiblyRelevantThreshold"] == pytest.approx(0.63)
    assert "learnings.highConfidenceThreshold" in captured.err
    assert "learnings.possiblyRelevantThreshold" in captured.err


def test_invalid_new_env_thresholds_do_not_override_valid_file_values(tmp_path, monkeypatch, capsys):
    _reset_threshold_env(monkeypatch)
    _write_config(
        tmp_path,
        {
            "learnings": {
                "distanceThreshold": 0.66,
                "highConfidenceThreshold": 0.34,
                "possiblyRelevantThreshold": 0.49,
                "keywordBoostWeight": 0.73,
            }
        },
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.setenv("LEARNINGS_DISTANCE_THRESHOLD", "0.58")
    monkeypatch.setenv("LEARNINGS_HIGH_CONFIDENCE_THRESHOLD", "oops")
    monkeypatch.setenv("LEARNINGS_POSSIBLY_RELEVANT_THRESHOLD", "nan")
    monkeypatch.setenv("LEARNINGS_KEYWORD_BOOST_WEIGHT", "1.5")

    config = db.load_config()
    captured = capsys.readouterr()

    assert config["learnings"]["highConfidenceThreshold"] == pytest.approx(0.34)
    assert config["learnings"]["possiblyRelevantThreshold"] == pytest.approx(0.49)
    assert config["learnings"]["keywordBoostWeight"] == pytest.approx(0.73)
    assert "LEARNINGS_HIGH_CONFIDENCE_THRESHOLD" in captured.err
    assert "LEARNINGS_POSSIBLY_RELEVANT_THRESHOLD" in captured.err
    assert "LEARNINGS_KEYWORD_BOOST_WEIGHT" in captured.err


def test_out_of_range_legacy_env_distance_is_ignored(tmp_path, monkeypatch, capsys):
    _reset_threshold_env(monkeypatch)
    _write_config(
        tmp_path,
        {
            "learnings": {
                "distanceThreshold": 0.62,
            }
        },
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.setenv("LEARNINGS_DISTANCE_THRESHOLD", "-0.1")

    config = db.load_config()
    captured = capsys.readouterr()

    assert config["learnings"]["highConfidenceThreshold"] == pytest.approx(0.62)
    assert config["learnings"]["possiblyRelevantThreshold"] == pytest.approx(0.62)
    assert "LEARNINGS_DISTANCE_THRESHOLD" in captured.err
