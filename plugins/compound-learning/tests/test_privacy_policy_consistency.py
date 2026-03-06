import json
import subprocess
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).parent.parent
CHECKER_SCRIPT = PLUGIN_ROOT / "scripts" / "check-privacy-policy.py"
CLAIMS_MANIFEST = PLUGIN_ROOT / "privacy-policy-claims.json"


def _run_checker(manifest_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER_SCRIPT),
            "--plugin-root",
            str(PLUGIN_ROOT),
            "--manifest",
            str(manifest_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_privacy_policy_checker_passes_for_repository_baseline():
    result = _run_checker(CLAIMS_MANIFEST)

    assert result.returncode == 0, (
        "checker should pass for repository baseline\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "[PASS]" in result.stdout
    assert "automated_fail=0" in result.stdout


def test_privacy_policy_checker_fails_on_manifest_mismatch(tmp_path):
    manifest = json.loads(CLAIMS_MANIFEST.read_text(encoding="utf-8"))

    for claim in manifest.get("claims", []):
        if claim.get("enforcement") == "automated":
            claim["checks"] = [
                {
                    "type": "contains",
                    "file": "README.md",
                    "value": "THIS_SENTINEL_SHOULD_NOT_EXIST_IN_README",
                }
            ]
            break
    else:
        raise AssertionError("expected at least one automated claim in manifest")

    mismatch_manifest = tmp_path / "privacy-policy-claims.mismatch.json"
    mismatch_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run_checker(mismatch_manifest)

    assert result.returncode != 0
    assert "[FAIL]" in result.stdout
    assert "THIS_SENTINEL_SHOULD_NOT_EXIST_IN_README" in result.stdout
