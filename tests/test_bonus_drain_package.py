"""Contract tests for the standalone Bonus Drain marketplace package.

These tests deliberately validate the artifact users install, rather than the
development checkout that produced it.  The copied skill's distribution
manifest is the single source of truth for its payload.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterator


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "bonus-drain"
SKILL_ROOT = PLUGIN_ROOT / "skills" / "bonus-drain"

EXACT_EXCLUDES = {
    "register-implement-factory-review.sh",
    "test-factory-review.sh",
}
PERSONAL_TEXT = {
    r"/home/": "a machine-specific home directory",
    r"\btheconnman\b": "the source checkout owner",
    r"\bbrian\s+conn\b": "a personal name",
    r"\bentlify\b": "a personal repository name",
    r"\brutherford-soddy\b": "a personal repository name",
    r"\bcurietech\b": "a personal repository name",
    r"\bagentos\b": "a personal repository name",
    r"~/(?:\.claude|\.codex)\b": "a personal tool-state path",
}
TOKEN_PATTERNS = (
    r"\b(?:sk|rk|pk)_[A-Za-z0-9_-]{16,}\b",
    r"\bgh[pousr]_[A-Za-z0-9_]{16,}\b",
    r"\bgithub_pat_[A-Za-z0-9_]{16,}\b",
    r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b",
    r"\bAKIA[0-9A-Z]{16}\b",
)
INLINE_SECRET_ASSIGNMENT = re.compile(
    r"""(?imx)
    ^\s*(?:export\s+)?
    (?:ANTHROPIC_API_KEY|OPENAI_API_KEY|API_KEY|ACCESS_TOKEN|AUTH_TOKEN|PASSWORD|SECRET)
    \s*=\s*
    (?!\s*(?:\$\{?[A-Z_][A-Z0-9_]*\}?|"?\$\(|<[^>]+>|(?:env|secret|keyring)://|resolve_secret\(|''|""))
    [^\s#]+
    """
)


class BonusDrainPackageContractTests(unittest.TestCase):
    """Keep the distributable package generic, self-contained, and discoverable."""

    maxDiff = None

    def require_plugin(self) -> None:
        self.assertTrue(
            PLUGIN_ROOT.is_dir(),
            f"Bonus Drain package is missing: {PLUGIN_ROOT}",
        )

    def require_file(self, path: Path) -> None:
        self.assertTrue(path.is_file(), f"required package file is missing: {path}")

    def load_json(self, path: Path) -> dict[str, Any]:
        self.require_file(path)
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        self.assertIsInstance(value, dict, f"{path} must contain a JSON object")
        return value

    def plugin_files(self) -> list[Path]:
        self.require_plugin()
        return sorted(
            path for path in PLUGIN_ROOT.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        )

    def distribution_manifest(self) -> tuple[dict[str, Any], set[str]]:
        manifest_path = SKILL_ROOT / "distribution-manifest.json"
        manifest = self.load_json(manifest_path)
        self.assertEqual(
            manifest.get("schema_version"),
            1,
            "distribution manifest schema_version must be 1",
        )
        included = manifest.get("include")
        self.assertIsInstance(included, list, "manifest include must be a list")
        self.assertTrue(included, "manifest include must not be empty")
        self.assertTrue(
            all(isinstance(entry, str) for entry in included),
            "manifest include entries must be strings",
        )

        include_set = set(included)
        self.assertEqual(len(include_set), len(included), "manifest include has duplicates")
        for entry in include_set:
            candidate = Path(entry)
            self.assertFalse(candidate.is_absolute(), f"manifest path is absolute: {entry}")
            self.assertNotIn("..", candidate.parts, f"manifest path escapes skill root: {entry}")
            self.assertEqual(candidate.as_posix(), entry, f"manifest path must use POSIX form: {entry}")
        return manifest, include_set

    def payload_text(self) -> Iterator[tuple[Path, str]]:
        for path in self.plugin_files():
            try:
                yield path, path.read_text(encoding="utf-8")
            except UnicodeDecodeError as error:
                self.fail(f"shipped payload must be UTF-8 text, unable to read {path}: {error}")

    def test_plugin_manifests_declare_bonus_drain_for_claude_and_codex(self) -> None:
        self.require_plugin()
        versions = []
        for manifest_path in (
            PLUGIN_ROOT / ".claude-plugin" / "plugin.json",
            PLUGIN_ROOT / ".codex-plugin" / "plugin.json",
        ):
            manifest = self.load_json(manifest_path)
            self.assertEqual(manifest.get("name"), "bonus-drain", manifest_path)
            self.assertRegex(
                str(manifest.get("version", "")),
                r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$",
                f"{manifest_path} needs a semantic version",
            )
            self.assertIsInstance(manifest.get("description"), str, manifest_path)
            self.assertTrue(manifest["description"].strip(), manifest_path)
            versions.append(manifest["version"])
        self.assertEqual(versions, ["0.2.0", "0.2.0"])

        sys.path.insert(0, str(SKILL_ROOT))
        try:
            from bonus_drain import __version__
            from bonus_drain import lifecycle
        finally:
            sys.path.pop(0)
        self.assertEqual(__version__, "0.2.0")
        self.assertEqual(lifecycle._DEFAULT_VERSION, "0.2.0")

    def test_packaged_viewer_supports_secretless_tailnet_controls(self) -> None:
        example = self.load_json(SKILL_ROOT / "config.example.json")
        viewer_config = example["viewer"]
        remote = viewer_config["remote"]
        self.assertEqual(viewer_config["bind"], "127.0.0.1")
        self.assertIs(viewer_config["mutations_enabled"], True)
        self.assertIs(remote["trusted_loopback_proxy"], True)
        self.assertNotIn("auth_secret_ref", remote)

        self.assertTrue((SKILL_ROOT / "services" / "jobs-viewer" / "server.py").is_file())

    def test_claude_and_codex_marketplaces_point_at_the_plugin_root(self) -> None:
        self.require_plugin()
        claude_marketplace = self.load_json(REPO_ROOT / ".claude-plugin" / "marketplace.json")
        codex_marketplace = self.load_json(
            REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
        )

        claude_entries = claude_marketplace.get("plugins")
        codex_entries = codex_marketplace.get("plugins")
        self.assertIsInstance(claude_entries, list, "Claude marketplace needs a plugins list")
        self.assertIsInstance(codex_entries, list, "Codex marketplace needs a plugins list")

        claude_entry = next(
            (entry for entry in claude_entries if entry.get("name") == "bonus-drain"),
            None,
        )
        self.assertIsNotNone(claude_entry, "Claude marketplace is missing bonus-drain")
        self.assertEqual(claude_entry.get("source"), "./plugins/bonus-drain")

        codex_entry = next(
            (entry for entry in codex_entries if entry.get("name") == "bonus-drain"),
            None,
        )
        self.assertIsNotNone(codex_entry, "Codex marketplace is missing bonus-drain")
        self.assertEqual(
            codex_entry.get("source", {}).get("path"),
            "./plugins/bonus-drain",
            "repo-local Codex marketplace must use source.path",
        )
        self.assertTrue(
            (REPO_ROOT / codex_entry["source"]["path"]).is_dir(),
            "Codex marketplace source.path must resolve to the package",
        )

    def test_distribution_manifest_is_the_exact_shipped_skill_file_list(self) -> None:
        self.require_plugin()
        manifest, included = self.distribution_manifest()
        actual = {
            path.relative_to(SKILL_ROOT).as_posix()
            for path in self.plugin_files()
            if path.is_relative_to(SKILL_ROOT)
        }
        self.assertEqual(
            included,
            actual,
            "distribution manifest include must equal every copied skill file",
        )

        sys.path.insert(0, str(SKILL_ROOT))
        try:
            from bonus_drain import lifecycle

            installed_payload = {
                path.as_posix() for path in lifecycle._runtime_files(SKILL_ROOT)
            }
        finally:
            sys.path.pop(0)
        self.assertEqual(
            installed_payload,
            included,
            "installer runtime selection must equal the distribution manifest",
        )

        excluded = manifest.get("exclude")
        self.assertIsInstance(excluded, list, "manifest exclude must be a list")
        self.assertEqual(
            set(excluded),
            EXACT_EXCLUDES,
            "manifest must declare exactly the known source-only exclusions",
        )
        self.assertTrue(all(isinstance(entry, str) for entry in excluded))
        self.assertFalse(set(excluded) & included, "excluded files cannot be included")
        self.assertFalse(set(excluded) & actual, "excluded files cannot ship")

        exclude_patterns = manifest.get("exclude_patterns")
        self.assertIsInstance(
            exclude_patterns,
            list,
            "manifest needs a future personal-registrar exclusion pattern",
        )
        self.assertIn("register-*.sh", exclude_patterns)
        self.assertTrue(
            fnmatch.fnmatchcase("register-a-future-personal-task.sh", "register-*.sh"),
            "the registrar exclusion pattern must cover future registrars",
        )

    def test_package_contains_standalone_runtime_configuration_docs_and_installers(self) -> None:
        self.require_plugin()
        required_paths = {
            "skills/bonus-drain/SKILL.md",
            "skills/bonus-drain/README.md",
            "skills/bonus-drain/MIGRATION.md",
            "skills/bonus-drain/SECURITY.md",
            "skills/bonus-drain/config.schema.json",
            "skills/bonus-drain/config.example.json",
            "skills/bonus-drain/distribution-manifest.json",
            "skills/bonus-drain/bin/bonus-drain",
            "skills/bonus-drain/bin/bonus-drain-account-usage",
            "skills/bonus-drain/bin/bonus-drain-grok-usage",
            "skills/bonus-drain/bin/bonus-drain-account-activation",
            "skills/bonus-drain/install.sh",
            "skills/bonus-drain/uninstall.sh",
            "skills/bonus-drain/bonus_drain/__init__.py",
            "skills/bonus-drain/bonus_drain/__main__.py",
            "skills/bonus-drain/bonus_drain/cli.py",
            "skills/bonus-drain/bonus_drain/config.py",
            "skills/bonus-drain/bonus_drain/db.py",
            "skills/bonus-drain/bonus_drain/adapters.py",
            "skills/bonus-drain/bonus_drain/usage.py",
            "skills/bonus-drain/bonus_drain/planner.py",
            "skills/bonus-drain/bonus_drain/dispatcher.py",
            "skills/bonus-drain/bonus_drain/kick.py",
            "skills/bonus-drain/bonus_drain/scout.py",
            "skills/bonus-drain/bonus_drain/lifecycle.py",
            "skills/bonus-drain/bonus_drain/cutover.py",
            "skills/bonus-drain/services/jobs-viewer/server.py",
            "skills/bonus-drain/systemd/bonus-drain-refresh.service",
            "skills/bonus-drain/systemd/bonus-drain-refresh.timer",
            "skills/bonus-drain/systemd/bonus-drain-scout.service",
            "skills/bonus-drain/systemd/bonus-drain-scout.timer",
            "skills/bonus-drain/systemd/bonus-drain-viewer.service",
        }
        actual = {
            path.relative_to(PLUGIN_ROOT).as_posix() for path in self.plugin_files()
        }
        self.assertTrue(
            required_paths <= actual,
            f"package lacks standalone assets: {sorted(required_paths - actual)}",
        )
        retired = {
            path for path in actual
            if path == "skills/bonus-drain/VIEWER_FOLLOW_UP.md"
            or path.endswith("services/jobs-viewer/usage.sh")
            or path.endswith("services/jobs-viewer/codex-usage.sh")
            or path.endswith("services/jobs-viewer/grok-usage.sh")
        }
        self.assertFalse(
            retired,
            f"retired copied collector assets must not ship: {sorted(retired)}",
        )

        for relative_path in (
            "skills/bonus-drain/bin/bonus-drain",
            "skills/bonus-drain/bin/bonus-drain-account-usage",
            "skills/bonus-drain/bin/bonus-drain-grok-usage",
            "skills/bonus-drain/bin/bonus-drain-account-activation",
            "skills/bonus-drain/install.sh",
            "skills/bonus-drain/uninstall.sh",
            "skills/bonus-drain/scripts/install.sh",
            "skills/bonus-drain/scripts/uninstall.sh",
        ):
            mode = (PLUGIN_ROOT / relative_path).stat().st_mode
            self.assertTrue(
                mode & stat.S_IXUSR,
                f"{relative_path} must be executable for a standalone install",
            )

    def test_packaged_adapters_are_manifest_owned_and_configuration_is_explicit(self) -> None:
        _manifest, included = self.distribution_manifest()
        adapter_paths = {
            "bin/bonus-drain-account-usage",
            "bin/bonus-drain-grok-usage",
            "bin/bonus-drain-account-activation",
        }
        self.assertTrue(
            adapter_paths <= included,
            f"distribution manifest does not own adapters: {sorted(adapter_paths - included)}",
        )
        for relative in adapter_paths:
            path = SKILL_ROOT / relative
            self.require_file(path)
            self.assertTrue(os.access(path, os.X_OK), f"{relative} must retain executable mode")

        wrappers = {
            name: (SKILL_ROOT / name).read_text(encoding="utf-8")
            for name in ("usage.sh", "codex-usage.sh", "grok-usage.sh")
        }
        for name, text in wrappers.items():
            with self.subTest(wrapper=name):
                self.assertNotIn("--legacy-index", text)
                self.assertNotIn("LEGACY_INDEX", text)

        example = self.load_json(SKILL_ROOT / "config.example.json")
        adapter_argv = {
            adapter["id"]: adapter.get("argv", [])
            for adapter in example.get("adapters", [])
            if isinstance(adapter, dict) and isinstance(adapter.get("id"), str)
        }
        flattened = "\n".join(
            "\0".join(str(token) for token in argv)
            for argv in adapter_argv.values()
        )
        for executable in sorted(adapter_paths):
            self.assertIn(
                executable.removeprefix("bin/"),
                flattened,
                f"example config must demonstrate shipped {executable}",
            )
        self.assertNotIn("/usr/local/libexec/bonus-drain", flattened)
        self.assertIn("{provider_id}", flattened)
        self.assertIn("{account_id}", flattened)
        self.assertIn("{action}", flattened)

    def test_package_test_command_is_idempotent_without_bytecode_environment_flags(self) -> None:
        if os.environ.get("BONUS_DRAIN_PACKAGE_IDEMPOTENCE_CHILD") == "1":
            return
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "marketplace"
            shutil.copytree(
                REPO_ROOT,
                checkout,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*.pyo"),
            )
            environment = dict(os.environ)
            environment.pop("PYTHONDONTWRITEBYTECODE", None)
            environment["BONUS_DRAIN_PACKAGE_IDEMPOTENCE_CHILD"] = "1"
            command = [sys.executable, "tests/test_bonus_drain_package.py"]
            results = [
                subprocess.run(
                    command, cwd=checkout, env=environment, check=False,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=30,
                )
                for _ in range(2)
            ]
            self.assertTrue(
                list((checkout / "plugins" / "bonus-drain").rglob("*.pyc")),
                "fixture must exercise generated bytecode rather than suppressing it",
            )
            for index, completed in enumerate(results, start=1):
                self.assertEqual(
                    completed.returncode, 0,
                    f"package test pass {index} failed:\n{completed.stdout}\n{completed.stderr}",
                )

    def test_shipped_runtime_has_only_stdlib_dependencies_and_no_legacy_dispatchers(self) -> None:
        self.require_plugin()
        dependency_artifacts = {
            "requirements.txt",
            "requirements-dev.txt",
            "pyproject.toml",
            "Pipfile",
            "Pipfile.lock",
            "poetry.lock",
            "uv.lock",
        }
        present_dependency_artifacts = {
            path.name for path in self.plugin_files() if path.name in dependency_artifacts
        }
        self.assertFalse(
            present_dependency_artifacts,
            f"standalone package must not ship dependency managers: {present_dependency_artifacts}",
        )

        runtime_text = "\n".join(
            text
            for path, text in self.payload_text()
            if path.suffix in {".py", ".sh", ".service", ".timer"}
            or os.access(path, os.X_OK)
        )
        self.assertNotRegex(runtime_text, r"(?i)\buv\b")
        self.assertNotRegex(runtime_text, r"(?i)\bcodex-bg-thread\b")
        core_runtime_text = "\n".join(
            text for path, text in self.payload_text()
            if path.name != "server.py" and (
                path.suffix in {".py", ".sh", ".service", ".timer"}
                or os.access(path, os.X_OK)
            )
        )
        self.assertNotRegex(core_runtime_text, r"(?i)\bbg-schedule\b")

        viewer_unit = (
            SKILL_ROOT / "systemd" / "bonus-drain-viewer.service"
        ).read_text(encoding="utf-8")
        self.assertIn(
                "ExecStart=/usr/bin/python3 -B %h/.local/lib/bonus-drain/current/services/jobs-viewer/server.py --port 8766",
            viewer_unit,
        )
        self.assertIn(
            "Environment=BONUS_DRAIN_CONFIG=%h/.config/bonus-drain/config.json",
            viewer_unit,
        )
        self.assertRegex(viewer_unit, r"(?m)^Wants=.*\bbonus-drain-refresh\.timer\b")
        self.assertRegex(viewer_unit, r"(?m)^After=.*\bbonus-drain-refresh\.timer\b")
        self.assertNotIn("Environment=BONUS_DB=", viewer_unit)
        self.assertNotIn("claude-settings", viewer_unit)
        self.assertNotRegex(viewer_unit, r"(?i)\buv\b")

        analyzer = shutil.which("systemd-analyze")
        if analyzer is not None:
            completed = subprocess.run(
                [analyzer, "verify", str(SKILL_ROOT / "systemd" / "bonus-drain-viewer.service")],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            self.assertEqual(
                completed.returncode,
                0,
                f"systemd-analyze rejected the shipped viewer unit:\n{completed.stdout}\n{completed.stderr}",
            )

        runtime_package = SKILL_ROOT / "bonus_drain"
        local_modules = {path.stem for path in runtime_package.glob("*.py")}
        stdlib_modules = set(getattr(sys, "stdlib_module_names", ())) | {
            "__future__",
            "bonus_drain",
        }
        for path in sorted(runtime_package.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    modules = [node.module]
                for module in modules:
                    root = module.split(".", 1)[0]
                    self.assertIn(
                        root,
                        stdlib_modules | local_modules,
                        f"{path.relative_to(PLUGIN_ROOT)} imports non-stdlib module {module}",
                    )

    def test_shipped_payload_has_no_personal_context_or_inline_secrets(self) -> None:
        self.require_plugin()
        for path, text in self.payload_text():
            relative_path = path.relative_to(PLUGIN_ROOT)
            for pattern, description in PERSONAL_TEXT.items():
                self.assertNotRegex(
                    text,
                    re.compile(pattern, re.IGNORECASE),
                    f"{relative_path} contains {description}",
                )
            for pattern in TOKEN_PATTERNS:
                self.assertNotRegex(
                    text,
                    re.compile(pattern),
                    f"{relative_path} contains a credential-shaped token",
                )
            self.assertNotRegex(
                text,
                INLINE_SECRET_ASSIGNMENT,
                f"{relative_path} contains an inline secret assignment; use a secret reference",
            )

    def test_viewer_example_enables_tailnet_controls_but_schema_defaults_safe(self) -> None:
        self.require_plugin()
        example = self.load_json(SKILL_ROOT / "config.example.json")
        viewer = example.get("viewer")
        self.assertIsInstance(viewer, dict, "config example needs a viewer section")
        self.assertIn(viewer.get("bind"), {"127.0.0.1", "localhost"})
        self.assertIs(viewer.get("mutations_enabled"), True)

        schema = self.load_json(SKILL_ROOT / "config.schema.json")
        viewer_schema = schema.get("properties", {}).get("viewer", {})
        viewer_properties = viewer_schema.get("properties", {})
        self.assertIn(
            viewer_properties.get("bind", {}).get("default"),
            {"127.0.0.1", "localhost"},
            "viewer schema must default to loopback",
        )
        self.assertIs(
            viewer_properties.get("mutations_enabled", {}).get("default"),
            False,
            "viewer mutations must default off",
        )

    def test_package_readme_has_manual_operations_and_dependency_guidance(self) -> None:
        self.require_plugin()
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        headings = [
            match.group(1).strip().lower()
            for match in re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", readme)
        ]
        required_heading_terms = {
            "manual cutover": ("manual", "cutover"),
            "backup": ("backup",),
            "rollback": ("rollback",),
            "tailscale": ("tailscale",),
            "dependencies": ("depend",),
        }
        for section, terms in required_heading_terms.items():
            self.assertTrue(
                any(all(term in heading for term in terms) for heading in headings),
                f"README needs a {section} section",
            )


if __name__ == "__main__":
    unittest.main()
