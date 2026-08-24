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

        def commission_map(self, **arguments):
            calls.append(("commission_map", arguments))
            return {"operation": "commission_map"}

        def runtime_status(self, **arguments):
            calls.append(("runtime_status", arguments))
            return {"operation": "runtime_status"}

        def outbox_status(self, **arguments):
            calls.append(("outbox_status", arguments))
            return {"operation": "outbox_status"}

        def recover_outbox(self, **arguments):
            calls.append(("recover_outbox", arguments))
            return {"operation": "recover_outbox"}

        def repair_outbox(self, **arguments):
            calls.append(("repair_outbox", arguments))
            return {"operation": "repair_outbox"}

        def publish_map(self, **arguments):
            calls.append(("publish_map", arguments))
            return {"operation": "publish_map"}

        def reconcile_publication(self, **arguments):
            calls.append(("reconcile_publication", arguments))
            return {"operation": "reconcile_publication"}

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
    monkeypatch.setattr(
        native,
        "publisher_application_for_profile",
        lambda _control, **_settings: ApplicationProbe(),
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
        parser.parse_args(
            [
                "commission",
                "--map",
                "I_atlas_41",
                "--profile",
                "ceo",
                "--session",
                "ceo-live",
            ]
        ),
        parser.parse_args(
            [
                "runtime",
                "status",
                "--map",
                "I_atlas_41",
                "--profile",
                "ceo",
                "--session",
                "ceo-live",
            ]
        ),
        parser.parse_args(
            [
                "resume",
                "--map",
                "I_atlas_41",
                "--profile",
                "ceo",
                "--session",
                "ceo-live",
            ]
        ),
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
        parser.parse_args(
            [
                "publish",
                "--map",
                "I_atlas_41",
                "--control-profile",
                "ceo",
                "--approval-request",
                "publish-main-a",
                "--mutation-id",
                "publish-main-a-action",
            ]
        ),
        parser.parse_args(
            [
                "reconcile-publication",
                "--map",
                "I_atlas_41",
                "--control-profile",
                "ceo",
                "--action-id",
                "publish-main-a-action",
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
        {"operation": "commission_map"},
        {"operation": "runtime_status"},
        {"operation": "commission_map"},
        {"operation": "outbox_status"},
        {"operation": "recover_outbox"},
        {"operation": "repair_outbox"},
        {"operation": "publish_map"},
        {"operation": "reconcile_publication"},
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
        ("profile", {"profile": "ceo"}),
        (
            "commission_map",
            {
                "map_id": "I_atlas_41",
                "request_identity": native.GovernanceRequestIdentity("ceo", "ceo-live"),
            },
        ),
        ("profile", {"profile": "ceo"}),
        (
            "runtime_status",
            {
                "map_id": "I_atlas_41",
                "request_identity": native.GovernanceRequestIdentity("ceo", "ceo-live"),
            },
        ),
        ("profile", {"profile": "ceo"}),
        (
            "commission_map",
            {
                "map_id": "I_atlas_41",
                "request_identity": native.GovernanceRequestIdentity("ceo", "ceo-live"),
            },
        ),
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
        (
            "publish_map",
            {
                "map_id": "I_atlas_41",
                "approval_request_id": "publish-main-a",
                "mutation_id": "publish-main-a-action",
            },
        ),
        (
            "reconcile_publication",
            {"map_id": "I_atlas_41", "action_id": "publish-main-a-action"},
        ),
    ]


def test_native_recovery_and_identity_repair_are_profile_scoped_and_explicit(
    tmp_path, monkeypatch, capsys
):
    calls = []

    class ApplicationProbe:
        def recover_restart(self, **arguments):
            calls.append(("recover_restart", arguments))
            return {"state": "recovered"}

        def preview_repairs(self):
            calls.append(("preview_repairs", {}))
            return {"plan_id": "identity-repair:one", "actions": []}

        def apply_repairs(self, **arguments):
            calls.append(("apply_repairs", arguments))
            return {"state": "applied"}

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
    plan = tmp_path / "repair-plan.json"
    plan.write_text(
        json.dumps({"plan_id": "identity-repair:one", "actions": []}),
        encoding="utf-8",
    )

    arguments = [
        parser.parse_args(["recover", "--profile", "ceo", "--limit", "25"]),
        parser.parse_args(["repair", "preview", "--profile", "ceo"]),
        parser.parse_args(
            [
                "repair",
                "apply",
                "--profile",
                "ceo",
                "--file",
                str(plan),
                "--action",
                "safe-action-1",
                "--authorizer",
                "basic:local-operator",
            ]
        ),
    ]

    assert [command["handler_fn"](args) for args in arguments] == [0, 0, 0]
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        {"state": "recovered"},
        {"actions": [], "plan_id": "identity-repair:one"},
        {"state": "applied"},
    ]
    assert calls == [
        ("profile", {"profile": "ceo"}),
        ("recover_restart", {"outbox_limit": 25}),
        ("profile", {"profile": "ceo"}),
        ("preview_repairs", {}),
        ("profile", {"profile": "ceo"}),
        (
            "apply_repairs",
            {
                "plan": {"plan_id": "identity-repair:one", "actions": []},
                "selected_action_ids": ["safe-action-1"],
                "authorizer": "basic:local-operator",
            },
        ),
    ]


def test_native_setup_and_doctor_use_prerequisite_seam_without_operational_storage(
    tmp_path, monkeypatch, capsys
):
    calls = []

    class PrerequisiteProbe:
        def setup_plan(self, **arguments):
            calls.append(("setup_plan", arguments))
            return {"status": "planned", "plan_id": "setup-plan:one"}

        def setup_apply(self, **arguments):
            calls.append(("setup_apply", arguments))
            return {"status": "applied", "readback": "confirmed"}

        def doctor(self):
            calls.append(("doctor", {}))
            return {"status": "pass", "checks": []}

    monkeypatch.setattr(
        native,
        "prerequisite_application_for_storage",
        lambda storage: (
            calls.append(("storage", {"storage": str(storage)})) or PrerequisiteProbe()
        ),
    )
    monkeypatch.setattr(
        native,
        "application_for_storage",
        lambda _root: (_ for _ in ()).throw(
            AssertionError("setup/doctor must not initialize operational storage")
        ),
    )
    registrations = []
    plugin_data = tmp_path / "plugin-data"
    plugin_data.mkdir()
    (plugin_data / "registry.db").write_bytes(b"existing registry")
    context = SimpleNamespace(
        state=SimpleNamespace(data_dir=plugin_data),
        profile_name="operator",
        register_skill=lambda *args, **kwargs: None,
        register_tool=lambda **kwargs: None,
        register_hook=lambda *args, **kwargs: None,
        register_cli_command=lambda **command: registrations.append(command),
    )
    native.register(context)
    parser = ArgumentParser()
    registrations[0]["setup_fn"](parser)
    desired_file = tmp_path / "desired.json"
    desired_file.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps({"plan_id": "setup-plan:one"}), encoding="utf-8")

    results = [
        registrations[0]["handler_fn"](
            parser.parse_args(["setup", "plan", "--file", str(desired_file)])
        ),
        registrations[0]["handler_fn"](
            parser.parse_args(
                [
                    "setup",
                    "apply",
                    "--file",
                    str(plan_file),
                    "--action",
                    "config.prerequisites",
                ]
            )
        ),
        registrations[0]["handler_fn"](parser.parse_args(["doctor"])),
    ]

    assert results == [0, 0, 0]
    assert [
        json.loads(line)["status"] for line in capsys.readouterr().out.splitlines()
    ] == [
        "planned",
        "applied",
        "pass",
    ]
    assert calls == [
        ("storage", {"storage": str(plugin_data)}),
        ("setup_plan", {"desired": {"schema_version": 1}}),
        ("storage", {"storage": str(plugin_data)}),
        (
            "setup_apply",
            {
                "plan": {"plan_id": "setup-plan:one"},
                "selected_action_ids": ["config.prerequisites"],
            },
        ),
        ("storage", {"storage": str(plugin_data)}),
        ("doctor", {}),
    ]


def test_native_registration_is_pure_and_first_operational_command_resumes_outbox(
    tmp_path, monkeypatch
):
    calls = []

    class ApplicationProbe:
        def start_outbox_runtime(self):
            calls.append("start")

        def health(self):
            return {"status": "ready"}

    monkeypatch.setattr(
        native,
        "application_for_storage",
        lambda root: calls.append(("build", root)) or ApplicationProbe(),
    )
    registrations = []
    storage = tmp_path / "plugin-data" / "map-governance"
    storage.mkdir(parents=True)
    (storage / "registry.db").write_bytes(b"existing-registry")
    context = SimpleNamespace(
        state=SimpleNamespace(data_dir=storage),
        profile_name="ceo",
        register_skill=lambda *args, **kwargs: None,
        register_tool=lambda **kwargs: None,
        register_hook=lambda *args, **kwargs: None,
        register_cli_command=lambda **command: registrations.append(command),
        get_config=lambda _key, default=None: default,
    )

    native.register(context)

    assert calls == []
    assert len(registrations) == 1
    result = registrations[0]["handler_fn"](SimpleNamespace(maps_command="health"))

    assert result == 0
    assert calls == [("build", storage), "start"]


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


def test_native_publish_denies_a_non_publisher_request_profile(
    tmp_path, monkeypatch, capsys
):
    def deny_publisher_profile(_control, **_settings):
        raise native.ProfileResolutionError("profile mismatch")

    monkeypatch.setattr(
        native,
        "application_for_storage",
        lambda _root: SimpleNamespace(),
    )
    monkeypatch.setattr(
        native,
        "publisher_application_for_profile",
        deny_publisher_profile,
    )
    registrations = []
    context = SimpleNamespace(
        state=SimpleNamespace(data_dir=tmp_path / "plugin-data"),
        profile_name="pm",
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
                "publish",
                "--map",
                "I_atlas_41",
                "--control-profile",
                "ceo",
                "--approval-request",
                "publish-main-a",
                "--mutation-id",
                "publish-main-a-action",
            ]
        )
    )

    assert result == 1
    assert json.loads(capsys.readouterr().err) == {
        "error": {
            "type": "publisher_profile_denied",
            "map_id": "I_atlas_41",
            "reason": "request profile has no privileged publisher capability",
            "retryable": False,
        }
    }
