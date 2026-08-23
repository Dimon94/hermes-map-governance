from __future__ import annotations

import json
from types import SimpleNamespace

import map_governance.ceo_tool as ceo_tool
from map_governance import GovernanceRequestIdentity


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
