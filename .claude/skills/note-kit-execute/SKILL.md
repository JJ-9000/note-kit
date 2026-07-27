---
name: note-kit-execute
description: Grounds in a scope's canonical plan and the resident standards, then works the plan's open items to completion — in dependency order, one editor per file, raising anything that needs the user as a queue decision and continuing, blind-verifying the finished work with a fresh sub-agent, and leaving the plan and its project docs matching what the run did. Activates on "execute the plan", "work the open items", "run the plan for X", "pick up X and run it", /note-kit-execute, or a queue directive naming a plan to execute.
---

# execute

Plan in, worked plan out. One run reads a scope's canonical plan to its current state, works each open item in dependency order to its acceptance signal, verifies what landed through a fresh reader, and leaves the plan and its project docs reading as the next session's opening move. Work that has no plan yet starts by invoking the note-kit-plan skill.

## Definitions

| term | meaning |
| ---- | ------- |
| Scope | one project, area, or program together with the canonical plan that governs it (CONFIG § Types, `plan` row). One plan drives one run. |
| Open item | an unchecked `[ ]` checkbox on that plan or on a subordinate plan it links. |
| Acceptance signal | the observable an item's outcome produces on the path the user runs — a log line the next run writes, a detector reading, a measured delta, a screenshot of the running build. The standards that govern it are resident (CLAUDE.md § Resident standards — Deployed-Is-Not-Loaded, Verify-The-Whole-Path-The-User-Runs-Not-A-Mid-Pipeline-Proxy). |
| Wave | the items worked between two verifications — each sharing no file and no dependency with the others. |
| Editing window | one file, every approved change to it, one editor, one verification, one deploy. |
| Pass | one lease-bounded stretch of the run, from taking the lease to releasing it. |

## Ground before acting

- Fetch the scope's canonical plan, read it to its current state of play, and read the sibling and subordinate plans it links.
- Load the resident voice, design, and format standards (CLAUDE.md § Resident standards); fetch the specific standard an item's work calls for.
- Confirm each item's state against ground truth — the live code, the files on disk, the vault, the running host — before treating it as open or as landed. Inherited work runs its own battery first.
- Read the scope's open decisions and resume records; apply a decision the live record already settles, and take the disposition in CONFIG § Holds and approvals for one still open.
- Take the run lease before the first edit and release it when the pass exits (CONFIG § Concurrency); on finding a fresh lease held, defer the pass and report the holder. Size each pass to complete inside the lease's own expiry.
- On a change to the target set mid-pass, stop and defer the remainder to a settled pass (CONFIG § Concurrency).
- Write an acceptance signal onto any item carrying none, before that item executes.

## Work the open items in order

- Take the open items in dependency order — each after the item whose outcome it needs.
- Form the run's waves from the open items, and work each wave through before opening the next.
- Give each file one editor: serialize or stagger items touching the same file, and bundle every approved change to a shared file into one editing window.
- Execute each item to its stated outcome, observe its acceptance signal, then check the box and record the observation in the item's annotation.
- Archive before every destructive step (CONFIG § Versioning and archiving discipline).
- Split an item this pass cannot finish — a runtime run, an irreversible change, an out-of-vault operation: land the reversible in-vault part now and resolve the remainder by its disposition in CONFIG § Holds and approvals.
- Write one line per item to the run's log (CONFIG § Log files).
- Spawn sub-agents on the top line model, naming every tool, rule, and CONFIG citation each one needs (CONFIG § Sub-agent execution). Running inside a sub-agent, work the items serially in this context — same output, no fan-out.

## Raise the ask and continue

- Route a judgment call this run cannot settle to `<user-queue>` in the canonical item shape, then move to the next item (CONFIG § Queue protocol).
- Route a machine gate — a checkable step carrying no judgment — onto the plan.
- Resolve a blocker by its hold disposition and carry the rest of the run forward (CONFIG § Holds and approvals).
- Apply CONFIG § Rules' repeated-correction rule, and continue with the items that stand clear of the raised issue.

## Blind-verify

- At the close of a wave, spawn a fresh verifier whose input is the plan's intent and the landed artifacts alone, reporting against both.
- Give the verifier its acceptance signals to check, the standards to judge against, and every tool it needs by name (CONFIG § Sub-agent execution).
- Correct what the verification finds and re-verify, looping per CONFIG § Loop budget. At the cap, record the finding with a named resolution path and hand it back.

## Update the plan and project docs

- Annotate each landed item in the plan's own style — the date, what landed, and the signal observed — and strike work the run retired.
- Correct course on targets that moved: revise the item text so it states the real remaining work.
- Reopen an item whose acceptance signal stayed silent, naming what fired instead.
- Refresh the state-of-play block on the scope's cover, maintained in place (CONFIG § Status).
- Order the plan's open items so the next one to run stands first.

## Invocation

| mode | behavior |
| ---- | -------- |
| interactive (`/note-kit-execute`) | ground, settle the clarifying questions in chat, then work the items with the user reachable for device and live-session steps |
| unattended (a `<machine-queue>` directive, an approved `<user-queue>` item, or a scheduled dispatch) | ground, then run every open item to completion (this skill's Unattended runs section) |

## Unattended runs

- Infer past the routine gaps, raise the mission-critical decision to `<user-queue>`, and continue the run either way (CONFIG § Queue protocol).
- Work inside the guardrail floor and the standing approvals in CONFIG § Holds and approvals; queue a kit change that carries no approval (CONFIG § Self-modification).
- Size each pass to complete: split a long program across successive passes.

## Delivery discipline

Machine gates and metrics nominate; the user's eye decides. An item whose acceptance is visual or judgment-bearing is delivered as a candidate with the diagnostics that rate it, and stays open until the user rules. Delivery prose meets the resident standards (CLAUDE.md § Resident standards).

## Output

The plan and the project docs the run maintained, edited in place as living documents (CONFIG § Status). A new note the run produces lands in `<inbox>` as a `reviewed: false` draft in its type's shape (CONFIG § Types, § Format notes). Run lines land in the run's log (CONFIG § Log files). A run that closes a working session ends by invoking the note-kit-handoff skill.
