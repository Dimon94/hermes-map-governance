# Hermes Map Governance Development Guide

## Product boundary

This repository owns an external standalone Hermes plugin. Do not place product code in the Hermes core repository. Integrate only through documented plugin, dashboard-extension, Skill, toolset, session, CLI, and MCP surfaces.

## Architectural invariants

- GitHub Map Issues are governance truth; local databases hold bindings, enforcement records, Outbox state, and rebuildable projections only.
- Existing Hermes Kanban tasks and worker state are not governance cards and must remain untouched.
- One Map has one canonical long-lived CEO session. Keep the system prompt and toolset stable for prompt caching; Map data enters through conversation content.
- Do not add Hermes core model tools. Register narrow plugin tools in role-specific named toolsets.
- Resolve profile, session, and Map authority from request-scoped context, never ambient process identity.
- Keep dashboard, tool, CLI, and event adapters thin over one deep Map Governance application interface.
- External writes must be idempotent and recoverable through durable intent or reconciliation.
- PM and worker execution evidence stays in delivery tickets, lane registries, Git commits, and checks rather than the executive board.

## Configuration and security

- Behavioral settings belong in normal Hermes/plugin configuration, not new non-secret environment variables.
- Separate worker authority from publisher authority. Local delivery does not imply remote publication.
- Never mutate or destroy sessions, worktrees, Herdr resources, or tracker records whose ownership is ambiguous.

## Verification

Use a temporary Hermes home for plugin discovery and profile/session integration tests. Assert behavior and invariants through the public application seam. Run the smallest focused checks during implementation and the full relevant suite before closeout.
