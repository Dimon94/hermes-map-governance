from __future__ import annotations

import json
from argparse import ArgumentParser
from pathlib import Path
from types import SimpleNamespace

import map_governance.native as native
from map_governance import (
    CEOSessionRepairRequired,
    MapTransitionError,
    StaleProjectionError,
)


def test_native_health_uses_context_scoped_plugin_storage(tmp_path, capsys):
    storage_root = tmp_path / "profile" / "plugin-data" / "native-namespace"
    registrations = []
    context = SimpleNamespace(
        state=SimpleNamespace(data_dir=storage_root),
        profile_name="ceo",
        register_skill=lambda *args, **kwargs: None,
        register_tool=lambda **kwargs: None,
        register_hook=lambda *args, **kwargs: None,
        register_cli_command=lambda **command: registrations.append(command),
    )

    native.register(context)

    command = registrations[0]
    result = command["handler_fn"](SimpleNamespace(maps_command="health"))
    report = json.loads(capsys.readouterr().out)

    assert command["name"] == "maps"
    assert result == 0
    assert report["status"] == "ready"
    assert set(report["components"]) == {
        "native",
        "dashboard",
        "application",
        "storage",
    }
    assert report["components"]["native"]["command"] == "maps health"
    assert report["components"]["dashboard"]["path"] == "/maps"
    assert Path(report["components"]["storage"]["database"]).is_relative_to(
        storage_root
    )
    assert not (tmp_path / "profile" / "kanban.db").exists()


def test_native_maps_commands_delegate_to_the_application(
    tmp_path, monkeypatch, capsys
):
    calls = []

    class ApplicationProbe:
        def configure_project(self, **arguments):
            calls.append(("configure_project", arguments))
            return {"operation": "configure_project"}

        def bind_map(self, **arguments):
            calls.append(("bind_map", arguments))
            return {"operation": "bind_map"}

        def refresh(self, **arguments):
            calls.append(("refresh", arguments))
            return {"operation": "refresh"}

        def board(self):
            calls.append(("board", {}))
            return {"operation": "board"}

        def transition_map(self, **arguments):
            calls.append(("transition_map", arguments))
            return {"operation": "transition_map"}

        def map_detail(self, **arguments):
            calls.append(("map_detail", arguments))
            return {"operation": "map_detail"}

        def open_map(self, **arguments):
            calls.append(("open_map", arguments))
            return {"operation": "open_map"}

        def outbox_status(self, **arguments):
            calls.append(("outbox_status", arguments))
            return {"operation": "outbox_status"}

        def recover_outbox(self, **arguments):
            calls.append(("recover_outbox", arguments))
            return {"operation": "recover_outbox"}

        def repair_outbox(self, **arguments):
            calls.append(("repair_outbox", arguments))
            return {"operation": "repair_outbox"}

    monkeypatch.setattr(
        native, "application_for_storage", lambda root: ApplicationProbe()
    )
    monkeypatch.setattr(
        native,
        "application_for_profile",
        lambda profile: (
            calls.append(("profile", {"profile": profile})) or ApplicationProbe()
        ),
    )
    registrations = []
    context = SimpleNamespace(
        state=SimpleNamespace(data_dir=tmp_path / "plugin-data"),
        profile_name="ceo",
        register_skill=lambda *args, **kwargs: None,
        register_tool=lambda **kwargs: None,
        register_hook=lambda *args, **kwargs: None,
        register_cli_command=lambda **command: registrations.append(command),
    )
    native.register(context)
    command = registrations[0]
    parser = ArgumentParser()
    command["setup_fn"](parser)

    arguments = [
        parser.parse_args(
            ["project", "configure", "--url", "https://github.com/orgs/acme/projects/7"]
        ),
        parser.parse_args(
            [
                "bind",
                "--project",
                "PVT_acme_7",
                "--issue",
                "https://github.com/acme/atlas/issues/41",
            ]
        ),
        parser.parse_args(["refresh", "--project", "PVT_acme_7"]),
        parser.parse_args(
            [
                "transition",
                "--map",
                "I_atlas_41",
                "--from",
                "authorized",
                "--stage",
                "delivery",
            ]
        ),
        parser.parse_args(["board"]),
        parser.parse_args(["detail", "--map", "I_atlas_41"]),
        parser.parse_args(["open", "--map", "I_atlas_41", "--profile", "ceo"]),
        parser.parse_args(["outbox", "status", "--effect", "effect-001"]),
        parser.parse_args(["outbox", "recover", "--limit", "12"]),
        parser.parse_args(
            [
                "outbox",
                "repair",
                "--effect",
                "effect-001",
                "--repair-id",
                "repair-001",
                "--note",
                "Verified downstream.",
            ]
        ),
    ]

    assert [command["handler_fn"](args) for args in arguments] == [
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    ]
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert reports == [
        {"operation": "configure_project"},
        {"operation": "bind_map"},
        {"operation": "refresh"},
        {"operation": "transition_map"},
        {"operation": "board"},
        {"operation": "map_detail"},
        {"operation": "open_map"},
        {"operation": "outbox_status"},
        {"operation": "recover_outbox"},
        {"operation": "repair_outbox"},
    ]
    assert calls == [
        (
            "configure_project",
            {"project_url": "https://github.com/orgs/acme/projects/7"},
        ),
        (
            "bind_map",
            {
                "project_id": "PVT_acme_7",
                "issue_url": "https://github.com/acme/atlas/issues/41",
            },
        ),
        ("refresh", {"project_id": "PVT_acme_7"}),
        (
            "transition_map",
            {
                "map_id": "I_atlas_41",
                "expected_stage": "authorized",
                "requested_stage": "delivery",
            },
        ),
        ("board", {}),
        ("map_detail", {"map_id": "I_atlas_41"}),
        ("profile", {"profile": "ceo"}),
        ("open_map", {"map_id": "I_atlas_41"}),
        ("outbox_status", {"effect_id": "effect-001"}),
        ("recover_outbox", {"limit": 12}),
        (
            "repair_outbox",
            {
                "effect_id": "effect-001",
                "repair_id": "repair-001",
                "note": "Verified downstream.",
            },
        ),
    ]


def test_native_transition_prints_structured_policy_failure(
    tmp_path, monkeypatch, capsys
):
    class ApplicationProbe:
        def transition_map(self, **arguments):
            raise MapTransitionError(
                current_stage="authorized",
                requested_stage=arguments["requested_stage"],
                reason="acceptance can only be entered from delivery",
            )

    monkeypatch.setattr(
        native, "application_for_storage", lambda root: ApplicationProbe()
    )
    registrations = []
    context = SimpleNamespace(
        state=SimpleNamespace(data_dir=tmp_path / "plugin-data"),
        profile_name="ceo",
        register_skill=lambda *args, **kwargs: None,
        register_tool=lambda **kwargs: None,
        register_hook=lambda *args, **kwargs: None,
        register_cli_command=lambda **command: registrations.append(command),
    )
    native.register(context)
    parser = ArgumentParser()
    registrations[0]["setup_fn"](parser)

    result = registrations[0]["handler_fn"](
        parser.parse_args(
            [
                "transition",
                "--map",
                "I_atlas_41",
                "--from",
                "authorized",
                "--stage",
                "acceptance",
            ]
        )
    )

    assert result == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": {
            "type": "invalid_transition",
            "current_stage": "authorized",
            "requested_stage": "acceptance",
            "reason": "acceptance can only be entered from delivery",
            "retryable": False,
        }
    }


def test_native_mutation_prints_structured_stale_interlock(
    tmp_path, monkeypatch, capsys
):
    class ApplicationProbe:
        def bind_map(self, **_arguments):
            raise StaleProjectionError(
                project_id="PVT_acme_7",
                source="tracker",
                last_success_at="2026-08-23T07:30:00Z",
                reason="Tracker authority is unreachable",
            )

    monkeypatch.setattr(
        native, "application_for_storage", lambda _root: ApplicationProbe()
    )
    registrations = []
    context = SimpleNamespace(
        state=SimpleNamespace(data_dir=tmp_path / "plugin-data"),
        profile_name="ceo",
        register_skill=lambda *args, **kwargs: None,
        register_tool=lambda **kwargs: None,
        register_hook=lambda *args, **kwargs: None,
        register_cli_command=lambda **command: registrations.append(command),
    )
    native.register(context)
    parser = ArgumentParser()
    registrations[0]["setup_fn"](parser)

    result = registrations[0]["handler_fn"](
        parser.parse_args(
            [
                "bind",
                "--project",
                "PVT_acme_7",
                "--issue",
                "https://github.com/acme/atlas/issues/41",
            ]
        )
    )

    assert result == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["type"] == "stale_projection"
    assert "authoritative reconcile" in error["recovery"]


def test_native_open_prints_structured_session_repair_failure(
    tmp_path, monkeypatch, capsys
):
    class ApplicationProbe:
        def open_map(self, **arguments):
            raise CEOSessionRepairRequired(
                reason="multiple_exact_canonical_sessions",
                candidate_count=2,
            )

    monkeypatch.setattr(
        native,
        "application_for_profile",
        lambda profile: ApplicationProbe(),
    )
    registrations = []
    context = SimpleNamespace(
        state=SimpleNamespace(data_dir=tmp_path / "plugin-data"),
        profile_name="ceo",
        register_skill=lambda *args, **kwargs: None,
        register_tool=lambda **kwargs: None,
        register_hook=lambda *args, **kwargs: None,
        register_cli_command=lambda **command: registrations.append(command),
    )
    native.register(context)
    parser = ArgumentParser()
    registrations[0]["setup_fn"](parser)

    result = registrations[0]["handler_fn"](
        parser.parse_args(["open", "--map", "I_atlas_41", "--profile", "ceo"])
    )

    assert result == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": {
            "type": "repair_required",
            "reason": "multiple_exact_canonical_sessions",
            "candidate_count": 2,
            "retryable": False,
        }
    }
