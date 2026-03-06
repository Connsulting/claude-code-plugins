---
description: Identify topic-level knowledge silos from indexed learnings
---

# Knowledge Silo Detector Command

Analyze indexed learnings for concentration risk by topic.

## Usage

```bash
/knowledge-silo-detector
```

## Your Task

1. Run the detector in human-readable mode:

```bash
python3 [plugin-path]/scripts/detect-knowledge-silos.py --format text
```

2. If the user requests machine-readable output or automation, run:

```bash
python3 [plugin-path]/scripts/detect-knowledge-silos.py --format json
```

3. Present results using this structure:

```markdown
## Knowledge Silo Report

Assumption: repository scope approximates team/domain boundaries when explicit team metadata is unavailable.

### Summary
- Indexed learnings: <count>
- Topics analyzed: <count>
- Findings: <count>
- Thresholds: repo >= <value>, contributor >= <value>

### Findings (risk-ranked)
1. Topic: <topic>
   - Risk level: <high|medium>
   - Repo concentration: <repo> <share> (<count>/<samples>)
   - Contributor concentration: <author> <share> (<count>/<samples>)
   - Recommendations:
     - <action 1>
     - <action 2>

### Notes
- If no indexed data exists, instruct the user to run `/index-learnings` first.
```

## Options

Pass thresholds explicitly when requested:

```bash
python3 [plugin-path]/scripts/detect-knowledge-silos.py \
  --min-topic-samples 5 \
  --repo-dominance-threshold 0.75 \
  --author-dominance-threshold 0.70 \
  --format text
```

## Guardrails

- Read-only analysis only. Do not modify indexed learnings or source files.
- Keep findings sorted by risk score.
- Include recommendations for each finding.
