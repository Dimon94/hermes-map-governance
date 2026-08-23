from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import httpx
from fastapi import FastAPI

from map_governance import MapTransitionError
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
            health = await client.get(
                "/api/plugins/map-governance/health?profile=ceo"
            )
            board = await client.get(
                "/api/plugins/map-governance/board?profile=ceo"
            )
            unscoped = await client.get("/api/plugins/map-governance/health")
        return health, board, unscoped

    health_response, board_response, unscoped_response = asyncio.run(
        exercise_routes()
    )

    assert health_response.status_code == 200
    assert health_response.json() == {"operation": "health"}
    assert board_response.status_code == 200
    assert board_response.json() == {"operation": "board"}
    assert unscoped_response.status_code == 422
    assert requested_profiles == ["ceo", "ceo"]


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
        return configured, bound, refreshed, transitioned

    configured, bound, refreshed, transitioned = asyncio.run(exercise_routes())

    assert configured.status_code == 200
    assert bound.status_code == 200
    assert refreshed.status_code == 200
    assert transitioned.status_code == 200
    assert [configured.json(), bound.json(), refreshed.json(), transitioned.json()] == [
        {"operation": "configure_project"},
        {"operation": "bind_map"},
        {"operation": "refresh"},
        {"operation": "transition_map"},
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
            raise TrackerError("GitHub write failed; verify issue-write permission")

    monkeypatch.setattr(
        adapter,
        "application_for_profile",
        lambda profile: ApplicationProbe(),
    )

    async def exercise_routes():
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
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
        return invalid, failed

    invalid, failed = asyncio.run(exercise_routes())

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
