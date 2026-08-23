"""PM Skill and role-specific executive reporting Tool registration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .application import (
    GovernanceAuthorizationError,
    GovernanceRequestIdentity,
    MapBindingError,
    PMReportConflict,
    TrackerPMReportConfirmationError,
)
from .reports import PMReportDraft
from .runtime import application_for_profile
from .tracker import TrackerError


PM_TOOL_NAME = "map_governance_pm"
PM_TOOLSET = "map-governance-pm"
PM_TOOL_BRIDGES = frozenset({"tool_search", "tool_describe", "tool_call"})
PM_COORDINATOR_RUNTIME_BRIDGES = frozenset({"map_governance_pm_dispatch"})
PM_SKILL = Path(__file__).resolve().parents[1] / "skills" / "pm" / "SKILL.md"

PM_TOOL_SCHEMA = {
    "name": PM_TOOL_NAME,
    "description": "Inspect the assigned initiative or submit one executive report.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["inspect", "report"]},
            "report": {
                "type": "object",
                "properties": {
                    "record_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "type": {
                        "type": "string",
                        "enum": [
                            "checkpoint",
                            "question",
                            "blocker",
                            "acceptance",
                            "failure",
                        ],
                    },
                    "summary": {"type": "string", "minLength": 1},
                    "timestamp": {"type": "string", "format": "date-time"},
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                    "blocking": {"type": "boolean"},
                    "continuation_requirement": {
                        "type": "string",
                        "minLength": 1,
                    },
                    "failure_code": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                    },
                },
                "required": ["record_id", "type", "summary", "timestamp"],
                "additionalProperties": False,
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def register_pm_capabilities(ctx) -> None:
    """Register the opt-in PM Skill and its independent named Toolset."""
    registered_profile = str(ctx.profile_name)

    def handle_pm_tool(
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
            if action == "inspect":
                result = application.pm_state(request_identity=identity)
            elif action == "report":
                raw_report = arguments.get("report")
                if not isinstance(raw_report, dict):
                    raise ValueError("report is required for the report action")
                draft = dict(raw_report)
                draft["report_type"] = draft.pop("type", None)
                result = application.report_pm(
                    request_identity=identity,
                    report=PMReportDraft(**draft),
                )
            else:
                raise ValueError("action must be inspect or report")
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except (GovernanceAuthorizationError, PMReportConflict) as error:
            return json.dumps({"error": error.as_dict()}, sort_keys=True)
        except TrackerPMReportConfirmationError as error:
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

    def enforce_pm_toolset(
        *,
        tool_name: str = "",
        session_id: str = "",
        **_kwargs: Any,
    ) -> dict[str, str] | None:
        application = application_for_profile(registered_profile)
        try:
            application.enforce_assigned_pm_toolset(
                request_identity=GovernanceRequestIdentity(
                    profile_name=registered_profile,
                    session_id=str(session_id or ""),
                ),
                tool_name=str(tool_name or ""),
                allowed_tool_names=(
                    frozenset({PM_TOOL_NAME})
                    | PM_TOOL_BRIDGES
                    | PM_COORDINATOR_RUNTIME_BRIDGES
                ),
            )
        except GovernanceAuthorizationError:
            return {
                "action": "block",
                "message": (
                    "Assigned PM sessions may only use PM governance or its bounded "
                    "coordinator bridge."
                ),
            }
        return None

    ctx.register_skill(
        "pm",
        PM_SKILL,
        "Coordinate delivery and submit concise executive reports for one assignment.",
    )
    ctx.register_tool(
        name=PM_TOOL_NAME,
        toolset=PM_TOOLSET,
        schema=PM_TOOL_SCHEMA,
        handler=handle_pm_tool,
        description=PM_TOOL_SCHEMA["description"],
        emoji="📍",
    )
    ctx.register_hook("pre_tool_call", enforce_pm_toolset)
