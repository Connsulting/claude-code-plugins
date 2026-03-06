# Schema Evolution Advisor Plugin

`schema-evolution-advisor` is a deterministic, offline analyzer for SQL schema migrations. It scans changed migration files and flags risky evolution patterns before merge.

## Scope

This plugin focuses on static analysis of migration SQL text (primarily PostgreSQL-style DDL). It does not connect to a database and does not auto-fix migrations.

### What It Detects

- `DROP TABLE`
- `ALTER TABLE ... DROP COLUMN`
- `DROP TYPE`
- `ALTER TABLE ... ALTER COLUMN ... TYPE`
- `SET NOT NULL` changes
- `ADD COLUMN ... NOT NULL` without `DEFAULT`
- Constraint tightening (`UNIQUE`, `PRIMARY KEY`, `CHECK`, `FOREIGN KEY` additions)
- Rename heuristics for tables/columns
- Enum value rename heuristics

Findings are ranked as `high`, `medium`, or `low`, each with rationale and mitigation guidance.

## Supported SQL Dialect Boundaries

- Primary target: PostgreSQL-style DDL migrations.
- Best effort for other SQL dialects when statements are syntactically similar.
- Not a full SQL parser: highly dynamic SQL, procedural migration scripts, and engine-specific edge cases may be missed.

## Installation

From this marketplace:

```bash
/plugin marketplace add Connsulting/claude-code-plugins
/plugin install schema-evolution-advisor@connsulting-plugins
```

## Configuration

Default settings live in `.claude-plugin/config.json`:

```json
{
  "analysis": {
    "migrationGlobs": [
      "migrations/**/*.sql",
      "db/migrations/**/*.sql",
      "**/migrations/**/*.sql",
      "**/*migration*.sql"
    ],
    "minimumSeverity": "low",
    "failOnSeverity": "high",
    "defaultOutputFormat": "text",
    "maxFindings": 200
  }
}
```

## Usage

### Analyze changed migration files from git diff

```bash
python3 scripts/analyze-schema-changes.py --format text
```

### Analyze explicit files

```bash
python3 scripts/analyze-schema-changes.py --format text migrations/202603051200_add_users.sql
```

### Emit JSON for CI automation

```bash
python3 scripts/analyze-schema-changes.py --format json --fail-on medium --base-ref origin/main
```

### Force an explicit diff range

```bash
python3 scripts/analyze-schema-changes.py --diff-range origin/main...HEAD --format text
```

## Output Contract

- Text mode: human-readable summary and risk-ranked findings with mitigation hints.
- JSON mode: stable object containing:
  - `status`, `assumption`, `summary`, `files`, and flattened `findings`.

Exit codes:

- `0`: no findings at/above `--fail-on`
- `1`: at least one finding at/above `--fail-on`
- `2`: execution/configuration error

## Assumptions and Limitations

- Static analysis only; no runtime data checks (null distribution, row volume, lock timing).
- Risk ranking is heuristic and intentionally conservative for destructive changes.
- Always combine results with migration review, staging validation, and rollout/rollback planning.

## Command Integration

The command file is at `commands/schema-evolution-advisor.md` and is intended for `/schema-evolution-advisor` usage.
