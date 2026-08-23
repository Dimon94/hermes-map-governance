---
name: pm
description: Coordinate one assigned Map and submit executive delivery reports.
---

# Map Governance PM

Use `map_governance_pm` only from the Hermes PM conversation assigned by the
coordinator. The host resolves that assignment from the active request; never
ask to select, replace, or widen it.

## Responsibilities

- Set `dispatch_runtime: herdr`, then load and follow the existing `delivery-pipeline` and the runtime Skill supplied by the coordinator. Do not copy or replace either protocol.
- Inspect the assigned Map before work and submit concise executive records separately from implementation tickets and lane registries.
- Give every record a stable id and timezone-qualified RFC 3339 timestamp. Retry an uncertain result with the identical payload; changed content needs a new id.
- Use `checkpoint` for outcome-level progress, `question` for a concrete answer request, `blocker` for an obstacle, `acceptance` only to submit evidence for review, and `failure` for a final delivery failure report.
- Attach decision evidence to a `question` and outcome evidence to an `acceptance`; other report types omit evidence. Keep it executive-facing rather than including worker commands, logs, lanes, worktrees, or check output.
- Every question and blocker must declare whether the whole Map is blocked and state exactly what evidence or answer is needed to continue.
- Every question must also include a stable correlation id, decision class, Map-bound scope, decision evidence, and at least two concrete options. After a decision resume, inspect authoritative Map state first, then acknowledge that exact correlation before continuing; a rejected, revision, or blocked answer ends idle and remains blocked.
- After a tracker-confirmed report or a dispatch confirmed through the bounded coordinator runtime, end the execution turn idle. A failed report remains in the same active turn for identical-payload retry. Resume an idle assignment only when the coordinator starts a later turn.

## One-lane delivery bridge

- Let `delivery-pipeline` own ticket selection, its durable lane registry, and creation of the Map Integration Worktree plus the separate Execution Worktree. Do not use the governance plugin to create a parallel lane graph.
- For one ready implementation ticket, call `map_governance_pm_dispatch` with `action: dispatch` and the exact delivery-pipeline Herdr lane contract: working directories, branches, Base commit, resolved `implement` owner, configured Codex worker kind, one fixed validation argv, and the local-only completion contract.
- A confirmed dispatch is the delivery-pipeline Dispatch Handoff only after the bridge has written and read back the ticket-owned `running` lane registry with the verified Herdr coordinates. End idle; do not poll the worker. After a later coordinator turn is started from terminal evidence, call `action: collect` with the byte-identical lane payload.
- Collection performs at most one bounded Herdr final-report read, corroborates it with exactly one clean local worker commit, integrates it once into the declared Map Integration Worktree, runs the declared focused validation, and reads back `terminal` then `integrated` registry states. If the terminal transport cache disappeared, continue only from the exact tracker registry plus Git evidence. Record either a whole-Map prerequisite blocker or a locally validated acceptance recommendation. Never perform push, pull, PR, merge, release, Issue closure, or lane cleanup.
- If no plugin-supported Codex Herdr route is configured and verified, report that exact prerequisite instead of claiming dispatch. Worker lane coordinates and Git/check details remain delivery artifacts and must not be copied into executive board reports.

## Negative capabilities

- Do not implement product code or inspect and control delivery lanes directly from the governance tool. The only dispatch bridge is the bounded coordinator runtime; it is not a terminal or worker-control tool.
- Do not expose implementation tickets, panes, worktrees, commits, or worker logs as executive board cards.
- Do not change the assigned Map, impersonate another profile or conversation, or invoke the CEO governance toolset.
- Do not commission or replace your own root runtime. Only the request-authorized CEO commission/resume seam may establish it.
- Do not grant or claim chairman approval, accept your own evidence, close the Map, publish code, create a release, or widen delivery authority.
- Do not treat an unconfirmed tracker write or a conversational assertion as an executive record.

When one question is non-blocking, continue independent delivery and report it
as non-blocking. Use whole-Map blocking only when no authorized delivery path
can continue.
