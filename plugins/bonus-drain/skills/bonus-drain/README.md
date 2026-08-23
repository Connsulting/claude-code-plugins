# Bonus Drain runtime

Bonus Drain queues low-priority work and spends only configured provider capacity
that would otherwise expire. It is a Python-standard-library runtime with one stable
`bonus-drain` command. Providers, accounts, plans, limits, usage readers, and optional
account activators are data in the validated JSON config; the planner contains no
provider-name branches.

Every launch goes through an `agent-router` adapter. `auto` is classification only:
the router is queried with a dry run, Bonus Drain validates the result, then claims and
launches once with a concrete provider and account. Missing, malformed, resetless, or
stale cache data closes only the affected account. Leaving work queued is expected.

## Requirements

- Python 3.10 or newer with the standard `sqlite3` module.
- An executable `agent-router` configured as the dispatch adapter.
- One executable usage adapter for each account. It may report reset timestamps with usage,
  or the account may select a separate reset adapter. Adapters receive only validated argv,
  an allowlisted environment, a timeout, and an output limit.
- The shipped legacy Claude/Codex/Grok adapter examples are optional compatibility assets:
  they require Bash, `jq`, `curl`, and GNU `date`, `find`, `sort`, `tail`, and `tac` where
  their comments indicate. A generic installation may replace them with any executable
  adapter that emits the documented JSON contract.
- Optional activation adapters for installations that switch among accounts.
- `ai-token-rotator` is not required; when used, configure it as an activation adapter and
  let `doctor` report whether its executable is present.
- systemd user services only if hourly scout and ten-minute refresh timers are wanted.
- Tailscale Serve or another authenticated HTTPS terminator only if the viewer is used
  remotely.

There is no runtime dependency on `uv`, `bg-schedule`, `codex-bg-thread`, a provider CLI,
or a shell evaluator. Provider-specific readers may have their own documented external
dependencies, but they are adapters rather than Bonus Drain internals.

## Paths

The defaults follow XDG and can be changed by their standard XDG environment variables:

| Purpose | Default |
| --- | --- |
| validated config | `~/.config/bonus-drain/config.json` |
| SQLite queue | `~/.local/state/bonus-drain/queue.db` |
| normalized usage cache | `~/.cache/bonus-drain/` |
| versions | `~/.local/lib/bonus-drain/<version>/` |
| active version | `~/.local/lib/bonus-drain/current` |
| stable command | `~/.local/bin/bonus-drain` |

`BONUS_DB` and `BONUSDB` remain compatibility exports. They resolve to the same
config-selected database and stable command as `BONUS_DRAIN_BIN`; they are not alternate
state locations.

## Configuration

Start from `config.example.json` and validate before installing services:

```sh
install -d -m 0700 "${XDG_CONFIG_HOME:-$HOME/.config}/bonus-drain"
install -m 0600 config.example.json \
  "${XDG_CONFIG_HOME:-$HOME/.config}/bonus-drain/config.json"
BONUS_DRAIN_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/bonus-drain/config.json" \
  bonus-drain doctor --json
```

The configuration graph is versioned and uses IDs for all relationships:

- `adapters[]` declares argv arrays for `agent-router`, usage, optional separate reset,
  and optional activation adapters.
- `providers[]` binds a provider ID to the router adapter and its router-facing name.
- `plans[]`, `accounts[]`, and `limits[]` describe independent capacity windows.
- `secret_refs[]` names externally injected values. Credential-shaped inline fields,
  shell interpolation, dangling references, duplicate IDs, and direct-provider dispatch
  are rejected.
- `pr_exceptions[]` is the only place to grant repository-specific push behavior.
- `viewer` defaults to loopback and no mutations. See [SECURITY.md](SECURITY.md) before
  enabling any remote mode.

Do not put a credential value in JSON, a unit file, an argv, or a repository. Supply the
named external secret to the service environment or credential manager at runtime. Config
validation resolves references, not secret values.

## Install and operate

From the skill directory, install a version and the five self-owned unit templates:

```sh
./install.sh
~/.local/bin/bonus-drain status --json
~/.local/bin/bonus-drain doctor --json
```

Installation does not enable or start anything. After placing the validated config and
injecting its external secrets, initialize and perform one cache refresh manually:

```sh
bonus-drain init
bonus-drain refresh --json
bonus-drain gates --json
bonus-drain plan --json
```

Only after those commands report the intended DB, accounts, and closed/open gates should
an operator enable scheduling:

```sh
systemctl --user daemon-reload
systemctl --user enable --now bonus-drain-refresh.timer bonus-drain-scout.timer
systemctl --user list-timers 'bonus-drain-*'
```

The refresher is the only component that runs usage adapters. Scout, `gates`, `plan`, the
Bonus-only viewer, and the combined jobs viewer read normalized cache and SQLite only.

Useful read-only checks:

```sh
bonus-drain status --json
bonus-drain doctor --json
bonus-drain queue --json
bonus-drain runs --json
journalctl --user -u bonus-drain-refresh.service -u bonus-drain-scout.service --since today
```

## Verification from a source checkout

These are the complete source checks. Run them in a disposable HOME/XDG environment when
validating lifecycle behavior; none requires provider credentials:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
python3 tests/verify_distribution.py \
  --manifest distribution-manifest.json --source .
for test_file in test-*.sh; do bash "$test_file"; done
python3 -m compileall -q bonus_drain
BONUS_DRAIN_CONFIG=config.example.json bin/bonus-drain doctor --json
systemd-analyze verify systemd/*.service systemd/*.timer
```

Provider adapter integration should use fixture executables and a disposable DB/cache.
Proof must include a failed sibling refresh retaining its last good cache, a stale account
closing independently, one atomic claim under a race, and no adapter call from a viewer or
scout request path.

## Migration and removal

There are two separate operations:

- `bonus-drain import-legacy --backlog FILE --runs FILE` imports the old markdown/jsonl
  queue format into a selected DB. The deprecated `migrate.py` delegates only to this
  importer and never controls units.
- `bonus-drain migrate --from-db FILE --from-units DIR --destination DIR --dry-run`
  inspects an old DB/unit installation and emits a manual cutover report. Code refuses
  apply and rollback.

Read [MIGRATION.md](MIGRATION.md) for the required stop/mask, writer proof, backup,
manual apply, verification, and rollback sequence. Do not run old and new writers against
one queue.

To remove runtime-owned files while preserving config, DB, cache, and operator state:

```sh
systemctl --user disable --now \
  bonus-drain-scout.timer bonus-drain-refresh.timer bonus-drain-viewer.service
./uninstall.sh
```

Uninstall verifies hashes and refuses if an installed version, wrapper, or unit has been
changed or contains an unknown file. Move operator additions elsewhere and retry; do not
delete the state directories as part of runtime removal.
