---
name: note-kit-plan
description: Writes and maintains a plan note: a living checklist of the work to reach a goal, in topic sections whose items are checkboxes stating what to do and the outcome it reaches. The shape fits the task, whether an execution plan, an investigation plan, or both, and runs end to end when that is what the work needs. Research and gates appear only when the work calls for them. Activates on "make a plan", "plan out X", "write a plan for", "scope X", /note-kit-plan, and from the note-kit-handoff skill to write or update a session's plan.
---

# plan

The note-kit-plan skill writes and maintains one plan per project — a living, multi-step checklist toward a goal, kept current as the work moves.

## Generation guardrails

A plan names its gates proactively — a decision point is a written gate, not discovered mid-run. It orders items so each follows what it needs, and it writes execution intent: items runnable in order without manual verification at every step. Format-Plan is the canonical shape (CONFIG § Format notes); fetch it before authoring.

## Invocation

| mode | behavior |
|---|---|
| interactive (`/note-kit-plan`) | write or update the plan, then surface its shape and any open decisions to the user before finalizing. |
| automatic (from note-kit-handoff or an action file) | write or update the plan to the structure below; flag any item inferred rather than stated. |

In interactive mode, plan *with* the user before anything is altered: surface the gaps, ambiguities, and blind spots you see, and resolve them together until you both read the goal the same way.

## One master plan per project

A project keeps exactly one master plan, at its project root. New planning folds into that plan as new items or moves to the project's `plan` subfolder or the WIP `inbox`; never open a second plan at the project root. When redundant or unrelated plans have accumulated loose at a root, merge their live items into the master plan, mark each merged plan superseded with a forward link, and relocate it to the `plans` subfolder so one root plan remains. The analyst detects duplicates (`duplicate-canonical-plan`) and proposes merges; this skill never opens a second canonical plan for a scope.

## Items state action and outcome

Every checkbox names what to do and the outcome it reaches, in language a reader who never saw the session can act on: "Write X so Y holds" — the action is visible in the item, not only the result. An item naming an internal section, a function, or session shorthand instead of the work is the failure this skill guards against hardest. Let the filing sweep settle where the note lands and how its frontmatter reads.

## Shape the plan to the work

- **Execution plan.** When the path is known, list the work as ordered, checkable steps. End to end is correct when the task is understood end to end.
- **Investigation plan.** When viability is unknown, list the open questions and rate each as the answers arrive. The build order is written later, from the findings.
- **Both, the common case.** Settled work goes down as steps; open questions stay tracked until answered.
- **Parallel where independent.** Group work so streams touching different files run at once; sequence only where one stream feeds another or two would edit the same file. Name the few real dependencies.

## Research and gates, only when the work calls for them

Some plans need neither. Bring research in when a decision cannot be made without evidence: call the note-kit-research skill on that question, or flag the item for it, and let the rest of the plan run in parallel. Add a gate only at a real decision point, where the work after it turns on a choice made from what came before. A gate that pauses for the user in-session is a decision point written into the document. A gate that must pause and resume the run on its own writes its open questions as `<user-queue>` proposals and keeps its state in this plan file; the action-agent re-invokes the skill with this file once the user resolves them (CONFIG § Queue protocol).

## Keep the plan current

The plan is a live log of run state, not a one-time draft.

- **Check an item the moment its work lands** — after the work completes, never when the plan is first written. When possible, an agent not executing the plan verifies the work before checking it off.
- **Add items as they surface**, in the section they belong to.
- **Uncheck an item when later work reopens it**, revising the item so the box reflects the real state.
- **Revise stale framing when a decision settles.** When a gate resolves or a stream is committed, edit the items it touched so none still reads as open or provisional.
- **Mark a parked or decided item inline** with its date and reason.
- **Move completed work to a changelog** when finished items crowd the live plan, leaving the plan showing what remains. The changelog is `<Plan-Name>-Changelog`, sitting beside the plan it tracks; the opening line links it.
- **Label distinct revision passes.** A reopened plan does not un-check work completed by a full review or revision pass. Either label the old plan and move it to the `<plans>/` subfolder, or label and append the new plan to the old one.

## The note

A short opening line says what the plan covers and links the changelog when one exists. Topic sections follow, each a chunk of work under a heading that names it, checkbox items beneath. Once the shape settles, an execution-order section can group the streams that run in parallel and the few that must wait. Open decisions, where they exist, sit in their own section as checkboxes, each marked decided inline once settled.

## Output

The plan lands as a draft in `<inbox>` if brand new, under its parent or project's subfolder when present; the filename follows the plan type's naming pattern (CONFIG § Types). Stamp `parent:`/`project:` only after confirming the target exists — one vault search or project listing check; no match → leave the field empty for downstream inference, never invent a name. If the plan contradicts or supersedes an existing plan, change the filed canonical plan's frontmatter to `reviewed: false`, move it to the inbox, and append the relevant changes. Do not edit an existing plan without returning it to the `<inbox>` for review.
