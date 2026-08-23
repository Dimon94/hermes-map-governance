"""Executive Map stages and their governed transition policy."""

from __future__ import annotations


ACTIVE_STAGES = frozenset(
    {
        "discovery",
        "awaiting-approval",
        "authorized",
        "delivery",
        "decision",
        "acceptance",
        "parked",
    }
)

ALLOWED_TRANSITIONS = {
    "discovery": frozenset({"awaiting-approval", "parked"}),
    "awaiting-approval": frozenset({"discovery", "authorized", "parked"}),
    "authorized": frozenset({"delivery", "parked"}),
    "delivery": frozenset({"decision", "acceptance", "parked"}),
    "decision": frozenset({"delivery", "parked"}),
    "acceptance": frozenset({"delivery", "parked"}),
    "parked": frozenset({"discovery"}),
}

_ENTRY_REQUIREMENTS = {
    "awaiting-approval": "awaiting-approval can only be entered from discovery",
    "authorized": "authorized can only be entered from awaiting-approval",
    "delivery": "delivery requires authorization or a return from decision or acceptance",
    "decision": "decision can only be entered from delivery for a whole-Map blocker",
    "acceptance": "acceptance can only be entered from delivery",
    "discovery": "discovery can only be resumed from awaiting-approval or parked",
}


def available_transitions(stage: str) -> tuple[str, ...]:
    """Return stable transition choices for an executive stage."""
    return tuple(sorted(ALLOWED_TRANSITIONS.get(stage, ())))


def executive_stage(
    *,
    issue_state: str,
    state_reason: str | None,
    labels: tuple[str, ...],
) -> str:
    """Resolve tracker Issue state and label names to one executive stage."""
    if issue_state == "closed":
        return "cancelled" if state_reason == "not_planned" else "done"
    stage_labels = tuple(
        label.removeprefix("map-stage/")
        for label in labels
        if label.startswith("map-stage/")
    )
    if len(stage_labels) != 1 or stage_labels[0] not in ACTIVE_STAGES:
        raise ValueError(
            "Open Map Issue must have exactly one supported map-stage/* label"
        )
    return stage_labels[0]


def rejection_reason(current_stage: str, requested_stage: str) -> str:
    """Explain the policy rule that rejects one requested transition."""
    if current_stage in {"done", "cancelled"}:
        return "terminal Map stages can only change by reopening the tracker Issue"
    if requested_stage in {"done", "cancelled"}:
        return "terminal stages are derived from tracker closure"
    if requested_stage not in ACTIVE_STAGES:
        return "requested stage is not supported"
    if current_stage == requested_stage:
        return "the Map is already in the requested stage"
    return _ENTRY_REQUIREMENTS.get(
        requested_stage,
        f"{current_stage} cannot transition directly to {requested_stage}",
    )
