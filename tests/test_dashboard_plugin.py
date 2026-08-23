from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from map_governance import (
    CEOSessionRepairRequired,
    MapTransitionError,
    StaleProjectionError,
)
from map_governance.tracker import TrackerError

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_dashboard_adapter():
    module_name = "map_governance_test_dashboard_adapter"
    module_path = PLUGIN_ROOT / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load dashboard adapter from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_rest_health_and_board_delegate_with_explicit_profile(
    tmp_path, monkeypatch, hermes_host_root
):
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    adapter = _load_dashboard_adapter()
    api = FastAPI()
    api.include_router(adapter.router, prefix="/api/plugins/map-governance")
    requested_profiles = []

    class ApplicationProbe:
        def health(self):
            return {"operation": "health"}

        def board(self):
            return {"operation": "board"}

    def application_for_profile(profile):
        requested_profiles.append(profile)
        return ApplicationProbe()

    monkeypatch.setattr(adapter, "application_for_profile", application_for_profile)

    async def exercise_routes():
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            health = await client.get("/api/plugins/map-governance/health?profile=ceo")
            board = await client.get("/api/plugins/map-governance/board?profile=ceo")
            unscoped = await client.get("/api/plugins/map-governance/health")
        return health, board, unscoped

    health_response, board_response, unscoped_response = asyncio.run(exercise_routes())

    assert health_response.status_code == 200
    assert health_response.json() == {"operation": "health"}
    assert board_response.status_code == 200
    assert board_response.json() == {"operation": "board"}
    assert unscoped_response.status_code == 422
    assert requested_profiles == ["ceo", "ceo"]


def test_event_stream_contract_uses_hermes_isolated_dependencies(hermes_host_root):
    runtime = hermes_host_root / "venv" / "bin" / "python"
    assert runtime.is_file(), "Hermes isolated Python runtime is required"
    for mode in ("catch-up", "expired", "reconnect", "slow"):
        result = subprocess.run(
            [
                str(runtime),
                str(PLUGIN_ROOT / "tests" / "dashboard_stream_probe.py"),
                str(PLUGIN_ROOT),
                mode,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == f"dashboard stream {mode} ready\n"


def test_event_stream_disconnects_a_blocked_consumer_without_holding_a_writer(
    monkeypatch,
):
    adapter = _load_dashboard_adapter()

    class ApplicationProbe:
        board_stream_settings = SimpleNamespace(
            batch_size=1,
            poll_seconds=0.01,
            send_timeout_seconds=0.001,
        )

        def board_events(self, *, cursor, limit):
            return {
                "status": "open",
                "events": [],
                "cursor": cursor,
                "latest_cursor": cursor,
                "has_more": False,
            }

    class BlockedWebSocket:
        query_params = {"profile": "ceo", "cursor": "0"}

        def __init__(self):
            self.close_code = None

        async def accept(self):
            return None

        async def send_json(self, _payload):
            await asyncio.Event().wait()

        async def close(self, *, code):
            self.close_code = code

    monkeypatch.setattr(adapter, "_ws_upgrade_authorized", lambda _ws: True)
    monkeypatch.setattr(
        adapter, "application_for_profile", lambda _profile: ApplicationProbe()
    )
    socket = BlockedWebSocket()

    asyncio.run(adapter.stream_events(socket))

    assert socket.close_code == 1013


def test_profile_scoped_applications_use_isolated_storage(
    tmp_path, monkeypatch, hermes_host_root
):
    hermes_home = tmp_path / "hermes-home"
    worker_home = hermes_home / "profiles" / "worker"
    worker_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    adapter = _load_dashboard_adapter()

    default_report = adapter.application_for_profile("default").health()
    worker_report = adapter.application_for_profile("worker").health()
    default_database = Path(default_report["components"]["storage"]["database"])
    worker_database = Path(worker_report["components"]["storage"]["database"])

    assert default_database != worker_database
    assert default_database.is_relative_to(hermes_home / "plugin-data")
    assert worker_database.is_relative_to(worker_home / "plugin-data")


def test_rest_binding_and_refresh_routes_delegate_with_explicit_profile(
    tmp_path, monkeypatch, hermes_host_root
):
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    adapter = _load_dashboard_adapter()
    api = FastAPI()
    api.include_router(adapter.router, prefix="/api/plugins/map-governance")
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
        adapter,
        "application_for_profile",
        lambda profile: calls.append(("profile", profile)) or ApplicationProbe(),
    )

    async def exercise_routes():
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            configured = await client.post(
                "/api/plugins/map-governance/projects?profile=ceo",
                json={"project_url": "https://github.com/orgs/acme/projects/7"},
            )
            bound = await client.post(
                "/api/plugins/map-governance/bindings?profile=ceo",
                json={
                    "project_id": "PVT_acme_7",
                    "issue_url": "https://github.com/acme/atlas/issues/41",
                },
            )
            refreshed = await client.post(
                "/api/plugins/map-governance/refresh?profile=ceo",
                json={"project_id": "PVT_acme_7"},
            )
            transitioned = await client.post(
                "/api/plugins/map-governance/transitions?profile=ceo",
                json={
                    "map_id": "I_atlas_41",
                    "expected_stage": "authorized",
                    "requested_stage": "delivery",
                },
            )
            detail = await client.get(
                "/api/plugins/map-governance/maps/I_atlas_41?profile=ceo"
            )
            opened = await client.post(
                "/api/plugins/map-governance/maps/I_atlas_41/session?profile=ceo"
            )
            outbox_status = await client.get(
                "/api/plugins/map-governance/outbox/effect-001?profile=ceo"
            )
            recovered = await client.post(
                "/api/plugins/map-governance/outbox/recover?profile=ceo",
                json={"limit": 12},
            )
            repaired = await client.post(
                "/api/plugins/map-governance/outbox/effect-001/repair?profile=ceo",
                json={"repair_id": "repair-001", "note": "Verified downstream."},
            )
        return (
            configured,
            bound,
            refreshed,
            transitioned,
            detail,
            opened,
            outbox_status,
            recovered,
            repaired,
        )

    responses = asyncio.run(exercise_routes())
    (
        configured,
        bound,
        refreshed,
        transitioned,
        detail,
        opened,
        outbox_status,
        recovered,
        repaired,
    ) = responses

    assert configured.status_code == 200
    assert bound.status_code == 200
    assert refreshed.status_code == 200
    assert transitioned.status_code == 200
    assert detail.status_code == 200
    assert opened.status_code == 200
    assert outbox_status.status_code == 200
    assert recovered.status_code == 200
    assert repaired.status_code == 200
    assert [
        configured.json(),
        bound.json(),
        refreshed.json(),
        transitioned.json(),
        detail.json(),
        opened.json(),
        outbox_status.json(),
        recovered.json(),
        repaired.json(),
    ] == [
        {"operation": "configure_project"},
        {"operation": "bind_map"},
        {"operation": "refresh"},
        {"operation": "transition_map"},
        {"operation": "map_detail"},
        {"operation": "open_map"},
        {"operation": "outbox_status"},
        {"operation": "recover_outbox"},
        {"operation": "repair_outbox"},
    ]
    assert calls == [
        ("profile", "ceo"),
        (
            "configure_project",
            {"project_url": "https://github.com/orgs/acme/projects/7"},
        ),
        ("profile", "ceo"),
        (
            "bind_map",
            {
                "project_id": "PVT_acme_7",
                "issue_url": "https://github.com/acme/atlas/issues/41",
            },
        ),
        ("profile", "ceo"),
        ("refresh", {"project_id": "PVT_acme_7"}),
        ("profile", "ceo"),
        (
            "transition_map",
            {
                "map_id": "I_atlas_41",
                "expected_stage": "authorized",
                "requested_stage": "delivery",
            },
        ),
        ("profile", "ceo"),
        ("map_detail", {"map_id": "I_atlas_41"}),
        ("profile", "ceo"),
        ("open_map", {"map_id": "I_atlas_41"}),
        ("profile", "ceo"),
        ("outbox_status", {"effect_id": "effect-001"}),
        ("profile", "ceo"),
        ("recover_outbox", {"limit": 12}),
        ("profile", "ceo"),
        (
            "repair_outbox",
            {
                "effect_id": "effect-001",
                "repair_id": "repair-001",
                "note": "Verified downstream.",
            },
        ),
    ]


def test_rest_transition_preserves_policy_and_tracker_failure_details(
    tmp_path, monkeypatch, hermes_host_root
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    adapter = _load_dashboard_adapter()
    api = FastAPI()
    api.include_router(adapter.router, prefix="/api/plugins/map-governance")

    class ApplicationProbe:
        def transition_map(self, *, map_id, expected_stage, requested_stage):
            if requested_stage == "acceptance":
                raise MapTransitionError(
                    current_stage="authorized",
                    requested_stage=requested_stage,
                    reason="acceptance can only be entered from delivery",
                )
            if requested_stage == "parked":
                raise StaleProjectionError(
                    project_id="PVT_acme_7",
                    source="tracker",
                    last_success_at="2026-08-23T07:30:00Z",
                    reason="Tracker authority is unreachable",
                )
            raise TrackerError("GitHub write failed; verify issue-write permission")

    monkeypatch.setattr(
        adapter,
        "application_for_profile",
        lambda profile: ApplicationProbe(),
    )

    async def exercise_routes():
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            invalid = await client.post(
                "/api/plugins/map-governance/transitions?profile=ceo",
                json={
                    "map_id": "I_atlas_41",
                    "expected_stage": "authorized",
                    "requested_stage": "acceptance",
                },
            )
            failed = await client.post(
                "/api/plugins/map-governance/transitions?profile=ceo",
                json={
                    "map_id": "I_atlas_41",
                    "expected_stage": "authorized",
                    "requested_stage": "delivery",
                },
            )
            stale = await client.post(
                "/api/plugins/map-governance/transitions?profile=ceo",
                json={
                    "map_id": "I_atlas_41",
                    "expected_stage": "authorized",
                    "requested_stage": "parked",
                },
            )
        return invalid, failed, stale

    invalid, failed, stale = asyncio.run(exercise_routes())

    assert invalid.status_code == 409
    assert invalid.json()["detail"] == {
        "type": "invalid_transition",
        "current_stage": "authorized",
        "requested_stage": "acceptance",
        "reason": "acceptance can only be entered from delivery",
        "retryable": False,
    }
    assert failed.status_code == 502
    assert failed.json()["detail"] == (
        "GitHub write failed; verify issue-write permission"
    )
    assert stale.status_code == 503
    assert stale.json()["detail"] == {
        "type": "stale_projection",
        "project_id": "PVT_acme_7",
        "source": "tracker",
        "last_success_at": "2026-08-23T07:30:00Z",
        "reason": "Tracker authority is unreachable",
        "recovery": (
            "Reconnect tracker authority and complete authoritative reconcile."
        ),
        "retryable": True,
    }


def test_rest_session_open_preserves_repair_required_detail(
    tmp_path, monkeypatch, hermes_host_root
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    adapter = _load_dashboard_adapter()
    api = FastAPI()
    api.include_router(adapter.router, prefix="/api/plugins/map-governance")

    class ApplicationProbe:
        def open_map(self, *, map_id):
            raise CEOSessionRepairRequired(
                reason="multiple_exact_canonical_sessions",
                candidate_count=2,
            )

    monkeypatch.setattr(
        adapter,
        "application_for_profile",
        lambda profile: ApplicationProbe(),
    )

    async def exercise_route():
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.post(
                "/api/plugins/map-governance/maps/I_atlas_41/session?profile=ceo"
            )

    response = asyncio.run(exercise_route())

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "type": "repair_required",
        "reason": "multiple_exact_canonical_sessions",
        "candidate_count": 2,
        "retryable": False,
    }


def test_rest_chairman_decision_uses_authenticated_request_identity(
    tmp_path, monkeypatch, hermes_host_root
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    adapter = _load_dashboard_adapter()
    api = FastAPI()

    @api.middleware("http")
    async def authenticated_chairman(request, call_next):
        request.state.session = SimpleNamespace(
            user_id="chairman-1",
            provider="basic",
        )
        return await call_next(request)

    api.include_router(adapter.router, prefix="/api/plugins/map-governance")
    calls = []

    class ApplicationProbe:
        def decide_approval(self, **arguments):
            calls.append(("decide_approval", arguments))
            return {"operation": "decide_approval"}

        def transition_map(self, **arguments):
            calls.append(("transition_map", arguments))
            return {"operation": "transition_map"}

    monkeypatch.setattr(
        adapter,
        "application_for_profile",
        lambda profile: ApplicationProbe(),
    )

    async def exercise_routes():
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            decision = await client.post(
                "/api/plugins/map-governance/maps/I_atlas_41/approvals/approval-1/decision?profile=ceo",
                json={"decision": "approved", "note": "Approve exact scope."},
            )
            transition = await client.post(
                "/api/plugins/map-governance/transitions?profile=ceo",
                json={
                    "map_id": "I_atlas_41",
                    "expected_stage": "awaiting-approval",
                    "requested_stage": "authorized",
                    "approval_request_id": "approval-1",
                    "mutation_id": "transition-1",
                },
            )
        return decision, transition

    decision, transition = asyncio.run(exercise_routes())

    assert decision.status_code == transition.status_code == 200
    actor = calls[0][1]["actor_identity"]
    assert actor.role == "chairman"
    assert actor.profile_name == "ceo"
    assert actor.actor_id == "basic:chairman-1"
    assert actor.session_id == "dashboard:basic:chairman-1"
    assert calls[1][1]["actor_identity"] == actor
    assert calls[1][1]["approval_request_id"] == "approval-1"
    assert calls[1][1]["mutation_id"] == "transition-1"


def test_rest_chairman_decision_rejects_missing_interactive_identity(
    tmp_path, monkeypatch, hermes_host_root
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    adapter = _load_dashboard_adapter()
    api = FastAPI()
    api.include_router(adapter.router, prefix="/api/plugins/map-governance")
    monkeypatch.setattr(
        adapter,
        "application_for_profile",
        lambda profile: (_ for _ in ()).throw(AssertionError("must not delegate")),
    )

    async def exercise_route():
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                "/api/plugins/map-governance/maps/I_atlas_41/approvals/approval-1/decision?profile=ceo",
                json={"decision": "approved", "note": "Forged."},
            )

    response = asyncio.run(exercise_route())
    assert response.status_code == 403
