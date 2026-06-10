---
type: format
tags:
  - format
  - standards
  - snippet
date: 2026-06-01
parent: "[[01-Format]]"
reviewed: true
status: complete
---

````markdown
---
type: snippet
tags:
  - snippet
  - <language>
  - <domain>
date: YYYY-MM-DD
parent: "[[Parent-Note]]"
reviewed: false
status: draft
---

# kebab-case-name

One sentence: what this snippet does, named concretely.

```<language>
# The functional code, ready to paste.
# Include only what is needed to run it.
# Strip scaffolding that the caller supplies.
```

## Parameters

Optional. A short list of the values the caller must supply, named and typed.

- `PARAM_NAME` (`type`): what it is and where to find it.

## Notes

Optional. One to three lines on edge cases, version requirements, or known limitations that a reader would otherwise discover by running it.
````
