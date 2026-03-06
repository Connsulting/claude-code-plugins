# Compound Learning Privacy Policy

This document defines privacy claims for the `compound-learning` plugin and maps each claim to either:

- `automated` enforcement via `privacy-policy-claims.json` and `scripts/check-privacy-policy.py`
- `manual_review` when static checks are not sufficient

## Scope

These claims cover only behavior in this repository's plugin code and default configuration.
They do not guarantee upstream Claude CLI/service behavior, host OS controls, or third-party model routing.

## Claims

| Claim ID | Claim | Enforcement |
| --- | --- | --- |
| `CL-PRIV-001` | Default SQLite database location is local (`${HOME}/.claude/compound-learning.db`). | automated |
| `CL-PRIV-002` | Observability logging is disabled by default. | automated |
| `CL-PRIV-003` | Observability log default path is local (`~/.claude/plugins/compound-learning/observability.jsonl`). | automated |
| `CL-PRIV-004` | Session dedupe state is stored as local `.seen` files under `~/.claude/plugins/compound-learning/sessions`, with 24-hour pruning. | automated |
| `CL-PRIV-005` | `extract-learnings` and `auto-peek` hooks skip in subprocess mode (`CLAUDE_SUBPROCESS`) to avoid recursive hook processing. | automated |
| `CL-PRIV-006` | Auto-extraction subprocess uses minimal declared Claude tool permissions: `Write,Bash(mkdir:*)`. | automated |
| `CL-PRIV-007` | Hook activity logs are written to local filesystem paths under user home (with local fallback directories). | automated |
| `CL-PRIV-900` | No additional external transport beyond configured Claude runtime can be guaranteed statically. | manual_review |
| `CL-PRIV-901` | Learning content may include transcript-derived information; retention/access controls depend on local environment setup. | manual_review |

## Verification

Run:

```bash
python3 scripts/check-privacy-policy.py
```

The checker exits non-zero if any `automated` claim fails.
