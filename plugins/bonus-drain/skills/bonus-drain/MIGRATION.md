# Manual Bonus Drain cutover

Cutover changes scheduler ownership and the only writable queue. It is intentionally a
manual operator procedure. The shipped command can inspect and render a dry-run report;
it cannot apply or roll back.

Markdown/jsonl import is a different operation. Use `bonus-drain import-legacy` for those
files. Never pass them to `bonus-drain migrate`.

## 1. Inventory and dry run

Record the old DB path, validated new config path, target XDG paths, and all old scout,
anchor, refresh, and viewer units. Then run:

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

## 3. Back up

Create a timestamped directory on the same trusted host. Preserve modes and copy:

- the DB plus any `-wal` and `-shm` files after writers are quiesced;
- the old and new config files, excluding resolved secret values;
- the unit files and the `unit-files.before.txt`, `timers.before.txt`, and
  `units.before.txt` inventories;
- the installed-version/current-link inventory.

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
5. Run one `bonus-drain refresh --json`; verify every account independently and ensure a
   failed reader did not replace a last-good cache.
6. Run `gates --json` and `plan --json`. Do not dispatch until provider/account identities,
   reset times, and closed/open reasons are expected.
7. Enable only `bonus-drain-refresh.timer`. Observe one successful refresh, then enable
   `bonus-drain-scout.timer`.
8. Keep old writer units masked through at least one observed scheduling cycle.

Do not enable the viewer until its local or remote mode has been reviewed against
[SECURITY.md](SECURITY.md).

## 5. Acceptance evidence

Retain the exact commands and output proving:

- one XDG database is used by the stable CLI and every enumerated consumer;
- old writers are inactive and masked, and no old DB writer remains;
- config, DB, unit-state, and version backups exist and pass read-back checks;
- refresh writes only atomic `0600` snapshots and retains a last-good snapshot on failure;
- scout and viewers perform no foreground provider read;
- `doctor` has no ambiguous claim requiring reconciliation;
- the new refresh/scout timer identities and next-fire times are correct.

## Rollback

Rollback is also manual:

1. Disable, stop, and mask the new scout and refresh timers/services. Stop the viewer.
2. Prove no new process has the queue DB, WAL, or SHM open for writing.
3. Preserve the failed-cutover DB and logs separately for diagnosis.
4. Restore the verified DB/config backup with its recorded modes.
5. Restore old unit files and their exact enabled/masked state from the inventory.
6. Run the old read-only status/doctor checks before unmasking any writer.
7. Unmask and start only the previously active old units, then prove one writer topology.

Do not uninstall the new runtime until rollback evidence is complete. Runtime uninstall
preserves XDG state and refuses unknown/changed installed files, so it is not a substitute
for restoring a DB backup.
