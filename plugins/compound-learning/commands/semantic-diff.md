---
description: Explain the semantic meaning of local git code changes
---

# Semantic Diff Command

Explain what a code diff means semantically for behavior, contracts, and risk.

## Usage

```bash
/semantic-diff
/semantic-diff --staged
/semantic-diff --working-tree
/semantic-diff main..HEAD
/semantic-diff main...feature-branch
```

## Your Task

You are the orchestrator.

1. Parse arguments from the user input:
- No args: default workspace diff (`HEAD` vs current index + working tree)
- `--staged`: staged-only diff
- `--working-tree`: unstaged-only diff
- Any non-flag token: treat as a git range and pass via `--range`
- Reject conflicting combinations (`range` with staged/working-tree, or staged + working-tree)

2. Run the collector script:

```bash
python3 [plugin-path]/scripts/semantic-diff.py --cwd "[current-working-directory]" [parsed-args]
```

3. Parse the returned JSON:
- If `status == "error"`: show `message` and `hint`, then stop.
- If `status == "empty"`: show the empty-state guidance and file counts, then stop.
- If `status == "ok"`: continue.

4. Invoke `semantic-diff-explainer` with the JSON payload and request analysis.

5. Return final output with:
- A short header line showing mode/range and changed-file counts
- The explainer's four sections:
  - Behavioral Impact
  - API / Schema / Contract Changes
  - Risk Level
  - Recommended Test Focus

## Guardrails

- Local git diffs only; do not call GitHub APIs.
- Keep explanations grounded in the provided patch snippets and stats.
- If notes indicate truncated patches, mention that confidence may be reduced for omitted context.
