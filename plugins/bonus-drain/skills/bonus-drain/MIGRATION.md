# Manual Bonus Drain cutover

Cutover changes scheduler ownership and the only writable queue. It is intentionally a
manual operator procedure. The shipped command can inspect and render a dry-run report;
it cannot apply or roll back.

Markdown/jsonl import is a different operation. Use `bonus-drain import-legacy` for those
files. Never pass them to `bonus-drain migrate`.

## 1. Inventory and dry run

Record the old DB path, validated new config path, target XDG paths, and all old scout,
anchor, refresh, and viewer units. Also inventory the viewer bind, exact remote Host/HTTPS
Origin allowlists, trusted-loopback-proxy flag, mutations flag, and HTTPS terminator mapping.
Then run:

```sh
bonus-drain migrate \
  --from-db /path/to/legacy/bonus-drain.db \
  --from-units "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user" \
  --destination "${XDG_STATE_HOME:-$HOME/.local/state}/bonus-drain" \
  --dry-run
```

The report must name stop, mask, writer proof, backup, verification, and rollback steps.
It makes no directory, DB, config, or unit change. Resolve every warning before continuing.

## 2. Quiesce old writers

Capture current unit state before changing it:

```sh
systemctl --user list-unit-files > unit-files.before.txt
systemctl --user list-timers --all > timers.before.txt
systemctl --user status OLD_SCOUT.service OLD_ANCHOR.timer OLD_VIEWER.service \
  > units.before.txt
```

Replace the placeholders with the exact inventoried units. Stop both timers and services,
then mask them. A stopped service is not enough: a timer, dependency, or login can restart
it. Confirm they are inactive and masked.

Prove no process still has the old DB, WAL, or SHM file open for writing. Use the host's
approved open-file/process inventory tooling and record the output. If any writer remains,
stop here and reconcile it. Do not copy a live WAL database and do not point the new runtime
at it.

Queue inventory taken earlier is advisory only. Tasks can be added while preparation is in
progress, so query task/run/status counts again immediately before quiescence and take the
authoritative migration snapshot only after all old writers and viewers are stopped. Compare
the quiesced source counts with both the verified backup and destination DB before enabling
any new writer. This last snapshot is what includes late-added jobs.

## 3. Back up

Create a timestamped directory on the same trusted host. Preserve modes and copy:

1. the DB plus any `-wal` and `-shm` files after writers are quiesced;
1. the old and new config files, excluding resolved secret values;
1. the unit files and the `unit-files.before.txt`, `timers.before.txt`, and
  `units.before.txt` inventories;
1. the viewer profile and HTTPS terminator mapping inventory;
1. the installed-version/current-link inventory.

Hash the backup and make it read-only. Open the copied DB with SQLite in read-only mode and
record `PRAGMA integrity_check`, table names, task/run counts, and schema version. A backup
that has not been read back is not a rollback point.

## 4. Manual apply

With old writers masked and the backup verified:

1. Install the versioned runtime with `./install.sh`.
2. Put the validated config at the XDG config path with mode `0600`.
3. If needed, import markdown/jsonl once with `bonus-drain import-legacy`; otherwise copy or
   transform the quiesced DB using the separately reviewed operator procedure.
4. Run `bonus-drain doctor --json`, `status --json`, `queue --json`, and `runs --json`.
5. Run one `bonus-drain refresh --json`; verify every subscription and ensure a
   failed or stale reader closes its whole provider pool.
6. Run `gates --json` and `plan --json`. Do not dispatch until provider/account identities,
   reset times, and closed/open reasons are expected.
7. Enable only `bonus-drain-refresh.timer`. Observe one successful refresh, then enable
   `bonus-drain-scout.timer` and `bonus-drain-scout.path`.
8. Keep old writer units masked through at least one observed scheduling cycle.

The viewer service, when separately enabled, runs the established two-tab UI on loopback port
8766 and has `Wants=`/`After=` on the refresh timer. Tailscale Serve is the sole access
boundary. Controls require `mutations_enabled: true`, exact Host/HTTPS Origin, and JSON request
bodies; they never require an application login or secret.

For an existing SQLite queue, copy the quiesced database into the separate XDG state path;
never repoint the new runtime at an old hard link. Run `bonus-drain init` only against that
copy so additive columns and claims are created without rewriting task or run identity. Verify
task/run/status counts before and after initialization. Migrated `claude_only` and recognized
legacy-exclusive model rows remain restricted to Claude declaring the reserved
`legacy-exclusive` capability in JSON.

Schema migration 5 drops the removed account ownership table and changes historical account
backoff causes to `account_unavailable`, preserving dispatch claims and attempts. It also
migrates stored check and grant names required by the source identifier contract. Back up the
quiesced database before upgrading; the prior runtime cannot use the removed table.

Remove configured plans, accounts, and provider usage/reset readers. Bind every pacing limit
to `provider_id`, declare the private dotenv file in `secret_refs`, and set `proxy_secret_ref`.
Add required `max_size` to each provider; the example uses Grok `small`, Muse `medium`, Codex
`large`, and Claude `huge`. Use routable IDs `grok` and `muse`; proxy vendor spellings are
normalized during refresh. New dispatches store the provider ID in `account_id`, while old
claims keep their original subscription ID. Terminal commands read database authority without
validating the provider graph, so existing workers can finish during this config upgrade.
Remove `--account` from manual starts and any `accounts select` calls.
Refresh populates the proxy account inventory before automated planning. `doctor` must report
no ambiguous dispatch claim before enabling scout.

Do not enable the viewer until its local or remote mode has been reviewed against
[SECURITY.md](SECURITY.md).

## 5. Acceptance evidence

Retain the exact commands and output proving:

1. one XDG database is used by the stable CLI and every enumerated consumer;
1. old writers are inactive and masked, and no old DB writer remains;
1. config, DB, unit-state, and version backups exist and pass read-back checks;
1. refresh writes only atomic `0600` snapshots and retains a last-good snapshot on failure;
1. scout and viewers perform no foreground provider read;
1. `doctor` has no ambiguous claim requiring reconciliation;
1. the new refresh/scout timer identities and next-fire times are correct.
1. exact viewer-unit text and `systemd-analyze verify` prove the direct server command and
  refresh-timer ordering; HTTP contracts prove exact Host/HTTPS Origin and JSON-only
  mutations, while a disposable Chrome test proves the frozen frontend renders through the
  real handler. Neither proves systemd, Tailscale, nor product TLS.

## Rollback

Rollback is also manual:

1. Disable, stop, and mask `bonus-drain-scout.path` and the new scout and refresh
   timers/services. Stop the viewer.
2. Prove no new process has the queue DB, WAL, or SHM open for writing.
3. Preserve the failed-cutover DB and logs separately for diagnosis.
4. Restore the verified DB/config backup with its recorded modes and restore the recorded
   viewer profile/HTTPS terminator mapping only when separately authorized.
5. Restore old unit files and their exact enabled/masked state from the inventory.
6. Run the old read-only status/doctor checks before unmasking any writer.
7. Unmask and start only the previously active old units, then prove one writer topology.

Do not uninstall the new runtime until rollback evidence is complete. Runtime uninstall
preserves XDG state and refuses unknown/changed installed files, so it is not a substitute
for restoring a DB backup.
