-- Provider-neutral Bonus Drain queue schema.
-- QueueDB applies additive migrations for databases created by the earlier shell runtime.
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tasks (
  id                    TEXT PRIMARY KEY,
  title                 TEXT NOT NULL,
  kind                  TEXT NOT NULL CHECK (kind IN ('oneoff','recurring')),
  priority              INTEGER NOT NULL DEFAULT 2 CHECK (priority BETWEEN 0 AND 4),
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
  required_capabilities_json TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  rowid_pk        INTEGER PRIMARY KEY AUTOINCREMENT,
  task            TEXT NOT NULL,
  kind            TEXT NOT NULL,
  cycle           INTEGER NOT NULL,
  eligibility_key TEXT,
  status          TEXT NOT NULL CHECK (status IN ('dispatched','done','skipped','failed')),
  ts              TEXT NOT NULL,
  branch          TEXT,
  summary         TEXT,
  engine          TEXT,
  provider_id     TEXT,
  account_id      TEXT,
  router_job_id   TEXT
);

CREATE TABLE IF NOT EXISTS dispatch_claims (
  task_id         TEXT NOT NULL,
  eligibility_key TEXT NOT NULL,
  provider_id     TEXT NOT NULL,
  account_id      TEXT,
  state           TEXT NOT NULL CHECK (state IN ('claimed','ambiguous')),
  claimed_at      TEXT NOT NULL,
  detail          TEXT,
  PRIMARY KEY (task_id, eligibility_key),
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task);
CREATE INDEX IF NOT EXISTS idx_runs_cycle ON runs(cycle);
CREATE INDEX IF NOT EXISTS idx_claims_task ON dispatch_claims(task_id);
