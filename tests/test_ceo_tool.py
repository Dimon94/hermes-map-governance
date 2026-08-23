from __future__ import annotations

import json
from types import SimpleNamespace

import map_governance.ceo_tool as ceo_tool
from map_governance import GovernanceRequestIdentity, StaleProjectionError


def test_ceo_skill_and_named_toolset_register_with_request_scoped_identity(
    tmp_path,
    monkeypatch,
):
    calls = []

    class ApplicationProbe:
        def executive_state(self, **arguments):
            calls.append(arguments)
            return {"operation": "inspect"}

    monkeypatch.setattr(
        ceo_tool,
        "application_for_profile",
        lambda profile: calls.append({"profile": profile}) or ApplicationProbe(),
    )
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "chairman")
    monkeypatch.setenv("HERMES_SESSION_ID", "environment-forgery")
    skills = []
    tools = []
    hooks = []
    context = SimpleNamespace(
        profile_name="ceo",
        register_skill=lambda *args, **kwargs: skills.append((args, kwargs)),
        register_tool=lambda **kwargs: tools.append(kwargs),
        register_hook=lambda *args: hooks.append(args),
    )

    ceo_tool.register_ceo_capabilities(context)

    assert len(skills) == 1
    assert skills[0][0][0] == "ceo"
    assert skills[0][0][1].name == "SKILL.md"
    assert skills[0][0][1].is_file()
    assert len(tools) == 1
    assert len(hooks) == 1
    assert hooks[0][0] == "pre_tool_call"
    registered = tools[0]
    assert registered["name"] == "map_governance_ceo"
    assert registered["toolset"] == "map-governance-ceo"
    schema_text = json.dumps(registered["schema"], sort_keys=True)
    assert registered["schema"]["parameters"]["properties"]["action"]["enum"] == [
        "inspect",
        "record_decision",
        "request_approval",
        "commission",
        "resume",
        "runtime_status",
    ]
    decision_schema = registered["schema"]["parameters"]["properties"]["decision"]
    assert decision_schema["properties"]["authority"] == {
        "type": "string",
        "enum": ["ceo"],
    }
    assert "profile" not in schema_text
    assert "session" not in schema_text
    assert "worker" not in schema_text
    assert "worktree" not in schema_text
    assert "publish" not in schema_text

    result = json.loads(
        registered["handler"](
            {"action": "inspect", "map_id": "I_atlas_41"},
            session_id="canonical-live-session",
        )
    )

    assert result == {"operation": "inspect"}
    assert calls == [
        {"profile": "ceo"},
        {
            "map_id": "I_atlas_41",
            "request_identity": GovernanceRequestIdentity(
                profile_name="ceo",
                session_id="canonical-live-session",
            ),
        },
    ]


def test_ceo_tool_exposes_explicit_commission_resume_and_status(monkeypatch):
    calls = []

    class ApplicationProbe:
        def commission_map(self, **arguments):
            calls.append(("commission_map", arguments))
            return {"state": "active"}

        def runtime_status(self, **arguments):
            calls.append(("runtime_status", arguments))
            return {"state": "active", "idempotent": True}

    monkeypatch.setattr(
        ceo_tool,
        "application_for_profile",
        lambda _profile: ApplicationProbe(),
    )
    tools = []
    context = SimpleNamespace(
        profile_name="ceo",
        register_skill=lambda *args, **kwargs: None,
        register_tool=lambda **kwargs: tools.append(kwargs),
        register_hook=lambda *args: None,
    )
    ceo_tool.register_ceo_capabilities(context)
    handler = tools[0]["handler"]

    results = [
        json.loads(
            handler(
                {"action": action, "map_id": "I_atlas_41"},
                session_id="canonical-live-session",
            )
        )
        for action in ("commission", "resume", "runtime_status")
    ]

    assert results == [
        {"state": "active"},
        {"state": "active"},
        {"idempotent": True, "state": "active"},
    ]
    assert [name for name, _ in calls] == [
        "commission_map",
        "commission_map",
        "runtime_status",
    ]
    assert all(
        arguments["request_identity"]
        == GovernanceRequestIdentity("ceo", "canonical-live-session")
        for _, arguments in calls
    )


def test_ceo_tool_delegates_structured_decision_without_identity_arguments(
    monkeypatch,
):
    calls = []

    class ApplicationProbe:
        def record_decision(self, **arguments):
            calls.append(arguments)
            return {"operation": "record_decision"}

        def enforce_canonical_ceo_toolset(self, **arguments):
            calls.append(arguments)
            if arguments["tool_name"] not in arguments["allowed_tool_names"]:
                raise ceo_tool.GovernanceAuthorizationError(
                    action="invoke_tool:" + arguments["tool_name"],
                    map_id="I_atlas_41",
                    reason="tool_outside_ceo_toolset",
                )
            return True

    monkeypatch.setattr(
        ceo_tool,
        "application_for_profile",
        lambda profile: ApplicationProbe(),
    )
    registrations = []
    hooks = []
    context = SimpleNamespace(
        profile_name="ceo",
        register_skill=lambda *args, **kwargs: None,
        register_tool=lambda **kwargs: registrations.append(kwargs),
        register_hook=lambda *args: hooks.append(args),
    )
    ceo_tool.register_ceo_capabilities(context)

    result = json.loads(
        registrations[0]["handler"](
            {
                "action": "record_decision",
                "map_id": "I_atlas_41",
                "decision": {
                    "decision_id": "decision-atlas-market-001",
                    "type": "product",
                    "rationale": "Start with the research cohort.",
                    "authority": "ceo",
                    "affected_stage": "authorized",
                    "timestamp": "2026-08-23T09:25:00Z",
                    "authority_context": {
                        "decision_payload": {"cohort_size": 50},
                        "requested_scope": {"map_id": "I_atlas_41"},
                    },
                },
            },
            session_id="canonical-live-session",
        )
    )

    assert result == {"operation": "record_decision"}
    assert calls[0]["map_id"] == "I_atlas_41"
    assert calls[0]["request_identity"] == GovernanceRequestIdentity(
        "ceo", "canonical-live-session"
    )
    assert calls[0]["decision"].payload() == {
        "decision_id": "decision-atlas-market-001",
        "type": "product",
        "rationale": "Start with the research cohort.",
        "authority": "ceo",
        "affected_stage": "authorized",
        "timestamp": "2026-08-23T09:25:00Z",
        "authority_context": {
            "decision_payload": {"cohort_size": 50},
            "requested_scope": {"map_id": "I_atlas_41"},
        },
    }

    blocked = hooks[0][1](
        tool_name="terminal",
        session_id="canonical-live-session",
    )
    assert blocked == {
        "action": "block",
        "message": "Canonical CEO sessions may only use the Map Governance CEO tool.",
    }
    assert (
        hooks[0][1](
            tool_name="tool_search",
            session_id="canonical-live-session",
        )
        is None
    )


def test_ceo_tool_submits_complete_approval_packet_without_actor_override(
    monkeypatch,
):
    calls = []

    class ApplicationProbe:
        def request_approval(self, **arguments):
            calls.append(arguments)
            return {
                "operation": "request_approval",
                "payload_hash": arguments["packet"].payload_hash,
            }

        def enforce_canonical_ceo_toolset(self, **_arguments):
            return True

    monkeypatch.setattr(
        ceo_tool,
        "application_for_profile",
        lambda profile: ApplicationProbe(),
    )
    registrations = []
    context = SimpleNamespace(
        profile_name="ceo",
        register_skill=lambda *args, **kwargs: None,
        register_tool=lambda **kwargs: registrations.append(kwargs),
        register_hook=lambda *args: None,
    )
    ceo_tool.register_ceo_capabilities(context)

    result = json.loads(
        registrations[0]["handler"](
            {
                "action": "request_approval",
                "map_id": "I_atlas_41",
                "approval_packet": {
                    "request_id": "approval-delivery-001",
                    "decision_class": "delivery_authorization",
                    "proposed_action": "transition_map",
                    "alternatives": ["Authorize", "Revise"],
                    "rationale": "Delivery evidence is ready.",
                    "cost_risk": "Two engineering weeks.",
                    "evidence": ["https://github.com/acme/atlas/issues/41"],
                    "requested_scope": {"map_id": "I_atlas_41"},
                    "decision_payload": {
                        "expected_stage": "awaiting-approval",
                        "requested_stage": "authorized",
                    },
                },
            },
            session_id="canonical-live-session",
        )
    )

    assert result["operation"] == "request_approval"
    assert result["payload_hash"].startswith("sha256:")
    assert calls[0]["request_identity"] == GovernanceRequestIdentity(
        "ceo", "canonical-live-session"
    )
    assert calls[0]["packet"].request_id == "approval-delivery-001"


def test_ceo_tool_returns_structured_stale_interlock(monkeypatch):
    class ApplicationProbe:
        def record_decision(self, **_arguments):
            raise StaleProjectionError(
                project_id="PVT_acme_7",
                source="tracker",
                last_success_at="2026-08-23T07:30:00Z",
                reason="Tracker authority is unreachable",
            )

    monkeypatch.setattr(
        ceo_tool,
        "application_for_profile",
        lambda _profile: ApplicationProbe(),
    )
    tools = []
    context = SimpleNamespace(
        profile_name="ceo",
        register_skill=lambda *args, **kwargs: None,
        register_tool=lambda **kwargs: tools.append(kwargs),
        register_hook=lambda *args: None,
    )
    ceo_tool.register_ceo_capabilities(context)

    result = json.loads(
        tools[0]["handler"](
            {
                "action": "record_decision",
                "map_id": "I_atlas_41",
                "decision": {
                    "decision_id": "decision-stale",
                    "type": "product",
                    "rationale": "Wait for authority.",
                    "authority": "ceo",
                    "affected_stage": "authorized",
                    "timestamp": "2026-08-23T09:25:00Z",
                },
            },
            session_id="canonical-live-session",
        )
    )

    assert result["error"]["type"] == "stale_projection"
    assert "authoritative reconcile" in result["error"]["recovery"]
