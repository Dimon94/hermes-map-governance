from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

from map_governance.coordinator import (
    CommissioningContext,
    CoordinatorRuntime,
    DeliveryLaneRegistry,
    DeliveryLaneSpec,
    DeliveryRuntimeRequest,
    delivery_dispatch_id,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _smoke_module():
    path = REPOSITORY_ROOT / "tests" / "real_herdr_mixed_smoke.py"
    spec = importlib.util.spec_from_file_location("real_herdr_mixed_smoke", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("real Herdr smoke module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _request() -> DeliveryRuntimeRequest:
    lane = DeliveryLaneSpec(
        protocol="delivery-pipeline/herdr-implementation-v1",
        lane_id="implementation-900002",
        ticket_id="I_smoke_900002",
        ticket_title="Create the Codex smoke proof",
        ticket_url="https://github.com/map-governance-smoke/disposable/issues/900002",
        parent_spec_url=(
            "https://github.com/map-governance-smoke/disposable/issues/900004"
        ),
        integration_worktree="/tmp/mapgov-smoke-integration",
        integration_branch="feature/map-900001",
        execution_worktree="/tmp/mapgov-smoke-codex",
        execution_branch="codex/issue-900002",
        base_commit="a" * 40,
        owner_skill_name="implement",
        owner_skill_path="/tmp/mapgov-smoke-implement/SKILL.md",
        owner_invocation_label="$implement",
        worker_kind="codex",
        validation_argv=("git", "status", "--porcelain=v1"),
        completion_contract="one-local-commit-integrated-and-validated",
    )
    context = CommissioningContext(
        project_id="PVT_smoke",
        project_url="https://github.com/orgs/map-governance-smoke/projects/1",
        repository="map-governance-smoke/disposable",
        repository_path="/tmp/mapgov-smoke-source",
        pm_profile="pm-smoke",
        routing_policy="mixed",
        herdr_executable="herdr",
        skills=("map-governance:pm", "delivery-pipeline", "herdr"),
        supported_worker_kinds=("codex", "claude"),
    )
    bare = DeliveryRuntimeRequest(
        map_id="I_smoke_map",
        map_url="https://github.com/map-governance-smoke/disposable/issues/900001",
        context=context,
        lane=lane,
    )
    registry = DeliveryLaneRegistry(
        work_item=lane.ticket_url,
        role="implementation",
        lane_id=lane.lane_id,
        runtime="herdr-codex-pane",
        state="running",
        workspace_id="w-smoke",
        tab_id="t-smoke",
        pane_id="p-smoke",
        herdr_session_name="mapgov-smoke",
        herdr_session_owned=True,
        bootstrap_authority="none",
        agent_permission_mode="default",
        worktree=lane.execution_worktree,
        branch=lane.execution_branch,
        base_commit=lane.base_commit,
        head_commit=None,
        integrated_commit=None,
        updated_at="2026-08-24T00:00:00Z",
        dispatch_id=delivery_dispatch_id(bare),
    )
    return replace(bare, registry=registry)


def test_real_smoke_metrics_consume_integrated_registry_and_count_live_duplicates():
    smoke = _smoke_module()
    request = _request()
    integrated = replace(
        request.registry,
        state="integrated",
        head_commit="b" * 40,
        integrated_commit="c" * 40,
        evidence_source="herdr-final-report",
        final_report_digest="sha256:" + "d" * 64,
    )
    result = {
        "state": "locally_validated",
        "integrated_registry": integrated.payload(),
    }

    metric = smoke._completion_metric(
        request=request,
        result=result,
        dispatched_at=0.0,
        collect_latency=1.25,
        retries=2,
    )

    assert metric["completion_evidence"] == {
        "state": "locally_validated",
        "execution_commit": "b" * 40,
        "integration_commit": "c" * 40,
        "evidence_source": "herdr-final-report",
    }
    assert metric["retry_count"] == 2
    lifecycle = "smoke-lifecycle"
    expected_name = CoordinatorRuntime.delivery_agent_name(
        lifecycle,
        request.registry.dispatch_id,
        "codex",
    )
    agent = {
        "name": expected_name,
        "agent": "codex",
        "cwd": request.lane.execution_worktree,
        "pane_id": request.registry.pane_id,
    }
    duplicate = {
        "name": "unexpected-duplicate",
        "agent": "codex",
        "cwd": request.lane.execution_worktree,
        "pane_id": "p-duplicate",
    }

    assert (
        smoke._duplicate_lane_count(
            agent_list={"type": "agent_list", "agents": [agent, duplicate]},
            requests=[request],
            lifecycle=lifecycle,
        )
        == 1
    )
