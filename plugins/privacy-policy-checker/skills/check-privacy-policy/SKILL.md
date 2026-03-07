# Check Privacy Policy Skill

Run deterministic static checks that validate tagged privacy-policy claims in `PRIVACY_POLICY.md` (or a provided policy path) against source files.

## Supported claims (v1)

- `no_analytics_trackers`
- `no_third_party_exfiltration`
- `no_pii_logging`

## Command

From repository root, run:

```bash
python3 plugins/privacy-policy-checker/scripts/check-privacy-policy.py --repo-root . $ARGS
```

## Accepted args

- `--policy-path <path>`
- `--format text|json`
- `--output <path>`
- `--ignore-unsupported-claims`

## Behavior

1. Read policy tags in the form `[privacy-claim:<claim_id>]`.
2. Enforce only supported claim IDs.
3. Report violations with `claim_id`, `file`, `line`, and evidence.
4. Exit with `0` for pass, `1` for violations, `2` for input/config/claim errors.

If violations exist, summarize them clearly in your response.
