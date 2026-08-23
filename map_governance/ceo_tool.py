"""CEO Skill and role-specific governance Tool registration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .application import (
    GovernanceAuthorizationError,
    GovernanceRequestIdentity,
    MapBindingError,
    StructuredDecisionConflict,
    TrackerDecisionConfirmationError,
)
from .runtime import application_for_profile
from .tracker import StructuredDecision, TrackerError


CEO_TOOL_NAME = "map_governance_ceo"
CEO_TOOLSET = "map-governance-ceo"
CEO_TOOL_BRIDGES = frozenset({"tool_search", "tool_describe", "tool_call"})
CEO_SKILL = Path(__file__).resolve().parents[1] / "skills" / "ceo" / "SKILL.md"

CEO_TOOL_SCHEMA = {
    "name": CEO_TOOL_NAME,
    "description": "Inspect one governed Map or record one CEO decision.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["inspect", "record_decision"],
            },
            "map_id": {
                "type": "string",
                "minLength": 1,
                "description": "Bound GitHub Map Issue node id.",
            },
            "decision": {
                "type": "object",
                "properties": {
                    "decision_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "type": {"type": "string", "minLength": 1},
                    "rationale": {"type": "string", "minLength": 1},
                    "authority": {"type": "string", "enum": ["ceo"]},
                    "affected_stage": {"type": "string", "minLength": 1},
                    "timestamp": {
                        "type": "string",
                        "format": "date-time",
                    },
                },
                "required": [
                    "decision_id",
                    "type",
                    "rationale",
                    "authority",
                    "affected_stage",
                    "timestamp",
                ],
                "additionalProperties": False,
            },
        },
        "required": ["action", "map_id"],
        "additionalProperties": False,
    },
}


def register_ceo_capabilities(ctx) -> None:
    """Register the opt-in CEO Skill and its narrow named Toolset."""
    # Hermes registers plugin tools in a profile-keyed registry overlay. Capture
    # that immutable scope now; call-time process environment is not identity.
    registered_profile = str(ctx.profile_name)

    def handle_ceo_tool(
        arguments: dict[str, Any],
        *,
        session_id: str | None = None,
        **_kwargs: Any,
    ) -> str:
        application = application_for_profile(registered_profile)
        identity = GovernanceRequestIdentity(
            profile_name=registered_profile,
            session_id=str(session_id or ""),
        )
        try:
            action = arguments.get("action")
            map_id = arguments.get("map_id")
            if not isinstance(map_id, str) or not map_id:
                raise ValueError("map_id is required")
            if action == "inspect":
                result = application.executive_state(
                    map_id=map_id,
                    request_identity=identity,
                )
            elif action == "record_decision":
                raw_decision = arguments.get("decision")
                if not isinstance(raw_decision, dict):
                    raise ValueError("decision is required for record_decision")
                result = application.record_decision(
                    map_id=map_id,
                    request_identity=identity,
                    decision=StructuredDecision(**raw_decision),
                )
            else:
                raise ValueError("action must be inspect or record_decision")
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except (
            GovernanceAuthorizationError,
            StructuredDecisionConflict,
        ) as error:
            return json.dumps({"error": error.as_dict()}, sort_keys=True)
        except TrackerDecisionConfirmationError as error:
            return json.dumps(
                {
                    "error": {
                        "type": "tracker_confirmation_error",
                        "reason": str(error),
                        "retryable": True,
                    }
                },
                sort_keys=True,
            )
        except TrackerError as error:
            return json.dumps(
                {
                    "error": {
                        "type": "tracker_error",
                        "reason": str(error),
                        "retryable": True,
                    }
                },
                sort_keys=True,
            )
        except (MapBindingError, TypeError, ValueError) as error:
            return json.dumps(
                {
                    "error": {
                        "type": "invalid_request",
                        "reason": str(error),
                        "retryable": False,
                    }
                },
                sort_keys=True,
            )

    def enforce_ceo_toolset(
        *,
        tool_name: str = "",
        session_id: str = "",
        **_kwargs: Any,
    ) -> dict[str, str] | None:
        application = application_for_profile(registered_profile)
        try:
            application.enforce_canonical_ceo_toolset(
                request_identity=GovernanceRequestIdentity(
                    profile_name=registered_profile,
                    session_id=str(session_id or ""),
                ),
                tool_name=str(tool_name or ""),
                allowed_tool_names=frozenset({CEO_TOOL_NAME}) | CEO_TOOL_BRIDGES,
            )
        except GovernanceAuthorizationError:
            return {
                "action": "block",
                "message": (
                    "Canonical CEO sessions may only use the Map Governance CEO tool."
                ),
            }
        return None

    ctx.register_skill(
        "ceo",
        CEO_SKILL,
        "Govern one Map and persist executive decisions without implementation control.",
    )
    ctx.register_tool(
        name=CEO_TOOL_NAME,
        toolset=CEO_TOOLSET,
        schema=CEO_TOOL_SCHEMA,
        handler=handle_ceo_tool,
        description=CEO_TOOL_SCHEMA["description"],
        emoji="🧭",
    )
    ctx.register_hook("pre_tool_call", enforce_ceo_toolset)
