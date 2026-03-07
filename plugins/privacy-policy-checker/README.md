# Privacy Policy Checker Plugin

Deterministic static checks for machine-readable privacy-policy claims embedded in markdown policy files.

## Scope

This checker enforces only explicitly tagged claims in `PRIVACY_POLICY.md` (default) using tags like:

```markdown
[privacy-claim:no_analytics_trackers]
[privacy-claim:no_third_party_exfiltration]
[privacy-claim:no_pii_logging]
```

Free-form legal interpretation is intentionally out of scope.

## Supported claim IDs (v1)

- `no_analytics_trackers`: Flags common analytics tracker signatures (`gtag`, GA domains, Segment/Mixpanel/Amplitude/PostHog/Hotjar/FullStory markers).
- `no_third_party_exfiltration`: Flags hardcoded external `http(s)` domains unless allowlisted.
- `no_pii_logging`: Flags logging statements that include PII-like fields on the same line.

## Installation

```bash
/plugin marketplace add Connsulting/claude-code-plugins
/plugin install privacy-policy-checker@connsulting-plugins
```

## Usage

```bash
/check-privacy-policy
/check-privacy-policy --policy-path docs/PRIVACY_POLICY.md
/check-privacy-policy --format json --output reports/privacy-check.json
```

Direct script usage:

```bash
python3 plugins/privacy-policy-checker/scripts/check-privacy-policy.py --repo-root .
```

## Exit codes

- `0`: Pass (no violations, no hard errors)
- `1`: Violations found
- `2`: Input/config errors (missing policy, invalid config, unsupported claim IDs unless ignored)

## Configuration

Default config: `plugins/privacy-policy-checker/.claude-plugin/config.json`

Key options:

- `policy.defaultPath`: default policy file path
- `source.include`: include globs for scanned source files
- `source.exclude`: exclude globs (defaults include tests/vendor/build artifacts)
- `network.allowlistDomains`: domains accepted by `no_third_party_exfiltration`
- `scanner.maxFileSizeBytes`: skip oversized files

## Testing

```bash
python3 -m pytest plugins/privacy-policy-checker/tests/test_check_privacy_policy.py
```
