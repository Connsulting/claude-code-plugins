#!/usr/bin/env python3
"""Detect knowledge silo concentration risk from indexed learnings."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Sequence

# Locate plugin root so lib/ is importable regardless of cwd
_PLUGIN_ROOT = os.environ.get(
    "CLAUDE_PLUGIN_ROOT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
sys.path.insert(0, _PLUGIN_ROOT)

import lib.db as db
import lib.observability as observability


ASSUMPTION_TEXT = (
    "Assumption: repository scope approximates team/domain boundaries when explicit "
    "team metadata is unavailable."
)

DEFAULT_MIN_TOPIC_SAMPLES = 4
DEFAULT_REPO_DOMINANCE_THRESHOLD = 0.70
DEFAULT_AUTHOR_DOMINANCE_THRESHOLD = 0.65
DEFAULT_MAX_FINDINGS = 25


@dataclass(frozen=True)
class LearningRecord:
    topic: str
    repo: str
    author: str
    file_path: str


class GitContributorResolver:
    """Best-effort attribution for a learning file path using git history."""

    def __init__(self) -> None:
        self._file_to_author: Dict[str, str] = {}
        self._file_to_repo_root: Dict[str, str | None] = {}

    def resolve(self, file_path: str) -> str:
        normalized_path = str(Path(file_path).expanduser()) if file_path else ""
        if not normalized_path:
            return "unknown"

        if normalized_path in self._file_to_author:
            return self._file_to_author[normalized_path]

        author = self._resolve_author(normalized_path) or "unknown"
        self._file_to_author[normalized_path] = author
        return author

    def _resolve_author(self, file_path: str) -> str | None:
        repo_root = self._resolve_repo_root(file_path)
        if not repo_root:
            return None

        path_obj = Path(file_path)
        try:
            relative_path = str(path_obj.resolve().relative_to(Path(repo_root).resolve()))
        except Exception:
            relative_path = str(path_obj)

        log_cmd = ["git", "-C", repo_root, "log", "-n", "1", "--format=%an", "--", relative_path]
        try:
            proc = subprocess.run(
                log_cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )
        except Exception:
            return None

        if proc.returncode != 0:
            return None

        author = proc.stdout.strip()
        return author or None

    def _resolve_repo_root(self, file_path: str) -> str | None:
        if file_path in self._file_to_repo_root:
            return self._file_to_repo_root[file_path]

        path_obj = Path(file_path)
        if not path_obj.exists():
            self._file_to_repo_root[file_path] = None
            return None

        probe_dir = str(path_obj.parent if path_obj.is_file() else path_obj)
        root_cmd = ["git", "-C", probe_dir, "rev-parse", "--show-toplevel"]
        try:
            proc = subprocess.run(
                root_cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
        except Exception:
            self._file_to_repo_root[file_path] = None
            return None

        if proc.returncode != 0:
            self._file_to_repo_root[file_path] = None
            return None

        repo_root = proc.stdout.strip() or None
        self._file_to_repo_root[file_path] = repo_root
        return repo_root


def _normalize_topic(raw_topic: Any) -> str:
    topic = str(raw_topic or "").strip().lower().replace(" ", "-")
    return topic or "other"


def _normalize_repo(scope: Any, repo: Any) -> str:
    if str(scope or "").strip().lower() == "repo" and str(repo or "").strip():
        return str(repo).strip()
    return "global"


def _dominant(counter: Counter[str]) -> tuple[str, int]:
    if not counter:
        return "unknown", 0
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))[0]


def _distribution(counter: Counter[str], sample_size: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0])):
        share = (count / sample_size) if sample_size else 0.0
        rows.append({
            "name": name,
            "count": count,
            "share": round(share, 4),
        })
    return rows


def _risk_score(
    *,
    sample_size: int,
    repo_share: float,
    author_share: float,
    repo_signal: bool,
    author_signal: bool,
) -> float:
    score = 0.0
    if repo_signal:
        score += repo_share * 0.55
    if author_signal:
        score += author_share * 0.35

    # Slightly prioritize larger samples while keeping 0-1 bound.
    score += min(0.10, sample_size / 100.0)
    return round(min(1.0, score), 4)


def _recommendations(
    *,
    topic: str,
    repo_signal: bool,
    author_signal: bool,
    dominant_repo: str,
    dominant_author: str,
    attribution_coverage: float,
) -> List[str]:
    recs: List[str] = []

    if repo_signal:
        recs.append(
            (
                f"Reduce repo concentration for '{topic}' by capturing this topic in at least "
                f"one additional repository beyond '{dominant_repo}'."
            )
        )

    if author_signal:
        recs.append(
            (
                f"Cross-train '{topic}' ownership by pairing a second contributor with "
                f"'{dominant_author}' and documenting handoff steps."
            )
        )

    if attribution_coverage < 0.5:
        recs.append(
            (
                f"Only {attribution_coverage * 100:.1f}% of '{topic}' entries have known contributors. "
                "Prefer git-tracked learning paths to improve person-level detection quality."
            )
        )

    recs.append("Run /index-learnings after updates so detector output reflects current coverage.")
    return recs


def build_learning_records(
    rows: Sequence[Mapping[str, Any]],
    contributor_resolver: Callable[[str], str],
) -> List[LearningRecord]:
    records: List[LearningRecord] = []

    for row in rows:
        file_path = str(row.get("file_path") or "")
        record = LearningRecord(
            topic=_normalize_topic(row.get("topic")),
            repo=_normalize_repo(row.get("scope"), row.get("repo")),
            author=(contributor_resolver(file_path) if file_path else "unknown") or "unknown",
            file_path=file_path,
        )
        records.append(record)

    return records


def detect_knowledge_silos(
    rows: Sequence[Mapping[str, Any]],
    *,
    min_topic_samples: int = DEFAULT_MIN_TOPIC_SAMPLES,
    repo_dominance_threshold: float = DEFAULT_REPO_DOMINANCE_THRESHOLD,
    author_dominance_threshold: float = DEFAULT_AUTHOR_DOMINANCE_THRESHOLD,
    contributor_resolver: Callable[[str], str] | None = None,
) -> Dict[str, Any]:
    resolver = contributor_resolver or GitContributorResolver().resolve
    records = build_learning_records(rows, resolver)

    total_topics = len({_normalize_topic(row.get("topic")) for row in rows})

    if not records:
        return {
            "status": "empty",
            "assumption": ASSUMPTION_TEXT,
            "summary": {
                "total_learnings": 0,
                "total_topics": 0,
                "topics_analyzed": 0,
                "topics_with_findings": 0,
                "min_topic_samples": min_topic_samples,
                "repo_dominance_threshold": repo_dominance_threshold,
                "author_dominance_threshold": author_dominance_threshold,
            },
            "findings": [],
            "guidance": "No indexed learnings found. Run /index-learnings then rerun this detector.",
        }

    by_topic: Dict[str, List[LearningRecord]] = defaultdict(list)
    for record in records:
        by_topic[record.topic].append(record)

    findings: List[Dict[str, Any]] = []
    topics_analyzed = 0

    for topic, topic_records in sorted(by_topic.items()):
        sample_size = len(topic_records)
        if sample_size < min_topic_samples:
            continue

        topics_analyzed += 1
        repo_counts = Counter(record.repo for record in topic_records)
        author_counts = Counter(record.author for record in topic_records)

        dominant_repo, dominant_repo_count = _dominant(repo_counts)
        dominant_author, dominant_author_count = _dominant(author_counts)

        repo_share = (dominant_repo_count / sample_size) if sample_size else 0.0
        author_share = (dominant_author_count / sample_size) if sample_size else 0.0

        repo_signal = repo_share >= repo_dominance_threshold
        attribution_coverage = (
            (sample_size - author_counts.get("unknown", 0)) / sample_size
            if sample_size
            else 0.0
        )

        evaluated_author = dominant_author
        evaluated_author_count = dominant_author_count
        evaluated_author_share = author_share

        if dominant_author == "unknown":
            known_counts = Counter(
                {name: count for name, count in author_counts.items() if name != "unknown"}
            )
            if known_counts:
                evaluated_author, evaluated_author_count = _dominant(known_counts)
                evaluated_author_share = (
                    evaluated_author_count / sample_size
                    if sample_size
                    else 0.0
                )

        author_signal = (
            evaluated_author != "unknown"
            and evaluated_author_share >= author_dominance_threshold
        )

        if not (repo_signal or author_signal):
            continue

        risk_level = "high" if (repo_signal and author_signal) else "medium"
        risk_score = _risk_score(
            sample_size=sample_size,
            repo_share=repo_share,
            author_share=evaluated_author_share,
            repo_signal=repo_signal,
            author_signal=author_signal,
        )

        findings.append(
            {
                "topic": topic,
                "sample_size": sample_size,
                "risk_level": risk_level,
                "risk_score": risk_score,
                "repo_dominance": {
                    "is_silo": repo_signal,
                    "dominant_repo": dominant_repo,
                    "dominant_count": dominant_repo_count,
                    "dominant_share": round(repo_share, 4),
                    "threshold": repo_dominance_threshold,
                    "distribution": _distribution(repo_counts, sample_size),
                },
                "author_dominance": {
                    "is_silo": author_signal,
                    "dominant_author": dominant_author,
                    "dominant_count": dominant_author_count,
                    "dominant_share": round(author_share, 4),
                    "evaluated_author": evaluated_author,
                    "evaluated_count": evaluated_author_count,
                    "evaluated_share": round(evaluated_author_share, 4),
                    "attribution_coverage": round(attribution_coverage, 4),
                    "threshold": author_dominance_threshold,
                    "distribution": _distribution(author_counts, sample_size),
                },
                "recommendations": _recommendations(
                    topic=topic,
                    repo_signal=repo_signal,
                    author_signal=author_signal,
                    dominant_repo=dominant_repo,
                    dominant_author=evaluated_author,
                    attribution_coverage=attribution_coverage,
                ),
            }
        )

    findings.sort(key=lambda item: (-item["risk_score"], -item["sample_size"], item["topic"]))

    report = {
        "status": "ok",
        "assumption": ASSUMPTION_TEXT,
        "summary": {
            "total_learnings": len(records),
            "total_topics": total_topics,
            "topics_analyzed": topics_analyzed,
            "topics_with_findings": len(findings),
            "min_topic_samples": min_topic_samples,
            "repo_dominance_threshold": repo_dominance_threshold,
            "author_dominance_threshold": author_dominance_threshold,
        },
        "findings": findings,
    }

    if not findings:
        report["guidance"] = (
            "No silo risks exceeded configured thresholds. Keep indexing fresh and rerun after "
            "major ownership or repository changes."
        )

    return report


def _format_percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_text_report(report: Mapping[str, Any]) -> str:
    summary = report.get("summary", {})
    lines = [
        "Knowledge Silo Detector",
        ASSUMPTION_TEXT,
        f"Indexed learnings: {summary.get('total_learnings', 0)}",
        (
            "Topics analyzed: "
            f"{summary.get('topics_analyzed', 0)} "
            f"(min samples/topic: {summary.get('min_topic_samples', DEFAULT_MIN_TOPIC_SAMPLES)})"
        ),
        f"Findings: {summary.get('topics_with_findings', 0)}",
        "",
    ]

    if report.get("status") == "empty":
        guidance = report.get("guidance", "No indexed learnings found.")
        lines.append(guidance)
        return "\n".join(lines)

    findings = report.get("findings", [])
    if not findings:
        lines.append(report.get("guidance", "No silo risks detected."))
        return "\n".join(lines)

    for index, finding in enumerate(findings, start=1):
        lines.append(
            (
                f"[{index}] Topic: {finding['topic']} | Risk: {finding['risk_level'].upper()} "
                f"| Score: {finding['risk_score']} | Samples: {finding['sample_size']}"
            )
        )

        repo_dominance = finding["repo_dominance"]
        lines.append(
            (
                "Repo concentration: "
                f"{repo_dominance['dominant_repo']} "
                f"{_format_percent(repo_dominance['dominant_share'])} "
                f"({repo_dominance['dominant_count']}/{finding['sample_size']}) "
                f"threshold {_format_percent(repo_dominance['threshold'])}"
            )
        )

        author_dominance = finding["author_dominance"]
        author_name = (
            author_dominance.get("evaluated_author")
            if author_dominance.get("is_silo")
            else author_dominance.get("dominant_author")
        )
        author_share = (
            author_dominance.get("evaluated_share")
            if author_dominance.get("is_silo")
            else author_dominance.get("dominant_share")
        )
        author_count = (
            author_dominance.get("evaluated_count")
            if author_dominance.get("is_silo")
            else author_dominance.get("dominant_count")
        )
        lines.append(
            (
                "Contributor concentration: "
                f"{author_name} "
                f"{_format_percent(float(author_share))} "
                f"({author_count}/{finding['sample_size']}) "
                f"threshold {_format_percent(author_dominance['threshold'])}"
            )
        )

        lines.append("Recommendations:")
        for recommendation in finding.get("recommendations", []):
            lines.append(f"- {recommendation}")

        lines.append("")

    return "\n".join(lines).rstrip()


def fetch_indexed_learnings(conn: Any) -> List[Mapping[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, topic, scope, repo, file_path, created_at
        FROM learnings
        """
    ).fetchall()
    return [dict(row) for row in rows]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze indexed learnings for knowledge silo risk using topic concentration "
            "across repositories and contributors."
        )
    )
    parser.add_argument(
        "--min-topic-samples",
        type=int,
        default=DEFAULT_MIN_TOPIC_SAMPLES,
        help=(
            "Minimum number of indexed learnings required before a topic is evaluated "
            f"(default: {DEFAULT_MIN_TOPIC_SAMPLES})."
        ),
    )
    parser.add_argument(
        "--repo-dominance-threshold",
        type=float,
        default=DEFAULT_REPO_DOMINANCE_THRESHOLD,
        help=(
            "Dominance share threshold (0-1) for repository concentration "
            f"(default: {DEFAULT_REPO_DOMINANCE_THRESHOLD})."
        ),
    )
    parser.add_argument(
        "--author-dominance-threshold",
        type=float,
        default=DEFAULT_AUTHOR_DOMINANCE_THRESHOLD,
        help=(
            "Dominance share threshold (0-1) for contributor concentration "
            f"(default: {DEFAULT_AUTHOR_DOMINANCE_THRESHOLD})."
        ),
    )
    parser.add_argument(
        "--max-findings",
        type=int,
        default=DEFAULT_MAX_FINDINGS,
        help=f"Maximum findings to return (default: {DEFAULT_MAX_FINDINGS}).",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    return parser.parse_args()


def _validate_range(name: str, value: float) -> None:
    if value < 0.0 or value > 1.0:
        raise ValueError(f"{name} must be between 0 and 1, got {value}.")


def _fallback_observability_config() -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "enabled": os.environ.get("LEARNINGS_OBS_ENABLED", "false"),
    }
    level = os.environ.get("LEARNINGS_OBS_LEVEL")
    if level:
        cfg["level"] = level
    log_path = os.environ.get("LEARNINGS_OBS_LOG_PATH")
    if log_path:
        cfg["logPath"] = os.path.expanduser(log_path)
    return {"observability": cfg}


def _detector_logger(config: Mapping[str, Any] | None = None) -> observability.StructuredLogger:
    return observability.get_logger(
        "knowledge_silo_detector",
        config,
        operation_name="detect_knowledge_silos",
    )


def _emit_terminal(
    logger: observability.StructuredLogger,
    started: float,
    *,
    status: str,
    exit_code: int,
    summary: Mapping[str, Any] | None = None,
) -> None:
    counts: Dict[str, Any] = {"exit_code": exit_code}
    if isinstance(summary, Mapping):
        for key in ("total_learnings", "topics_analyzed", "topics_with_findings"):
            value = summary.get(key)
            if isinstance(value, (int, float)):
                counts[key] = int(value)

    logger.emit(
        "detector_complete",
        status,
        level="info" if status == "success" else "error",
        duration_ms=observability.elapsed_ms(started),
        counts=counts,
    )


def main() -> int:
    args = parse_args()
    run_started = observability.now_perf()

    fallback_config = _fallback_observability_config()
    bootstrap_context = observability.attach_runtime_context(
        fallback_config,
        operation_name="detect_knowledge_silos",
    )
    logger = _detector_logger(fallback_config)

    config_started = observability.now_perf()
    try:
        config = db.load_config()
    except Exception as exc:
        logger.emit(
            "config_load",
            "error",
            level="error",
            duration_ms=observability.elapsed_ms(config_started),
            error=exc,
        )
        _emit_terminal(logger, run_started, status="error", exit_code=1)
        print(f"Failed to initialize database connection: {exc}", file=sys.stderr)
        return 1

    observability.attach_runtime_context(
        config,
        operation_name="detect_knowledge_silos",
        correlation_id=bootstrap_context.get("correlation_id"),
        session_id=bootstrap_context.get("session_id"),
    )
    logger = _detector_logger(config)
    logger.emit(
        "config_load",
        "success",
        level="debug",
        duration_ms=observability.elapsed_ms(config_started),
    )
    logger.emit(
        "detector_run",
        "start",
        level="info",
        counts={"max_findings": args.max_findings},
        output_format=args.format,
    )

    if args.min_topic_samples <= 0:
        message = "--min-topic-samples must be greater than 0."
        logger.emit(
            "validation",
            "error",
            level="error",
            message=message,
            counts={"min_topic_samples": args.min_topic_samples},
        )
        _emit_terminal(logger, run_started, status="error", exit_code=2)
        print(message, file=sys.stderr)
        return 2

    if args.max_findings < 0:
        message = "--max-findings cannot be negative."
        logger.emit(
            "validation",
            "error",
            level="error",
            message=message,
            counts={"max_findings": args.max_findings},
        )
        _emit_terminal(logger, run_started, status="error", exit_code=2)
        print(message, file=sys.stderr)
        return 2

    try:
        _validate_range("--repo-dominance-threshold", args.repo_dominance_threshold)
        _validate_range("--author-dominance-threshold", args.author_dominance_threshold)
    except ValueError as exc:
        message = str(exc)
        logger.emit(
            "validation",
            "error",
            level="error",
            message=message,
            counts={
                "repo_dominance_threshold": args.repo_dominance_threshold,
                "author_dominance_threshold": args.author_dominance_threshold,
            },
        )
        _emit_terminal(logger, run_started, status="error", exit_code=2)
        print(message, file=sys.stderr)
        return 2

    db_init_started = observability.now_perf()
    logger.emit("db_init", "start", level="debug")
    try:
        conn = db.get_connection(config)
    except Exception as exc:
        logger.emit(
            "db_init",
            "error",
            level="error",
            duration_ms=observability.elapsed_ms(db_init_started),
            error=exc,
        )
        _emit_terminal(logger, run_started, status="error", exit_code=1)
        print(f"Failed to initialize database connection: {exc}", file=sys.stderr)
        return 1
    logger.emit(
        "db_init",
        "success",
        level="info",
        duration_ms=observability.elapsed_ms(db_init_started),
        counts={"connections": 1},
    )

    read_started = observability.now_perf()
    logger.emit("db_read", "start", level="debug")
    try:
        rows = fetch_indexed_learnings(conn)
    except Exception as exc:
        logger.emit(
            "db_read",
            "error",
            level="error",
            duration_ms=observability.elapsed_ms(read_started),
            error=exc,
        )
        print(f"Failed to read indexed learnings: {exc}", file=sys.stderr)
        _emit_terminal(logger, run_started, status="error", exit_code=1)
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            # Closing a failed connection should not change detector behavior.
            pass
    logger.emit(
        "db_read",
        "success",
        level="info",
        duration_ms=observability.elapsed_ms(read_started),
        counts={"rows_read": len(rows)},
    )

    compute_started = observability.now_perf()
    logger.emit(
        "detector_compute",
        "start",
        level="info",
        counts={"rows_read": len(rows)},
    )
    try:
        report = detect_knowledge_silos(
            rows,
            min_topic_samples=args.min_topic_samples,
            repo_dominance_threshold=args.repo_dominance_threshold,
            author_dominance_threshold=args.author_dominance_threshold,
        )
    except Exception as exc:
        logger.emit(
            "detector_compute",
            "error",
            level="error",
            duration_ms=observability.elapsed_ms(compute_started),
            error=exc,
            counts={"rows_read": len(rows)},
        )
        _emit_terminal(logger, run_started, status="error", exit_code=1)
        raise
    summary = report.get("summary", {})
    logger.emit(
        "detector_compute",
        "success",
        level="info",
        duration_ms=observability.elapsed_ms(compute_started),
        counts={
            "rows_read": len(rows),
            "topics_analyzed": summary.get("topics_analyzed", 0),
            "topics_with_findings": summary.get("topics_with_findings", 0),
        },
    )

    findings_before = len(report["findings"])
    if args.max_findings and findings_before > args.max_findings:
        report["findings"] = report["findings"][: args.max_findings]
        report["summary"]["topics_with_findings"] = len(report["findings"])
        report["truncated"] = True
        logger.emit(
            "truncation",
            "applied",
            level="info",
            counts={
                "findings_before": findings_before,
                "findings_after": len(report["findings"]),
                "max_findings": args.max_findings,
            },
        )
    elif args.max_findings == 0:
        logger.emit(
            "truncation",
            "disabled",
            level="debug",
            counts={
                "findings_before": findings_before,
                "max_findings": args.max_findings,
            },
        )
    else:
        logger.emit(
            "truncation",
            "not_needed",
            level="debug",
            counts={
                "findings_before": findings_before,
                "max_findings": args.max_findings,
            },
        )

    emit_started = observability.now_perf()
    try:
        if args.format == "json":
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(render_text_report(report))
    except Exception as exc:
        logger.emit(
            "output_emit",
            "error",
            level="error",
            duration_ms=observability.elapsed_ms(emit_started),
            error=exc,
            output_format=args.format,
        )
        _emit_terminal(logger, run_started, status="error", exit_code=1, summary=summary)
        raise
    logger.emit(
        "output_emit",
        "success",
        level="info",
        duration_ms=observability.elapsed_ms(emit_started),
        counts={"findings_emitted": len(report.get("findings", []))},
        output_format=args.format,
    )
    _emit_terminal(logger, run_started, status="success", exit_code=0, summary=summary)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
