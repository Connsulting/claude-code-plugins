"""Single package contract for the mechanically extracted Big Plan plugin."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "big-plan"
SKILL_ROOT = PLUGIN_ROOT / "skills" / "big-plan"
PRESERVED_SHA256 = {
    "server.py": "a62f3528a7863a142771c39aff9c0a5c525860838c2ac9ba582e63b0c3bbd9f9",
    "render.py": "87252caf8f3616f16ffb46bfb9577f0fec699f9924e00f3cde33946951d5181c",
    "dispatch.py": "853f0467a4369fc7d53d43363db9a67779f80396bf6ba519b0d1797b5e2d8435",
    "template.md": "f6a28d978c5f8ec1d73193f0e9af560976e1d2230147888849496e8f89e8d007",
    "test_dispatch.py": "33fa3b2e0d11335688dce35ed33ddd263dc893973934e2ba502084fbfb2fbe23",
    "assets/app.js": "f82959fab0d33259c592466f9c96190229aa51815b5a6639d11edc071001eeb0",
    "assets/diff.js": "e9102cd30ac1c2006140719d48f4305dde3c91e3e1f41567342f21915c85758d",
    "assets/mermaid.min.js": "74d7c46dabca328c2294733910a8aa1ed0c37451776e8d5295da38a2b758fb9b",
    "assets/style.css": "c3b44e7ba8862ff0ecfa33eade859fc137e372c31f96b6f6c00a35ed4d5587e9",
}


class BigPlanPackageContractTest(unittest.TestCase):
    def test_package_contract(self) -> None:
        def load_json(path: Path) -> dict:
            self.assertTrue(path.is_file(), f"missing {path}")
            return json.loads(path.read_text(encoding="utf-8"))

        claude_manifest = load_json(PLUGIN_ROOT / ".claude-plugin" / "plugin.json")
        codex_manifest = load_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")
        for manifest in (claude_manifest, codex_manifest):
            self.assertEqual(manifest["name"], "big-plan")
            self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+$")
            self.assertEqual(manifest["author"]["name"], "Connsulting")

        claude_marketplace = load_json(REPO_ROOT / ".claude-plugin" / "marketplace.json")
        codex_marketplace = load_json(REPO_ROOT / ".agents" / "plugins" / "marketplace.json")
        claude_entry = next(entry for entry in claude_marketplace["plugins"] if entry["name"] == "big-plan")
        codex_entry = next(entry for entry in codex_marketplace["plugins"] if entry["name"] == "big-plan")
        self.assertEqual(claude_entry["source"], "./plugins/big-plan")
        self.assertEqual(codex_entry["source"]["path"], "./plugins/big-plan")

        for relative, expected_digest in PRESERVED_SHA256.items():
            path = SKILL_ROOT / relative
            self.assertTrue(path.is_file(), f"missing preserved file: {relative}")
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(),
                expected_digest,
                f"mechanically copied runtime/UI file changed: {relative}",
            )

        required = [
            PLUGIN_ROOT / "install.sh",
            PLUGIN_ROOT / "uninstall.sh",
            PLUGIN_ROOT / "scripts" / "install.sh",
            PLUGIN_ROOT / "scripts" / "uninstall.sh",
            PLUGIN_ROOT / "scripts" / "big-plan",
            PLUGIN_ROOT / "scripts" / "big-plan.service",
            SKILL_ROOT / "SKILL.md",
        ]
        for path in required:
            self.assertTrue(path.is_file(), f"missing lifecycle file: {path}")
        for path in required[:5]:
            self.assertTrue(os.access(path, os.X_OK), f"not executable: {path}")

        launcher = (PLUGIN_ROOT / "scripts" / "big-plan").read_text(encoding="utf-8")
        installer = (PLUGIN_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("tailscale status --json", launcher)
        self.assertIn("timeout 5s tailscale status --json", launcher)
        self.assertIn('status.get("BackendState") != "Running"', launcher)
        self.assertIn('self_node.get("DNSName")', launcher)
        self.assertIn('self_node.get("TailscaleIPs")', launcher)
        self.assertIn("ipaddress.ip_address", launcher)
        self.assertIn("address.version == 4", launcher)
        self.assertIn("host=0.0.0.0", launcher)
        self.assertIn("host=127.0.0.1", launcher)
        self.assertIn("BIG_PLAN_UV_BIN", launcher)
        self.assertIn("command -v uv", launcher)
        self.assertIn('uv_bin="$HOME/.local/bin/uv"', launcher)
        self.assertIn('case "$uv_bin" in', launcher)
        self.assertIn('exec "$uv_bin" run', launcher)
        self.assertIn("--enable", installer)
        self.assertNotIn("tailscale serve", installer)
        self.assertNotIn("enable --now", installer)
        self.assertRegex(
            installer,
            r"(?s)install -m 0644.*daemon-reload.*if \[ \"\$enable\" -eq 1 \].*enable big-plan\.service.*restart big-plan\.service",
        )
        daemon_index = installer.index("systemctl --user daemon-reload")
        enable_index = installer.index("systemctl --user enable big-plan.service")
        restart_index = installer.index("systemctl --user restart big-plan.service")
        self.assertLess(daemon_index, enable_index)
        self.assertLess(enable_index, restart_index)
        self.assertNotIn("systemctl --user start big-plan.service", installer[:enable_index])
        self.assertNotIn("systemctl --user restart big-plan.service", installer[:enable_index])

        portable_files = [
            PLUGIN_ROOT / ".claude-plugin" / "plugin.json",
            PLUGIN_ROOT / ".codex-plugin" / "plugin.json",
            PLUGIN_ROOT / "README.md",
            *required,
            SKILL_ROOT / "README.md",
            SKILL_ROOT / "big-plan.service",
        ]
        hardcoded_tailnet = re.compile(r"[a-z0-9-]+\.tail[a-z0-9-]+\.ts\.net", re.IGNORECASE)
        personal_home = re.compile(r"/home/(?!example(?:/|$))[a-z0-9._-]+", re.IGNORECASE)
        for path in portable_files:
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(hardcoded_tailnet.search(text), path)
            self.assertIsNone(personal_home.search(text), path)

        self.assertEqual(
            (SKILL_ROOT / "big-plan.service").read_bytes(),
            (PLUGIN_ROOT / "scripts" / "big-plan.service").read_bytes(),
        )




if __name__ == "__main__":
    unittest.main()
