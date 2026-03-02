import json
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

import lib.db as db


_CONFIG_ENV_VARS = (
    'CLAUDE_PLUGIN_ROOT',
    'SQLITE_DB_PATH',
    'LEARNINGS_GLOBAL_DIR',
    'LEARNINGS_REPO_SEARCH_PATH',
    'LEARNINGS_DISTANCE_THRESHOLD',
    'LEARNINGS_HIGH_CONFIDENCE_THRESHOLD',
    'LEARNINGS_POSSIBLY_RELEVANT_THRESHOLD',
    'LEARNINGS_KEYWORD_BOOST_WEIGHT',
)


def _write_config(plugin_root: Path, config: dict) -> None:
    cfg_dir = plugin_root / '.claude-plugin'
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / 'config.json').write_text(json.dumps(config), encoding='utf-8')


def _reset_env(monkeypatch, plugin_root: Path) -> None:
    for key in _CONFIG_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('CLAUDE_PLUGIN_ROOT', str(plugin_root))


def test_legacy_distance_threshold_is_honored_when_new_keys_are_missing(tmp_path, monkeypatch):
    plugin_root = tmp_path / 'plugin'
    _write_config(
        plugin_root,
        {
            'learnings': {
                'distanceThreshold': 0.5,
            }
        },
    )
    _reset_env(monkeypatch, plugin_root)

    config = db.load_config()

    assert config['learnings']['highConfidenceThreshold'] == 0.5
    assert config['learnings']['possiblyRelevantThreshold'] == 0.55


def test_new_threshold_keys_take_precedence_over_legacy_distance_threshold(tmp_path, monkeypatch):
    plugin_root = tmp_path / 'plugin'
    _write_config(
        plugin_root,
        {
            'learnings': {
                'distanceThreshold': 0.9,
                'highConfidenceThreshold': 0.41,
                'possiblyRelevantThreshold': 0.58,
            }
        },
    )
    _reset_env(monkeypatch, plugin_root)

    config = db.load_config()

    assert config['learnings']['highConfidenceThreshold'] == 0.41
    assert config['learnings']['possiblyRelevantThreshold'] == 0.58


def test_new_env_thresholds_override_legacy_env_and_config(tmp_path, monkeypatch):
    plugin_root = tmp_path / 'plugin'
    _write_config(
        plugin_root,
        {
            'learnings': {
                'distanceThreshold': 0.62,
            }
        },
    )
    _reset_env(monkeypatch, plugin_root)
    monkeypatch.setenv('LEARNINGS_DISTANCE_THRESHOLD', '0.88')
    monkeypatch.setenv('LEARNINGS_HIGH_CONFIDENCE_THRESHOLD', '0.33')
    monkeypatch.setenv('LEARNINGS_POSSIBLY_RELEVANT_THRESHOLD', '0.44')

    config = db.load_config()

    assert config['learnings']['highConfidenceThreshold'] == 0.33
    assert config['learnings']['possiblyRelevantThreshold'] == 0.44


def test_legacy_env_threshold_applies_when_new_keys_are_not_set(tmp_path, monkeypatch):
    plugin_root = tmp_path / 'plugin'
    _write_config(
        plugin_root,
        {
            'learnings': {
                'distanceThreshold': 0.5,
            }
        },
    )
    _reset_env(monkeypatch, plugin_root)
    monkeypatch.setenv('LEARNINGS_DISTANCE_THRESHOLD', '0.62')

    config = db.load_config()

    assert config['learnings']['highConfidenceThreshold'] == 0.62
    assert config['learnings']['possiblyRelevantThreshold'] == 0.62


def test_legacy_env_threshold_takes_precedence_over_legacy_file_threshold(tmp_path, monkeypatch):
    plugin_root = tmp_path / 'plugin'
    _write_config(
        plugin_root,
        {
            'learnings': {
                'distanceThreshold': 0.9,
            }
        },
    )
    _reset_env(monkeypatch, plugin_root)
    monkeypatch.setenv('LEARNINGS_DISTANCE_THRESHOLD', '0.45')

    config = db.load_config()

    assert config['learnings']['highConfidenceThreshold'] == 0.45
    assert config['learnings']['possiblyRelevantThreshold'] == 0.55
