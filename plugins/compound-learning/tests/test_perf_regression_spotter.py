import importlib.util
import json
import sys
from pathlib import Path
from typing import Dict


PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


def _load_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


spotter = _load_module('perf_regression_spotter', 'scripts/perf-regression-spotter.py')
search_mod = _load_module('search_learnings_module', 'scripts/search-learnings.py')


def _observed_from_baseline(baseline: Dict[str, Dict[str, float]]) -> Dict[str, Dict[str, float]]:
    observed: Dict[str, Dict[str, float]] = {}
    for metric, cfg in baseline.items():
        observed[metric] = {
            'samples': 12,
            'p50_ms': cfg['p50_baseline_ms'],
            'p95_ms': cfg['p95_baseline_ms'],
        }
    return observed


def test_load_baseline_missing_metrics_falls_back_to_defaults(tmp_path):
    baseline_path = tmp_path / 'perf-baseline.json'
    baseline_path.write_text(
        json.dumps(
            {
                'metrics': {
                    'total_search_ms': {
                        'p50_baseline_ms': 111.0,
                    }
                }
            }
        ),
        encoding='utf-8',
    )

    baseline, warnings = spotter.load_baseline(baseline_path)

    assert baseline['metrics']['total_search_ms']['p50_baseline_ms'] == 111.0
    assert (
        baseline['metrics']['total_search_ms']['p95_baseline_ms']
        == spotter.DEFAULT_METRIC_THRESHOLDS['total_search_ms']['p95_baseline_ms']
    )
    assert baseline['metrics']['vector_search_ms'] == spotter.DEFAULT_METRIC_THRESHOLDS['vector_search_ms']
    assert warnings


def test_detect_regressions_allows_values_on_threshold_edge():
    baseline = spotter._default_baseline()
    observed = {}
    for metric, cfg in baseline.items():
        observed[metric] = {
            'samples': 10,
            'p50_ms': round(cfg['p50_baseline_ms'] * (1 + cfg['max_regression_pct'] / 100.0), 3),
            'p95_ms': round(cfg['p95_baseline_ms'] * (1 + cfg['max_regression_pct'] / 100.0), 3),
        }

    evaluation, regressions = spotter.detect_regressions(observed, baseline)

    assert regressions == []
    for metric in spotter.REQUIRED_METRICS:
        assert evaluation[metric]['status'] == 'ok'


def test_detect_regressions_reports_relative_and_absolute_failures():
    baseline = spotter._default_baseline()
    observed = _observed_from_baseline(baseline)
    observed['total_search_ms']['p50_ms'] = 1300.0
    observed['total_search_ms']['p95_ms'] = 900.0

    _, regressions = spotter.detect_regressions(observed, baseline)

    p50 = next(
        r for r in regressions
        if r['metric'] == 'total_search_ms' and r['percentile'] == 'p50'
    )
    p95 = next(
        r for r in regressions
        if r['metric'] == 'total_search_ms' and r['percentile'] == 'p95'
    )
    assert set(p50['reasons']) == {'baseline_relative', 'absolute_cap'}
    assert set(p95['reasons']) == {'baseline_relative'}


def test_main_emits_fail_json_and_nonzero(monkeypatch, capsys):
    baseline = {
        'source': 'test',
        'metrics': spotter._default_baseline(),
        'workload': {
            'iterations': 1,
            'warmup_runs': 0,
            'queries': ['jwt'],
        },
    }
    run_result = {
        'samples': {
            metric: [2000.0]
            for metric in spotter.REQUIRED_METRICS
        },
        'queries': ['jwt'],
        'iterations': 1,
        'warmup_runs': 0,
        'documents_indexed': 1,
        'total_searches': 1,
    }

    monkeypatch.setattr(spotter, 'load_baseline', lambda path: (baseline, []))
    monkeypatch.setattr(spotter, 'run_workload', lambda queries, iterations, warmup_runs: run_result)

    code = spotter.main([])
    output = json.loads(capsys.readouterr().out)

    assert code == 1
    assert output['status'] == 'fail'
    assert output['regressions']


def test_main_emits_pass_json_and_zero(monkeypatch, capsys):
    baseline_metrics = spotter._default_baseline()
    baseline = {
        'source': 'test',
        'metrics': baseline_metrics,
        'workload': {
            'iterations': 1,
            'warmup_runs': 0,
            'queries': ['jwt'],
        },
    }
    run_result = {
        'samples': {
            metric: [baseline_metrics[metric]['p50_baseline_ms']]
            for metric in spotter.REQUIRED_METRICS
        },
        'queries': ['jwt'],
        'iterations': 1,
        'warmup_runs': 0,
        'documents_indexed': 1,
        'total_searches': 1,
    }

    monkeypatch.setattr(spotter, 'load_baseline', lambda path: (baseline, []))
    monkeypatch.setattr(spotter, 'run_workload', lambda queries, iterations, warmup_runs: run_result)

    code = spotter.main([])
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output['status'] == 'pass'
    assert output['regressions'] == []


def test_search_execute_collects_timing_metrics(monkeypatch):
    config = {
        'learnings': {
            'highConfidenceThreshold': 0.4,
            'possiblyRelevantThreshold': 0.55,
            'keywordBoostWeight': 0.65,
        }
    }

    class DummyConn:
        def close(self):
            return None

    monkeypatch.setattr(search_mod.db, 'load_config', lambda: config)
    monkeypatch.setattr(search_mod, 'detect_learning_hierarchy', lambda cwd, home: [])
    monkeypatch.setattr(search_mod, 'query_single_keyword', lambda *args, **kwargs: [
        {
            'id': 'doc-1',
            'document': 'JWT refresh token rotation for services',
            'metadata': {},
            'distance': 0.2,
        }
    ])
    monkeypatch.setattr(search_mod.db, 'get_connection', lambda cfg: DummyConn())
    monkeypatch.setattr(search_mod, 'fts5_search', lambda conn, query_text: {'doc-1'})

    result = search_mod.execute_search(query='jwt refresh token', collect_timings=True)

    assert result['exit_code'] == 0
    for metric in search_mod.TIMING_KEYS:
        assert metric in result['timings']
