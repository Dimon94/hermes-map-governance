from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import map_governance.pm_tool as pm_tool
import map_governance.runtime as runtime
from map_governance import GovernanceRequestIdentity, StaleProjectionError
from map_governance.storage import PluginStorage


def test_pm_skill_and_named_toolset_are_separate_and_hide_authority_coordinates(
    monkeypatch,
):
    calls = []

    class ApplicationProbe:
        def pm_state(self, **arguments):
            calls.append(arguments)
            return {"operation": "inspect"}

        def enforce_assigned_pm_toolset(self, **arguments):
            calls.append(arguments)
            return True

    monkeypatch.setattr(
        pm_tool,
        "application_for_pm_request",
        lambda profile, **identity: (
            calls.append({"profile": profile, **identity}) or ApplicationProbe()
        ),
    )
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "ceo")
    monkeypatch.setenv("HERMES_SESSION_ID", "environment-forgery")
    skills = []
    tools = []
    hooks = []
    context = SimpleNamespace(
        profile_name="pm",
        register_skill=lambda *args, **kwargs: skills.append((args, kwargs)),
        register_tool=lambda **kwargs: tools.append(kwargs),
        register_hook=lambda *args: hooks.append(args),
    )

    pm_tool.register_pm_capabilities(context)

    assert skills[0][0][0] == "pm"
    assert skills[0][0][1].is_file()
    registered = tools[0]
    assert registered["name"] == "map_governance_pm"
    assert registered["toolset"] == "map-governance-pm"
    assert registered["toolset"] != "map-governance-ceo"
    assert registered["schema"]["parameters"]["properties"]["action"]["enum"] == [
        "inspect",
        "report",
    ]
    schema_text = json.dumps(registered["schema"], sort_keys=True).lower()
    for forbidden in (
        "map_id",
        "profile",
        "session",
        "worker",
        "worktree",
        "terminal",
        "publish",
        "approval",
        "coordinator",
        "dispatch",
        "chairman",
    ):
        assert forbidden not in schema_text

    result = json.loads(
        registered["handler"](
            {"action": "inspect"},
            session_id="pm-session-atlas",
        )
    )

    assert result == {"operation": "inspect"}
    assert calls[:2] == [
        {"profile": "pm", "session_id": "pm-session-atlas"},
        {
            "request_identity": GovernanceRequestIdentity(
                profile_name="pm",
                session_id="pm-session-atlas",
            )
        },
    ]
    assert hooks[0][0] == "pre_tool_call"


def test_pm_report_handler_injects_request_identity_and_cannot_select_a_map(
    monkeypatch,
):
    calls = []

    class ApplicationProbe:
        def report_pm(self, **arguments):
            calls.append(arguments)
            return {"operation": "report", "type": arguments["report"].report_type}

        def enforce_assigned_pm_toolset(self, **arguments):
            calls.append(arguments)
            if arguments["tool_name"] not in arguments["allowed_tool_names"]:
                raise pm_tool.GovernanceAuthorizationError(
                    action="invoke_tool:" + arguments["tool_name"],
                    map_id="I_atlas_41",
                    reason="tool_outside_pm_toolset",
                )
            return True

    monkeypatch.setattr(
        pm_tool,
        "application_for_pm_request",
        lambda _profile, **_identity: ApplicationProbe(),
    )
    tools = []
    hooks = []
    context = SimpleNamespace(
        profile_name="pm",
        register_skill=lambda *args, **kwargs: None,
        register_tool=lambda **kwargs: tools.append(kwargs),
        register_hook=lambda *args: hooks.append(args),
    )
    pm_tool.register_pm_capabilities(context)

    result = json.loads(
        tools[0]["handler"](
            {
                "action": "report",
                "report": {
                    "record_id": "pm-checkpoint-tool-001",
                    "type": "checkpoint",
                    "summary": "The executive checkpoint is ready.",
                    "timestamp": "2026-08-23T10:20:00Z",
                },
            },
            session_id="pm-session-atlas",
        )
    )

    assert result == {"operation": "report", "type": "checkpoint"}
    assert calls[0]["request_identity"] == GovernanceRequestIdentity(
        "pm", "pm-session-atlas"
    )
    assert calls[0]["report"].record_id == "pm-checkpoint-tool-001"
    assert hooks[0][1](
        tool_name="map_governance_ceo",
        session_id="pm-session-atlas",
    ) == {
        "action": "block",
        "message": (
            "Assigned PM sessions may only use PM governance or its bounded "
            "coordinator bridge."
        ),
    }
    assert (
        hooks[0][1](
            tool_name="tool_search",
            session_id="pm-session-atlas",
        )
        is None
    )
    assert (
        hooks[0][1](
            tool_name="map_governance_pm_dispatch",
            session_id="pm-session-atlas",
        )
        is None
    )


def test_pm_tool_returns_structured_stale_interlock(monkeypatch):
    class ApplicationProbe:
        def report_pm(self, **_arguments):
            raise StaleProjectionError(
                project_id="PVT_acme_7",
                source="tracker",
                last_success_at="2026-08-23T07:30:00Z",
                reason="Tracker authority is unreachable",
            )

    monkeypatch.setattr(
        pm_tool,
        "application_for_pm_request",
        lambda _profile, **_identity: ApplicationProbe(),
    )
    tools = []
    context = SimpleNamespace(
        profile_name="pm",
        register_skill=lambda *args, **kwargs: None,
        register_tool=lambda **kwargs: tools.append(kwargs),
        register_hook=lambda *args: None,
    )
    pm_tool.register_pm_capabilities(context)

    result = json.loads(
        tools[0]["handler"](
            {
                "action": "report",
                "report": {
                    "record_id": "pm-stale",
                    "type": "checkpoint",
                    "summary": "Wait for authority.",
                    "timestamp": "2026-08-23T10:20:00Z",
                },
            },
            session_id="pm-session-atlas",
        )
    )

    assert result["error"]["type"] == "stale_projection"
    assert "authoritative reconcile" in result["error"]["recovery"]


def test_pm_request_resolves_profile_local_handoff_to_ceo_control_plane(
    tmp_path: Path, monkeypatch, hermes_host_root
):
    from hermes_cli import profiles
    from hermes_cli.plugins import PluginState
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    profile_homes = {
        "pm": tmp_path / "profiles" / "pm",
        "ceo": tmp_path / "profiles" / "ceo",
    }
    for home in profile_homes.values():
        home.mkdir(parents=True)
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name in profile_homes)
    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: profile_homes[name])
    token = set_hermes_home_override(profile_homes["pm"])
    try:
        pm_storage_root = PluginState("map-governance").data_dir
    finally:
        reset_hermes_home_override(token)
    PluginStorage(pm_storage_root).save_pm_control_plane_binding(
        profile_name="pm",
        session_id="pm-session-atlas",
        map_id="I_atlas_41",
        control_profile="ceo",
        coordinator_id="lifecycle-atlas",
        registered_at="2026-08-24T00:00:00Z",
    )
    calls = []

    class ControlPlaneProbe:
        def accepts_pm_control_binding(self, **arguments):
            calls.append(arguments)
            return True

    control_plane = ControlPlaneProbe()
    monkeypatch.setattr(
        runtime,
        "application_for_profile",
        lambda profile: control_plane if profile == "ceo" else None,
    )

    resolved = runtime.application_for_pm_request("pm", session_id="pm-session-atlas")

    assert resolved is control_plane
    assert calls == [
        {
            "request_identity": GovernanceRequestIdentity("pm", "pm-session-atlas"),
            "map_id": "I_atlas_41",
            "coordinator_id": "lifecycle-atlas",
        }
    ]
