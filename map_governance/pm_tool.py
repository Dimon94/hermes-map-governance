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
    StaleProjectionError,
    TrackerPMReportConfirmationError,
)
from .coordinator import (
    CommissioningPrerequisiteError,
    CoordinatorRuntimeError,
    DeliveryLaneSpec,
)
from .reports import PMReportDraft
from .runtime import application_for_pm_request
from .tracker import TrackerError


PM_TOOL_NAME = "map_governance_pm"
PM_TOOLSET = "map-governance-pm"
PM_TOOL_BRIDGES = frozenset({"tool_search", "tool_describe", "tool_call"})
PM_COORDINATOR_RUNTIME_BRIDGES = frozenset({"map_governance_pm_dispatch"})
PM_SKILL = Path(__file__).resolve().parents[1] / "skills" / "pm" / "SKILL.md"

PM_TOOL_SCHEMA = {
    "name": PM_TOOL_NAME,
    "description": (
        "Inspect the assigned initiative, submit one executive report, or acknowledge "
        "a committed decision correlation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["inspect", "report", "acknowledge_decision"],
            },
            "correlation_id": {"type": "string", "minLength": 1, "maxLength": 128},
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
                    "correlation_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                    },
                    "decision_class": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                    },
                    "scope": {"type": "object"},
                    "options": {
                        "type": "array",
                        "minItems": 2,
                        "items": {"type": "string", "minLength": 1},
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

PM_DISPATCH_TOOL_SCHEMA = {
    "name": "map_governance_pm_dispatch",
    "description": (
        "Dispatch or collect exactly one delivery-pipeline-owned Herdr "
        "implementation lane."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["dispatch", "collect"]},
            "lane": {
                "type": "object",
                "properties": {
                    "protocol": {
                        "type": "string",
                        "enum": ["delivery-pipeline/herdr-implementation-v1"],
                    },
                    "lane_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "ticket": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "minLength": 1},
                            "title": {"type": "string", "minLength": 1},
                            "url": {"type": "string", "minLength": 1},
                            "parent_spec_url": {"type": "string", "minLength": 1},
                        },
                        "required": ["id", "title", "url", "parent_spec_url"],
                        "additionalProperties": False,
                    },
                    "integration": {
                        "type": "object",
                        "properties": {
                            "worktree": {"type": "string", "minLength": 1},
                            "branch": {"type": "string", "minLength": 1},
                            "order": {"type": "integer", "minimum": 1},
                            "total": {"type": "integer", "minimum": 1},
                            "predecessor_ticket_urls": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                            },
                        },
                        "required": ["worktree", "branch"],
                        "additionalProperties": False,
                    },
                    "execution": {
                        "type": "object",
                        "properties": {
                            "working_directory": {"type": "string", "minLength": 1},
                            "branch": {"type": "string", "minLength": 1},
                            "base_commit": {
                                "type": "string",
                                "pattern": "^[0-9a-f]{40}$",
                            },
                        },
                        "required": ["working_directory", "branch", "base_commit"],
                        "additionalProperties": False,
                    },
                    "owner": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "enum": ["implement"]},
                            "skill_path": {"type": "string", "minLength": 1},
                            "invocation_label": {
                                "type": "string",
                                "enum": ["$implement"],
                            },
                        },
                        "required": ["name", "skill_path", "invocation_label"],
                        "additionalProperties": False,
                    },
                    "worker_kind": {
                        "type": "string",
                        "enum": ["codex", "claude"],
                    },
                    "validation": {
                        "type": "object",
                        "properties": {
                            "argv": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 64,
                                "items": {"type": "string", "minLength": 1},
                            }
                        },
                        "required": ["argv"],
                        "additionalProperties": False,
                    },
                    "completion_contract": {
                        "type": "string",
                        "enum": ["one-local-commit-integrated-and-validated"],
                    },
                    "known_limitations": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                },
                "required": [
                    "protocol",
                    "lane_id",
                    "ticket",
                    "integration",
                    "execution",
                    "owner",
                    "worker_kind",
                    "validation",
                    "completion_contract",
                ],
                "additionalProperties": False,
            },
        },
        "required": ["action", "lane"],
        "additionalProperties": False,
    },
}


def _delivery_lane(raw: Any) -> DeliveryLaneSpec:
    if not isinstance(raw, dict):
        raise ValueError("lane is required")

    def string_field(value: Any, path: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{path} must be a non-empty string")
        return value

    def object_field(name: str) -> dict[str, Any]:
        value = raw.get(name)
        if not isinstance(value, dict):
            raise ValueError(f"lane.{name} must be an object")
        return value

    ticket = object_field("ticket")
    integration = object_field("integration")
    execution = object_field("execution")
    owner = object_field("owner")
    validation = object_field("validation")
    validation_argv = validation.get("argv")
    predecessor_ticket_urls = integration.get("predecessor_ticket_urls", [])
    limitations = raw.get("known_limitations", [])
    if (
        not isinstance(validation_argv, list)
        or not isinstance(limitations, list)
        or not isinstance(predecessor_ticket_urls, list)
    ):
        raise ValueError("lane validation and limitations must be arrays")
    integration_order = integration.get("order", 1)
    integration_total = integration.get("total", 1)
    if (
        isinstance(integration_order, bool)
        or not isinstance(integration_order, int)
        or isinstance(integration_total, bool)
        or not isinstance(integration_total, int)
    ):
        raise ValueError("lane integration order and total must be integers")
    return DeliveryLaneSpec(
        protocol=string_field(raw.get("protocol"), "lane.protocol"),
        lane_id=string_field(raw.get("lane_id"), "lane.lane_id"),
        ticket_id=string_field(ticket.get("id"), "lane.ticket.id"),
        ticket_title=string_field(ticket.get("title"), "lane.ticket.title"),
        ticket_url=string_field(ticket.get("url"), "lane.ticket.url"),
        parent_spec_url=string_field(
            ticket.get("parent_spec_url"), "lane.ticket.parent_spec_url"
        ),
        integration_worktree=string_field(
            integration.get("worktree"), "lane.integration.worktree"
        ),
        integration_branch=string_field(
            integration.get("branch"), "lane.integration.branch"
        ),
        execution_worktree=string_field(
            execution.get("working_directory"), "lane.execution.working_directory"
        ),
        execution_branch=string_field(execution.get("branch"), "lane.execution.branch"),
        base_commit=string_field(
            execution.get("base_commit"), "lane.execution.base_commit"
        ),
        owner_skill_name=string_field(owner.get("name"), "lane.owner.name"),
        owner_skill_path=string_field(owner.get("skill_path"), "lane.owner.skill_path"),
        owner_invocation_label=string_field(
            owner.get("invocation_label"), "lane.owner.invocation_label"
        ),
        worker_kind=string_field(raw.get("worker_kind"), "lane.worker_kind"),
        validation_argv=tuple(validation_argv),
        completion_contract=string_field(
            raw.get("completion_contract"), "lane.completion_contract"
        ),
        known_limitations=tuple(limitations),
        integration_order=integration_order,
        integration_total=integration_total,
        integration_predecessor_ticket_urls=tuple(predecessor_ticket_urls),
    )


def register_pm_capabilities(ctx) -> None:
    """Register the opt-in PM Skill and its independent named Toolset."""
    registered_profile = str(ctx.profile_name)

    def handle_pm_tool(
        arguments: dict[str, Any],
        *,
        session_id: str | None = None,
        **_kwargs: Any,
    ) -> str:
        identity = GovernanceRequestIdentity(
            profile_name=registered_profile,
            session_id=str(session_id or ""),
        )
        try:
            application = application_for_pm_request(
                registered_profile, session_id=str(session_id or "")
            )
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
            elif action == "acknowledge_decision":
                correlation_id = arguments.get("correlation_id")
                if not isinstance(correlation_id, str):
                    raise ValueError(
                        "correlation_id is required for acknowledge_decision"
                    )
                result = application.acknowledge_pm_decision(
                    request_identity=identity,
                    correlation_id=correlation_id,
                )
            else:
                raise ValueError(
                    "action must be inspect, report, or acknowledge_decision"
                )
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except (
            GovernanceAuthorizationError,
            PMReportConflict,
            StaleProjectionError,
        ) as error:
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
        try:
            application = application_for_pm_request(
                registered_profile, session_id=str(session_id or "")
            )
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
        except (GovernanceAuthorizationError, ValueError):
            return {
                "action": "block",
                "message": (
                    "Assigned PM sessions may only use PM governance or its bounded "
                    "coordinator bridge."
                ),
            }
        return None

    def handle_pm_dispatch(
        arguments: dict[str, Any],
        *,
        session_id: str | None = None,
        **_kwargs: Any,
    ) -> str:
        identity = GovernanceRequestIdentity(
            profile_name=registered_profile,
            session_id=str(session_id or ""),
        )
        try:
            application = application_for_pm_request(
                registered_profile, session_id=str(session_id or "")
            )
            lane = _delivery_lane(arguments.get("lane"))
            action = arguments.get("action")
            if action == "dispatch":
                result = application.dispatch_pm_delivery_lane(
                    request_identity=identity,
                    lane=lane,
                )
            elif action == "collect":
                result = application.collect_pm_delivery_lane(
                    request_identity=identity,
                    lane=lane,
                )
            else:
                raise ValueError("action must be dispatch or collect")
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except (
            GovernanceAuthorizationError,
            PMReportConflict,
            StaleProjectionError,
            CommissioningPrerequisiteError,
            CoordinatorRuntimeError,
        ) as error:
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
        except (MapBindingError, RuntimeError, TypeError, ValueError) as error:
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
    ctx.register_tool(
        name=PM_DISPATCH_TOOL_SCHEMA["name"],
        toolset=PM_TOOLSET,
        schema=PM_DISPATCH_TOOL_SCHEMA,
        handler=handle_pm_dispatch,
        description=PM_DISPATCH_TOOL_SCHEMA["description"],
        emoji="🚚",
    )
    ctx.register_hook("pre_tool_call", enforce_pm_toolset)
