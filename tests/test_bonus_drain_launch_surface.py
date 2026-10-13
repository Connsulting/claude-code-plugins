"""Every launch is a T3 thread. The old launch_surface setting is gone."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/bonus-drain/skills/bonus-drain"))

from bonus_drain import config as config_module

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins/bonus-drain/skills/bonus-drain"


class LaunchSurfaceRemovedTests(unittest.TestCase):
    def test_schema_and_example_omit_launch_surface(self) -> None:
        schema = json.loads((SKILL / "config.schema.json").read_text())
        example = json.loads((SKILL / "config.example.json").read_text())
        self.assertNotIn("launch_surface", schema["properties"])
        self.assertNotIn("launch_surface", schema["$defs"]["provider"]["properties"])
        self.assertNotIn("launch_surface", example)

    def test_config_rejects_launch_surface(self) -> None:
        raw = json.loads((SKILL / "config.example.json").read_text())
        raw["launch_surface"] = "background"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(raw))
            with self.assertRaises(config_module.ConfigError):
                config_module.load_config(path)


if __name__ == "__main__":
    unittest.main()
