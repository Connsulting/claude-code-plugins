-- Provider-neutral Bonus Drain queue schema.
-- QueueDB applies additive migrations for databases created by the earlier shell runtime.
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tasks (
  id                    TEXT PRIMARY KEY,
  title                 TEXT NOT NULL,
  kind                  TEXT NOT NULL CHECK (kind IN ('oneoff','recurring')),
  priority              INTEGER NOT NULL DEFAULT 2 CHECK (priority BETWEEN 0 AND 4),
  size                  TEXT,
  cadence               TEXT CHECK (cadence IN ('weekly','monthly')),
  cwd                   TEXT NOT NULL,
  goal                  TEXT NOT NULL,
  context               TEXT,
  constraints           TEXT,
  precondition          TEXT,
  done_when             TEXT,
  created_at            TEXT NOT NULL,
  active                INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  claude_only           INTEGER NOT NULL DEFAULT 0 CHECK (claude_only IN (0,1)),
  model                 TEXT,
  mcp                   TEXT,
  use_implement         INTEGER NOT NULL DEFAULT 0 CHECK (use_implement IN (0,1)),
  allowed_providers_json TEXT,
  required_capabilities_json TEXT,
  source_ref            TEXT,
  start_ref             TEXT,
  work_group            TEXT,
  depends_on_json       TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  rowid_pk        INTEGER PRIMARY KEY AUTOINCREMENT,
  task            TEXT NOT NULL,
  kind            TEXT NOT NULL,
  cycle           INTEGER NOT NULL,
  eligibility_key TEXT,
  status          TEXT NOT NULL CHECK (status IN ('dispatched','done','skipped','failed','awaiting_human')),
  ts              TEXT NOT NULL,
  received_at     TEXT,
  branch          TEXT,
  summary         TEXT,
  engine          TEXT,
  provider_id     TEXT,
  account_id      TEXT,
  router_job_id   TEXT,
  trigger         TEXT,
  attempt_id      TEXT,
  outcome_json    TEXT
);

CREATE TABLE IF NOT EXISTS dispatch_claims (
  task_id         TEXT NOT NULL,
  eligibility_key TEXT NOT NULL,
  provider_id     TEXT NOT NULL,
  account_id      TEXT,
  state           TEXT NOT NULL CHECK (state IN ('claimed','ambiguous')),
  claimed_at      TEXT NOT NULL,
  detail          TEXT,
  attempt_id      TEXT,
  PRIMARY KEY (task_id, eligibility_key),
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS activation_leases (
  task_id         TEXT NOT NULL,
  eligibility_key TEXT NOT NULL,
  provider_id     TEXT NOT NULL,
  account_id      TEXT NOT NULL,
  state           TEXT NOT NULL CHECK (state IN ('activating','active','releasing')),
  acquired_at     TEXT NOT NULL,
  attempt_id      TEXT,
  PRIMARY KEY (task_id, eligibility_key),
  FOREIGN KEY (task_id, eligibility_key)
    REFERENCES dispatch_claims(task_id, eligibility_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task);
CREATE INDEX IF NOT EXISTS idx_runs_cycle ON runs(cycle);
CREATE INDEX IF NOT EXISTS idx_claims_task ON dispatch_claims(task_id);
CREATE INDEX IF NOT EXISTS idx_activation_provider_account
  ON activation_leases(provider_id, account_id);

-- Attempts are immutable execution identities. Historical run/claim rows intentionally
-- retain NULL attempt IDs; additive initialization never invents an identity for them.
CREATE TABLE IF NOT EXISTS task_attempts (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  ordinal INTEGER NOT NULL,
  eligibility_key TEXT,
  mode TEXT NOT NULL CHECK (mode IN ('normal','retry','verification')),
  origin TEXT NOT NULL CHECK (origin IN ('normal','automatic','operator','continuation')),
  state TEXT NOT NULL CHECK (state IN ('claimed','dispatched','done','skipped','failed','awaiting_human','ambiguous','aborted')),
  recovery_of TEXT REFERENCES task_attempts(id),
  recovery_of_legacy_run_rowid INTEGER REFERENCES runs(rowid_pk),
  contract_hash TEXT NOT NULL,
  reason_code TEXT,
  reason_signature TEXT,
  outcome_json TEXT,
  created_at TEXT NOT NULL,
  terminal_at TEXT,
  UNIQUE(task_id, ordinal),
  CHECK ((mode='normal' AND recovery_of IS NULL AND recovery_of_legacy_run_rowid IS NULL)
      OR (mode!='normal' AND ((recovery_of IS NULL) != (recovery_of_legacy_run_rowid IS NULL))))
);

-- This is only the current scheduling projection. The immutable attempt rows remain the
-- source of retry counts and history.
CREATE TABLE IF NOT EXISTS task_recovery (
  task_id TEXT PRIMARY KEY REFERENCES tasks(id),
  after_attempt_id TEXT REFERENCES task_attempts(id),
  after_legacy_run_rowid INTEGER REFERENCES runs(rowid_pk),
  mode TEXT NOT NULL CHECK (mode IN ('retry','verification')),
  origin TEXT NOT NULL CHECK (origin IN ('automatic','operator')),
  state TEXT NOT NULL CHECK (state IN ('scheduled','backoff','consumed','held','exhausted')),
  consumed_by_attempt_id TEXT REFERENCES task_attempts(id),
  contract_hash TEXT NOT NULL,
  not_before TEXT,
  reason_code TEXT NOT NULL,
  reason_signature TEXT,
  detail TEXT,
  updated_at TEXT NOT NULL,
  CHECK ((after_attempt_id IS NULL) != (after_legacy_run_rowid IS NULL)),
  CHECK ((state='consumed') = (consumed_by_attempt_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS handoff_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  source_run_rowid INTEGER NOT NULL REFERENCES runs(rowid_pk),
  prior_revision_id INTEGER REFERENCES handoff_revisions(id),
  outcome_json TEXT NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_handoff_revisions_task
  ON handoff_revisions(task_id, id);

CREATE INDEX IF NOT EXISTS idx_attempts_task_ordinal ON task_attempts(task_id, ordinal);

CREATE TABLE IF NOT EXISTS usage_history (
  ts           TEXT    NOT NULL,
  provider_id  TEXT    NOT NULL,
  account_id   TEXT    NOT NULL,
  limit_id     TEXT    NOT NULL,
  used_percent REAL,
  resets_at    INTEGER,
  PRIMARY KEY(ts, provider_id, account_id, limit_id)
);
CREATE INDEX IF NOT EXISTS idx_usage_history_account
  ON usage_history(account_id, limit_id, ts);

-- Goals are waiting records, never long-lived dispatch claims. Every coordination turn
-- is an ordinary one-off task and uses the existing router/claim/terminal lifecycle.
CREATE TABLE IF NOT EXISTS goals (
  id TEXT PRIMARY KEY,
  contract_json TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL CHECK (state IN ('waiting','queued','paused','finishing','complete')),
  turn INTEGER NOT NULL DEFAULT 0,
  coordinator_task TEXT REFERENCES tasks(id),
  wait_json TEXT NOT NULL DEFAULT '[]',
  candidate_json TEXT,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goal_members (
  goal_id TEXT NOT NULL REFERENCES goals(id),
  task_id TEXT NOT NULL REFERENCES tasks(id),
  role TEXT NOT NULL CHECK (role IN ('implementation','integration','acceptance','existing')),
  managed INTEGER NOT NULL,
  candidate_json TEXT,
  contract_hash TEXT,
  PRIMARY KEY(goal_id, task_id),
  UNIQUE(task_id)
);
CREATE TABLE IF NOT EXISTS goal_turns (
  goal_id TEXT NOT NULL REFERENCES goals(id),
  turn INTEGER NOT NULL,
  task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
  contract_hash TEXT NOT NULL,
  reason TEXT NOT NULL,
  decision_json TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(goal_id, turn)
);
CREATE TABLE IF NOT EXISTS goal_steering (
  goal_id TEXT NOT NULL REFERENCES goals(id),
  revision INTEGER NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(goal_id, revision)
);
CREATE TABLE IF NOT EXISTS goal_operations (
  goal_id TEXT NOT NULL REFERENCES goals(id),
  key TEXT NOT NULL,
  turn_task TEXT NOT NULL REFERENCES tasks(id),
  intent_json TEXT NOT NULL,
  receipt_json TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(goal_id, key)
);
