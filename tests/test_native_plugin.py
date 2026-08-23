from __future__ import annotations

import json
from argparse import ArgumentParser
from pathlib import Path
from types import SimpleNamespace

import map_governance.native as native


def test_native_health_uses_context_scoped_plugin_storage(tmp_path, capsys):
    storage_root = tmp_path / "profile" / "plugin-data" / "native-namespace"
    registrations = []
    context = SimpleNamespace(
        state=SimpleNamespace(data_dir=storage_root),
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


def test_native_maps_commands_delegate_to_the_application(tmp_path, monkeypatch, capsys):
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

    monkeypatch.setattr(native, "application_for_storage", lambda root: ApplicationProbe())
    registrations = []
    context = SimpleNamespace(
        state=SimpleNamespace(data_dir=tmp_path / "plugin-data"),
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
        parser.parse_args(["board"]),
    ]

    assert [command["handler_fn"](args) for args in arguments] == [0, 0, 0, 0]
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert reports == [
        {"operation": "configure_project"},
        {"operation": "bind_map"},
        {"operation": "refresh"},
        {"operation": "board"},
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
        ("board", {}),
    ]
