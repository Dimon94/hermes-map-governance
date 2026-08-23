from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import httpx
from fastapi import FastAPI

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
