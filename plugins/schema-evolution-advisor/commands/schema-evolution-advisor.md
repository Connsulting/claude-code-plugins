---
description: Analyze changed SQL schema migrations and report risky evolution patterns before merge
---

# Schema Evolution Advisor Command

Run deterministic static analysis on changed migration SQL files and produce a risk-ranked report.

## Usage

```bash
/schema-evolution-advisor
```

## Your Task

1. Run the analyzer in human-readable mode:

```bash
python3 [plugin-path]/scripts/analyze-schema-changes.py --format text
```

2. If the user requests machine-readable output, run:

```bash
python3 [plugin-path]/scripts/analyze-schema-changes.py --format json
```

3. If the user wants to analyze specific files only, pass explicit file arguments:

```bash
python3 [plugin-path]/scripts/analyze-schema-changes.py --format text path/to/migration.sql
```

4. Present findings using this structure:

```markdown
## Schema Evolution Report

### Summary
- Files analyzed: <count>
- Statements analyzed: <count>
- Findings: <count> (high=<h>, medium=<m>, low=<l>)

### Findings (risk-ranked)
1. [HIGH|MEDIUM|LOW] <file>:<line> <rule-id>
   - Message: <what is risky>
   - Rationale: <why this may break>
   - Mitigation: <safe rollout guidance>
```

## Options

Useful flags when needed:

```bash
python3 [plugin-path]/scripts/analyze-schema-changes.py \
  --format text \
  --min-severity medium \
  --fail-on medium \
  --base-ref origin/main
```

Or set an explicit diff range:

```bash
python3 [plugin-path]/scripts/analyze-schema-changes.py --diff-range origin/main...HEAD
```

## Guardrails

- Read-only analysis only. Do not modify migration files automatically.
- Keep output deterministic and sorted by severity, then file/line.
- Include mitigation guidance for each finding.
- Remind the user this is static SQL analysis and does not inspect live database state.
