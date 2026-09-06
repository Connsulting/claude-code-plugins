#!/usr/bin/env python3
"""Create an isolated, non-executing Async Work preview from a SQLite backup.

Does not install a runtime, start a service, or change Tailscale. The copied config
uses rejecting adapters, private paths, and the viewer's explicit preview mode.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'plugins/bonus-drain/skills/bonus-drain'
sys.path.insert(0, str(SKILL))
from bonus_drain.config import load_config
from bonus_drain.db import QueueDB


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-config', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--host', required=True, help='Exact Tailscale HTTPS host including port')
    parser.add_argument('--examples', action='store_true')
    args = parser.parse_args()
    source = load_config(args.source_config)
    destination = args.destination.expanduser().resolve()
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.umask(0o077)
    database = destination / 'queue.db'
    with sqlite3.connect(source.database.as_uri() + '?mode=ro', uri=True) as original:
        with sqlite3.connect(database) as copy:
            original.backup(copy)
    data = json.loads(args.source_config.read_text())
    data['database'] = str(database)
    data['cache_dir'] = str(destination / 'cache')
    if source.cache_dir.is_dir():
        shutil.copytree(source.cache_dir, destination / 'cache', ignore=shutil.ignore_patterns('*.lock'))
    data['record_command'] = [str(SKILL / 'bin/bonus-drain'), 'record']
    data['secret_refs'] = []
    data['pr_exceptions'] = []
    bindir = destination / 'bin'
    bindir.mkdir()
    for name in ('agent-router', 'bonus-drain-account-activation', 'usage-disabled'):
        path = bindir / name
        path.write_text('#!/bin/sh\necho "Preview: external execution disabled" >&2\nexit 1\n')
        path.chmod(0o700)
    for adapter in data['adapters']:
        if adapter['kind'] == 'agent-router':
            adapter['argv'] = [str(bindir / 'agent-router')]
        elif adapter['kind'] == 'usage':
            adapter['argv'] = [str(bindir / 'usage-disabled')]
        else:
            argv = adapter['argv']
            argv[0] = str(bindir / 'bonus-drain-account-activation')
            provider = next(account['provider_id'] for account in data['accounts'] if account.get('activation_adapter_id') == adapter['id'])
            for option, value in (('--pin-file', str(destination / provider / 'PIN')), ('--active-path', str(destination / provider / 'active')), ('--rotate', '/usr/bin/false')):
                if option in argv:
                    argv[argv.index(option) + 1] = value
        adapter['env_allowlist'] = []
        adapter.pop('secret_refs', None)
    for account in data['accounts']:
        account.pop('secret_refs', None)
    data['viewer'] = {'bind': '127.0.0.1', 'mutations_enabled': True, 'preview': True,
                      'remote': {'trusted_loopback_proxy': True, 'allowed_hosts': [args.host],
                                 'allowed_origins': ['https://' + args.host]}}
    config_path = destination / 'config.json'
    config_path.write_text(json.dumps(data, indent=2) + '\n')
    config = load_config(config_path)
    assert config.database == database and config.cache_dir == destination / 'cache'
    queue = QueueDB(database)
    queue.initialize()
    if args.examples:
        examples = (
            ('preview-01-plan', 'Example: agree the plan', (), 'manual'),
            ('preview-02-build', 'Example: build the change', ('preview-01-plan',), 'manual'),
            ('preview-03-review', 'Example: review the result', ('preview-01-plan', 'preview-02-build'), 'manual'),
            ('preview-04-research', 'Example: spare-capacity research', (), 'bonus'),
        )
        for task_id, title, dependencies, mode in examples:
            queue.add_task(dict(id=task_id, title=title, kind='oneoff', priority=0, size='small',
                                cwd=str(ROOT), goal='Preview example only. Explore the editor and dependency states.',
                                constraints='Demo fixture. Do not execute.', done_when='Preview reviewed.',
                                execution_mode=mode, work_group='Preview examples', source_ref='Preview fixture',
                                depends_on=dependencies))
        queue.record('preview-01-plan', 'preview/manual/2000000000', status='done', provider_id='preview',
                     summary='Demo fixture: prerequisite marked done to illustrate readiness.', trigger='manual')
    print(json.dumps({'config': str(config_path), 'database': str(database), 'url': 'https://' + args.host,
                      'server': str(SKILL / 'services/jobs-viewer/server.py'), 'execution_enabled': False}))


if __name__ == '__main__':
    main()
