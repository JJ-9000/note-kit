---
type: format
tags:
  - format
  - standards
  - research
date: 2026-06-01
parent: "[[01-Format]]"
reviewed: true
status: complete
---

```markdown
---
type: research
tags:
  - research
  - <domain>
date: YYYY-MM-DD
project: "[[Project-Name]]"
reviewed: false
status: draft
---

# Title-Case-Hyphens

One sentence: the research question or investigation focus.

## Scope

The locked mission scope in one declarative sentence: domain, scale, exclusions.

## Findings

Structured findings, each claim carrying an inline uncertainty tag:

- `[CONFIDENT]`: multiple independent sources converge.
- `[PROVISIONAL]`: single source or low precision; a working hypothesis.
- `[INFERRED]`: derived from adjacent evidence, not directly observed.
- `[CONTESTED]`: two sources contradict at the same scope; both named.

## Open Questions

Questions this pass cannot answer, each stated specifically enough for a follow-up to address.

## Sources

Footnote definitions for every source cited above.
```
