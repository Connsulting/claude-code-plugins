# {{ plan-name }}

One-sentence summary of what this plan delivers.

## Goal

What changes when this lands? Frame as an outcome the user can verify, not a list of steps. Include the why: what constraint or opportunity is driving it.

## Technical Architecture

Stack, components, data flow. Use sub-headings (###) for distinct subsystems. Diagrams welcome (paste mermaid or ASCII).

### Major components

### Data flow

### Storage / persistence

## Milestones

Tap a checkbox to mark a milestone done from the phone. State is recorded in the sidecar; the markdown is not edited.

- [ ] **M1: {{ first chunk }}** -- size: S/M/L. What is done at the end of this milestone.
- [ ] **M2: {{ second chunk }}** -- size: S/M/L. What is done.
- [ ] **M3: {{ ... }}** -- size: S/M/L.

## Approach options

When the plan has discrete options to compare, use a `compare` grid so the reviewer can see them side by side. Add `markdown="1"` so markdown still renders inside each column.

<div class="compare">
<div class="compare-col" markdown="1">
### Option A
What it does. Tradeoffs.
</div>
<div class="compare-col" markdown="1">
### Option B
What it does. Tradeoffs.
</div>
</div>

## Open Questions

For each decision, drop a `decide` (single-pick) or `decide-multi` (multi-pick) block. The reviewer taps an option and saves; the choice is recorded in the sidecar as a `decision` comment.

```decide
Which database should we use for the new pipeline?
- Postgres (existing infra, ops familiar)
- SQLite (zero ops, but breaks horizontal scaling later)
- DynamoDB (scales, but new query patterns to learn)
```

```decide-multi
Which surfaces should the feature flag cover at launch?
- API
- Admin UI
- Public web
- Mobile clients
```

Free-text questions can still go as a list and be commented on directly:

- **{{ question }}**: option A vs option B. Tradeoff: ...

## Out of scope

What you explicitly are NOT doing in this plan. Protects the scope from drift.

## References

Links to tickets, prior art, related plans.
