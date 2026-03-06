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
| `CL-PRIV-001` | Default SQLite DB path is `${HOME}/.claude/compound-learning.db`. | automated |
| `CL-PRIV-002` | Observability default enabled state is false. | automated |
| `CL-PRIV-003` | Default observability log path is `~/.claude/plugins/compound-learning/observability.jsonl`. | automated |
| `CL-PRIV-004` | Session dedupe state is stored in `~/.claude/plugins/compound-learning/sessions/*.seen` and stale files are pruned after 24 hours. | automated |
| `CL-PRIV-005` | `extract-learnings` and `auto-peek` skip execution when `CLAUDE_SUBPROCESS` is set. | automated |
| `CL-PRIV-006` | `extract-learnings` passes `--allowedTools "Write,Bash(mkdir:*)"` to `claude -p`. | automated |
| `CL-PRIV-007` | Hook activity logs default to `~/.claude/plugins/compound-learning/activity.log` with local fallback directories. | automated |
| `CL-PRIV-900` | This repository cannot statically prove runtime network routing behavior of `claude` subprocess calls. | manual_review |
| `CL-PRIV-901` | Local file retention and access controls depend on user host configuration and operational policy. | manual_review |

## Verification

Run:

```bash
python3 scripts/check-privacy-policy.py
```

The checker exits non-zero if any `automated` claim fails.
