#!/usr/bin/env python3
"""
Spot potential performance regressions in compound-learning search hot path.
"""

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Locate plugin root so lib/ is importable regardless of cwd.
_PLUGIN_ROOT = os.environ.get(
    'CLAUDE_PLUGIN_ROOT',
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
sys.path.insert(0, _PLUGIN_ROOT)

import lib.db as db


_SEARCH_SCRIPT = Path(__file__).with_name('search-learnings.py')
_SPEC = importlib.util.spec_from_file_location('search_learnings', _SEARCH_SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f'Failed to load search module from {_SEARCH_SCRIPT}')
_SEARCH_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SEARCH_MOD)

execute_search = _SEARCH_MOD.execute_search

REQUIRED_METRICS = (
    'total_search_ms',
    'vector_search_ms',
    'fts_ms',
    'rerank_ms',
)
PERCENTILES = ('p50', 'p95')

DEFAULT_WORKLOAD = {
    'iterations': 6,
    'warmup_runs': 1,
    'queries': [
        'jwt refresh token rotation',
        'python asyncio gather',
        'grafana alert duplicate notifications',
        'sqlite migration rollback strategy',
    ],
}

SYNTHETIC_DOCS: List[Tuple[str, str]] = [
    (
        'perf-doc-jwt',
        'JWT auth patterns: rotate refresh tokens and validate audience claims for service APIs.',
    ),
    (
        'perf-doc-asyncio',
        'Python asyncio patterns: use gather for concurrent network calls and avoid blocking sleep.',
    ),
    (
        'perf-doc-grafana',
        'Grafana alert tuning: set pending period longer than evaluation interval to reduce flapping.',
    ),
    (
        'perf-doc-sqlite',
        'SQLite migration safety: wrap schema changes in transactions and test rollback paths.',
    ),
    (
        'perf-doc-caching',
        'Prompt caching strategies: cache stable context and invalidate cache on schema changes.',
    ),
    (
        'perf-doc-k8s',
        'Kubernetes rollout checklist: verify readiness probes and staged canary deployment gates.',
    ),
]

DEFAULT_METRIC_THRESHOLDS: Dict[str, Dict[str, float]] = {
    'total_search_ms': {
        'p50_baseline_ms': 300.0,
        'p95_baseline_ms': 520.0,
        'max_regression_pct': 50.0,
        'absolute_cap_ms': 1200.0,
    },
    'vector_search_ms': {
        'p50_baseline_ms': 220.0,
        'p95_baseline_ms': 420.0,
        'max_regression_pct': 50.0,
        'absolute_cap_ms': 1000.0,
    },
    'fts_ms': {
        'p50_baseline_ms': 20.0,
        'p95_baseline_ms': 45.0,
        'max_regression_pct': 75.0,
        'absolute_cap_ms': 180.0,
    },
    'rerank_ms': {
        'p50_baseline_ms': 25.0,
        'p95_baseline_ms': 60.0,
        'max_regression_pct': 75.0,
        'absolute_cap_ms': 200.0,
    },
}


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _default_baseline() -> Dict[str, Dict[str, float]]:
    return {
        metric: dict(values)
        for metric, values in DEFAULT_METRIC_THRESHOLDS.items()
    }


def _load_workload_settings(raw: Dict[str, Any]) -> Dict[str, Any]:
    workload = dict(DEFAULT_WORKLOAD)
    section = raw.get('workload')
    if not isinstance(section, dict):
        return workload

    iterations = section.get('iterations')
    if isinstance(iterations, int) and iterations > 0:
        workload['iterations'] = iterations

    warmup_runs = section.get('warmup_runs')
    if isinstance(warmup_runs, int) and warmup_runs >= 0:
        workload['warmup_runs'] = warmup_runs

    queries = section.get('queries')
    if isinstance(queries, list):
        normalized = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
        if normalized:
            workload['queries'] = normalized

    return workload


def load_baseline(path: Path) -> Tuple[Dict[str, Any], List[str]]:
    """
    Load baseline config and fill missing metric fields with defaults.
    Returns normalized baseline data and warning messages.
    """
    warnings: List[str] = []
    if not path.exists():
        warnings.append(f'Baseline file not found at {path}; using defaults.')
        return {
            'source': 'defaults',
            'metrics': _default_baseline(),
            'workload': dict(DEFAULT_WORKLOAD),
        }, warnings

    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise ValueError(f'Invalid JSON in baseline file {path}: {exc}') from exc

    if not isinstance(raw, dict):
        raise ValueError(f'Baseline file {path} must be a JSON object.')

    raw_metrics = raw.get('metrics')
    normalized_metrics = _default_baseline()
    if not isinstance(raw_metrics, dict):
        warnings.append('Baseline metrics block missing or invalid; using default thresholds.')
    else:
        for metric in REQUIRED_METRICS:
            metric_cfg = raw_metrics.get(metric)
            if not isinstance(metric_cfg, dict):
                warnings.append(f"Metric '{metric}' missing; using defaults for this metric.")
                continue

            for field in ('p50_baseline_ms', 'p95_baseline_ms', 'max_regression_pct', 'absolute_cap_ms'):
                value = _coerce_number(metric_cfg.get(field))
                if value is None:
                    warnings.append(
                        f"Metric '{metric}' missing '{field}'; using default value "
                        f"{normalized_metrics[metric][field]}."
                    )
                    continue
                normalized_metrics[metric][field] = value

    return {
        'source': str(path),
        'metrics': normalized_metrics,
        'workload': _load_workload_settings(raw),
    }, warnings


def percentile(values: List[float], pct: float) -> float:
    if not values:
        raise ValueError('Cannot compute percentile for empty sample.')
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    rank = (len(ordered) - 1) * (pct / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    result = ordered[lower] * (1.0 - weight) + ordered[upper] * weight
    return round(result, 3)


def summarize_metric(values: List[float]) -> Dict[str, float]:
    if not values:
        return {'samples': 0, 'p50_ms': 0.0, 'p95_ms': 0.0, 'min_ms': 0.0, 'max_ms': 0.0}
    ordered = sorted(values)
    return {
        'samples': len(values),
        'p50_ms': percentile(values, 50.0),
        'p95_ms': percentile(values, 95.0),
        'min_ms': round(ordered[0], 3),
        'max_ms': round(ordered[-1], 3),
    }


def summarize_timings(samples: Dict[str, List[float]]) -> Dict[str, Dict[str, float]]:
    return {metric: summarize_metric(samples.get(metric, [])) for metric in REQUIRED_METRICS}


def detect_regressions(
    observed: Dict[str, Dict[str, float]],
    baseline_metrics: Dict[str, Dict[str, float]],
) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    evaluation: Dict[str, Dict[str, Any]] = {}
    regressions: List[Dict[str, Any]] = []

    for metric in REQUIRED_METRICS:
        metric_obs = observed.get(metric, {})
        baseline = baseline_metrics.get(metric, DEFAULT_METRIC_THRESHOLDS[metric])
        max_regression_pct = baseline['max_regression_pct']
        absolute_cap_ms = baseline['absolute_cap_ms']

        metric_eval: Dict[str, Any] = {
            'samples': int(metric_obs.get('samples', 0)),
            'p50_ms': metric_obs.get('p50_ms', 0.0),
            'p95_ms': metric_obs.get('p95_ms', 0.0),
            'thresholds': {
                'p50_baseline_ms': baseline['p50_baseline_ms'],
                'p95_baseline_ms': baseline['p95_baseline_ms'],
                'max_regression_pct': max_regression_pct,
                'absolute_cap_ms': absolute_cap_ms,
            },
            'status': 'ok',
            'checks': {},
        }

        for pct in PERCENTILES:
            observed_key = f'{pct}_ms'
            baseline_key = f'{pct}_baseline_ms'
            observed_value = float(metric_eval[observed_key])
            baseline_value = baseline[baseline_key]
            relative_limit = round(baseline_value * (1 + (max_regression_pct / 100.0)), 3)

            reasons: List[str] = []
            if observed_value > relative_limit:
                reasons.append('baseline_relative')
            if observed_value > absolute_cap_ms:
                reasons.append('absolute_cap')

            check = {
                'observed_ms': observed_value,
                'baseline_ms': baseline_value,
                'relative_limit_ms': relative_limit,
                'absolute_cap_ms': absolute_cap_ms,
                'status': 'regressed' if reasons else 'ok',
                'reasons': reasons,
            }
            metric_eval['checks'][pct] = check

            if reasons:
                metric_eval['status'] = 'regressed'
                regressions.append(
                    {
                        'metric': metric,
                        'percentile': pct,
                        'observed_ms': observed_value,
                        'relative_limit_ms': relative_limit,
                        'absolute_cap_ms': absolute_cap_ms,
                        'reasons': reasons,
                    }
                )

        evaluation[metric] = metric_eval

    return evaluation, regressions


def build_perf_config(db_path: Path) -> Dict[str, Any]:
    config = db.load_config()
    config['sqlite']['dbPath'] = str(db_path)
    return config


def index_synthetic_docs(config: Dict[str, Any]) -> None:
    conn = db.get_connection(config)
    try:
        for doc_id, content in SYNTHETIC_DOCS:
            db.upsert_document(
                conn,
                doc_id,
                content,
                {
                    'scope': 'global',
                    'repo': '',
                    'file_path': f'/synthetic/{doc_id}.md',
                    'topic': 'performance',
                    'keywords': 'perf,synthetic',
                },
            )
    finally:
        conn.close()


def run_workload(queries: List[str], iterations: int, warmup_runs: int) -> Dict[str, Any]:
    if not queries:
        raise ValueError('Workload queries cannot be empty.')
    if iterations <= 0:
        raise ValueError('Workload iterations must be > 0.')
    if warmup_runs < 0:
        raise ValueError('Warmup runs must be >= 0.')

    # One-time model warmup before measurements.
    db.get_embedding('compound-learning perf warmup')

    with tempfile.TemporaryDirectory(prefix='compound-learning-perf-') as tmpdir:
        tmp_path = Path(tmpdir)
        config = build_perf_config(tmp_path / 'perf-regression.db')
        index_synthetic_docs(config)

        for _ in range(warmup_runs):
            for query in queries:
                warmup = execute_search(
                    query=query,
                    working_dir=tmpdir,
                    max_results=5,
                    peek_mode=True,
                    collect_timings=False,
                    config_override=config,
                )
                if warmup.get('exit_code', 0) != 0:
                    raise RuntimeError(f'Warmup search failed for "{query}": {warmup}')

        samples: Dict[str, List[float]] = {metric: [] for metric in REQUIRED_METRICS}
        for _ in range(iterations):
            for query in queries:
                result = execute_search(
                    query=query,
                    working_dir=tmpdir,
                    max_results=5,
                    peek_mode=True,
                    collect_timings=True,
                    config_override=config,
                )
                if result.get('exit_code', 0) != 0:
                    raise RuntimeError(f'Search failed for "{query}": {result}')

                timings = result.get('timings', {})
                for metric in REQUIRED_METRICS:
                    value = _coerce_number(timings.get(metric))
                    if value is None:
                        raise RuntimeError(f'Missing timing metric "{metric}" in result: {timings}')
                    samples[metric].append(value)

        return {
            'samples': samples,
            'queries': queries,
            'iterations': iterations,
            'warmup_runs': warmup_runs,
            'documents_indexed': len(SYNTHETIC_DOCS),
            'total_searches': len(queries) * iterations,
        }


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Spot potential perf regressions in compound-learning search.')
    parser.add_argument(
        '--baseline',
        type=Path,
        default=Path(_PLUGIN_ROOT) / '.claude-plugin' / 'perf-baseline.json',
        help='Path to perf baseline JSON.',
    )
    parser.add_argument(
        '--iterations',
        type=int,
        default=None,
        help='Override measured iterations from baseline workload config.',
    )
    parser.add_argument(
        '--queries-json',
        type=str,
        default=None,
        help='Optional JSON array of query strings for workload override.',
    )
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        baseline_data, warnings = load_baseline(args.baseline)

        workload = dict(baseline_data['workload'])
        if args.iterations is not None:
            if args.iterations <= 0:
                raise ValueError('--iterations must be > 0')
            workload['iterations'] = args.iterations

        if args.queries_json is not None:
            try:
                raw_queries = json.loads(args.queries_json)
            except json.JSONDecodeError as exc:
                raise ValueError(f'--queries-json is invalid JSON: {exc}') from exc
            if not isinstance(raw_queries, list):
                raise ValueError('--queries-json must be a JSON array of strings.')
            normalized_queries = [q.strip() for q in raw_queries if isinstance(q, str) and q.strip()]
            if not normalized_queries:
                raise ValueError('--queries-json must contain at least one non-empty string.')
            workload['queries'] = normalized_queries

        run = run_workload(
            queries=workload['queries'],
            iterations=workload['iterations'],
            warmup_runs=workload['warmup_runs'],
        )
        observed = summarize_timings(run['samples'])
        evaluation, regressions = detect_regressions(observed, baseline_data['metrics'])

        status = 'pass' if not regressions else 'fail'
        output = {
            'status': status,
            'baseline_source': baseline_data['source'],
            'warnings': warnings,
            'workload': {
                'queries': run['queries'],
                'iterations': run['iterations'],
                'warmup_runs': run['warmup_runs'],
                'documents_indexed': run['documents_indexed'],
                'total_searches': run['total_searches'],
            },
            'metrics': evaluation,
            'regressions': regressions,
        }
        print(json.dumps(output, indent=2))
        return 0 if status == 'pass' else 1

    except Exception as exc:
        output = {
            'status': 'error',
            'message': str(exc),
        }
        print(json.dumps(output, indent=2))
        return 2


if __name__ == '__main__':
    sys.exit(main())
