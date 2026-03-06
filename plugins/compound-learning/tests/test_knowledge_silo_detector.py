import importlib.util
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).parent.parent
SPEC = importlib.util.spec_from_file_location(
    "knowledge_silo_detector",
    PLUGIN_ROOT / "scripts" / "detect-knowledge-silos.py",
)
DETECTOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DETECTOR
SPEC.loader.exec_module(DETECTOR)


def _rows(topic: str, repos: list[str], file_prefix: str = "learning") -> list[dict]:
    rows = []
    for index, repo in enumerate(repos, start=1):
        rows.append(
            {
                "topic": topic,
                "scope": "repo",
                "repo": repo,
                "file_path": f"/{file_prefix}-{index}.md",
            }
        )
    return rows


def _resolver(author_map: dict[str, str]):
    return lambda file_path: author_map[file_path]


def test_empty_dataset_returns_guidance():
    report = DETECTOR.detect_knowledge_silos(
        [],
        contributor_resolver=lambda _: "unknown",
    )

    assert report["status"] == "empty"
    assert report["findings"] == []
    assert "index-learnings" in report["guidance"]
    assert report["summary"]["total_learnings"] == 0


def test_single_repo_dominance_detected():
    rows = _rows("authentication", ["repo-a", "repo-a", "repo-a", "repo-a", "repo-b"])
    author_map = {
        "/learning-1.md": "alice",
        "/learning-2.md": "bob",
        "/learning-3.md": "carol",
        "/learning-4.md": "dave",
        "/learning-5.md": "erin",
    }

    report = DETECTOR.detect_knowledge_silos(
        rows,
        min_topic_samples=4,
        repo_dominance_threshold=0.70,
        author_dominance_threshold=0.90,
        contributor_resolver=_resolver(author_map),
    )

    assert len(report["findings"]) == 1
    finding = report["findings"][0]

    assert finding["topic"] == "authentication"
    assert finding["repo_dominance"]["is_silo"] is True
    assert finding["repo_dominance"]["dominant_repo"] == "repo-a"
    assert finding["repo_dominance"]["dominant_share"] == 0.8
    assert finding["author_dominance"]["is_silo"] is False


def test_balanced_topic_is_not_flagged():
    rows = _rows("ci", ["repo-a", "repo-b", "repo-a", "repo-b"])
    author_map = {
        "/learning-1.md": "alice",
        "/learning-2.md": "bob",
        "/learning-3.md": "carol",
        "/learning-4.md": "dave",
    }

    report = DETECTOR.detect_knowledge_silos(
        rows,
        min_topic_samples=4,
        repo_dominance_threshold=0.70,
        author_dominance_threshold=0.70,
        contributor_resolver=_resolver(author_map),
    )

    assert report["status"] == "ok"
    assert report["findings"] == []


def test_author_dominance_detected_without_repo_dominance():
    rows = _rows("incident-response", ["repo-a", "repo-b", "repo-c", "repo-b", "repo-d"])
    author_map = {
        "/learning-1.md": "alice",
        "/learning-2.md": "alice",
        "/learning-3.md": "alice",
        "/learning-4.md": "alice",
        "/learning-5.md": "bob",
    }

    report = DETECTOR.detect_knowledge_silos(
        rows,
        min_topic_samples=4,
        repo_dominance_threshold=0.80,
        author_dominance_threshold=0.70,
        contributor_resolver=_resolver(author_map),
    )

    assert len(report["findings"]) == 1
    finding = report["findings"][0]

    assert finding["author_dominance"]["is_silo"] is True
    assert finding["author_dominance"]["dominant_author"] == "alice"
    assert finding["author_dominance"]["dominant_share"] == 0.8
    assert finding["repo_dominance"]["is_silo"] is False


def test_threshold_boundaries_are_respected():
    rows = _rows("deployments", ["repo-a", "repo-a", "repo-a", "repo-b"])
    author_map = {
        "/learning-1.md": "bob",
        "/learning-2.md": "bob",
        "/learning-3.md": "bob",
        "/learning-4.md": "carol",
    }

    at_boundary = DETECTOR.detect_knowledge_silos(
        rows,
        min_topic_samples=4,
        repo_dominance_threshold=0.75,
        author_dominance_threshold=0.75,
        contributor_resolver=_resolver(author_map),
    )
    above_boundary = DETECTOR.detect_knowledge_silos(
        rows,
        min_topic_samples=4,
        repo_dominance_threshold=0.76,
        author_dominance_threshold=0.76,
        contributor_resolver=_resolver(author_map),
    )

    assert len(at_boundary["findings"]) == 1
    finding = at_boundary["findings"][0]
    assert finding["repo_dominance"]["is_silo"] is True
    assert finding["author_dominance"]["is_silo"] is True

    assert above_boundary["findings"] == []
