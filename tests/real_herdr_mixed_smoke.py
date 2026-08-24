"""Opt-in real Herdr smoke for two isolated mixed-worker delivery lanes.

This file is intentionally not named ``test_*.py``. It creates a temporary Git
repository, plugin storage, one uniquely named plugin-owned Herdr session, and
two disposable execution worktrees. Normal pytest runs never import or execute
it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from map_governance.coordinator import (
    CommissioningContext,
    CoordinatorRuntime,
    CoordinatorRuntimeError,
    DeliveryLaneRegistry,
    DeliveryLaneSpec,
    DeliveryRuntimeRequest,
    RootRuntimeRequest,
    delivery_confirmed_dispatch_id,
)
from map_governance.storage import PluginStorage


MAP_ID = "I_map_governance_real_smoke_900001"
MAP_URL = "https://github.com/map-governance-smoke/disposable/issues/900001"
SPEC_URL = "https://github.com/map-governance-smoke/disposable/issues/900004"
REPOSITORY = "map-governance-smoke/disposable"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _run(arguments: Sequence[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        list(arguments),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"smoke setup command failed: {Path(arguments[0]).name}")
    return completed.stdout.strip()


def _sessions(herdr: str) -> set[str]:
    payload = json.loads(_run([herdr, "session", "list", "--json"]))
    sessions = payload.get("sessions")
    if not isinstance(sessions, list):
        raise RuntimeError("Herdr session list was malformed")
    return {
        str(item["name"])
        for item in sessions
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def _make_repository(root: Path) -> tuple[Path, Path, Path, str]:
    repository = root / "integration"
    codex_worktree = root / "codex-lane"
    claude_worktree = root / "claude-lane"
    repository.mkdir()
    _run(["git", "init", "-b", "feature/map-900001"], cwd=repository)
    _run(["git", "config", "user.name", "Map Governance Smoke"], cwd=repository)
    _run(
        ["git", "config", "user.email", "map-governance-smoke@invalid"],
        cwd=repository,
    )
    (repository / "README.md").write_text(
        "# Disposable Map Governance real Herdr smoke\n", encoding="utf-8"
    )
    _run(["git", "add", "README.md"], cwd=repository)
    _run(
        ["git", "commit", "-m", "test: seed isolated smoke repository"], cwd=repository
    )
    base_commit = _run(["git", "rev-parse", "HEAD"], cwd=repository)
    _run(
        [
            "git",
            "worktree",
            "add",
            "-b",
            "codex/issue-900002",
            str(codex_worktree),
            base_commit,
        ],
        cwd=repository,
    )
    _run(
        [
            "git",
            "worktree",
            "add",
            "-b",
            "claude/issue-900003",
            str(claude_worktree),
            base_commit,
        ],
        cwd=repository,
    )
    return repository, codex_worktree, claude_worktree, base_commit


def _lane(
    *,
    worker_kind: str,
    ticket_number: int,
    title: str,
    execution_worktree: Path,
    integration_worktree: Path,
    base_commit: str,
    owner_skill_path: Path,
    order: int,
) -> DeliveryLaneSpec:
    predecessors = (
        ()
        if order == 1
        else ("https://github.com/map-governance-smoke/disposable/issues/900002",)
    )
    return DeliveryLaneSpec(
        protocol="delivery-pipeline/herdr-implementation-v1",
        lane_id=f"implementation-{ticket_number}",
        ticket_id=f"I_real_smoke_{ticket_number}",
        ticket_title=title,
        ticket_url=(
            f"https://github.com/map-governance-smoke/disposable/issues/{ticket_number}"
        ),
        parent_spec_url=SPEC_URL,
        integration_worktree=str(integration_worktree),
        integration_branch="feature/map-900001",
        execution_worktree=str(execution_worktree),
        execution_branch=f"{worker_kind}/issue-{ticket_number}",
        base_commit=base_commit,
        owner_skill_name="implement",
        owner_skill_path=str(owner_skill_path),
        owner_invocation_label="$implement",
        worker_kind=worker_kind,
        validation_argv=("git", "status", "--porcelain=v1"),
        completion_contract="one-local-commit-integrated-and-validated",
        known_limitations=(
            "Synthetic local-only smoke ticket; all remote actions are forbidden.",
        ),
        integration_order=order,
        integration_total=2,
        integration_predecessor_ticket_urls=predecessors,
    )


def _cleanup_owned_session(
    *, herdr: str, session_name: str, preexisting: bool
) -> dict[str, Any]:
    if preexisting:
        return {"attempted": False, "reason": "preexisting_namespace_not_owned"}
    try:
        present = session_name in _sessions(herdr)
    except (RuntimeError, json.JSONDecodeError):
        return {"attempted": False, "reason": "ownership_readback_unavailable"}
    if not present:
        return {"attempted": False, "reason": "session_already_absent"}
    stop = subprocess.run(
        [herdr, "session", "stop", session_name, "--json"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    delete = subprocess.run(
        [herdr, "session", "delete", session_name, "--json"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    return {
        "attempted": True,
        "stopped": stop.returncode == 0,
        "deleted": delete.returncode == 0,
    }


def _collect(
    *,
    runtime: CoordinatorRuntime,
    request: DeliveryRuntimeRequest,
    timeout_seconds: float,
) -> tuple[dict[str, Any], int, float]:
    started = time.monotonic()
    retries = 0
    while True:
        try:
            result = runtime.collect_lane(request)
        except CoordinatorRuntimeError as error:
            if not error.retryable or time.monotonic() - started >= timeout_seconds:
                raise
            retries += 1
            time.sleep(2)
            continue
        if result.get("state") != "locally_validated":
            raise RuntimeError(f"worker did not complete: {result.get('state')}")
        return result, retries, time.monotonic() - started


def _completion_metric(
    *,
    request: DeliveryRuntimeRequest,
    result: dict[str, Any],
    dispatched_at: float,
    collect_latency: float,
    retries: int,
) -> dict[str, Any]:
    registry = DeliveryLaneRegistry.from_payload(result["integrated_registry"])
    return {
        "worker_kind": request.lane.worker_kind,
        "lane_identity": request.lane.lane_id,
        "pane_identity": registry.pane_id,
        "completion_evidence": {
            "state": result["state"],
            "execution_commit": registry.head_commit,
            "integration_commit": registry.integrated_commit,
            "evidence_source": registry.evidence_source,
        },
        "latency_seconds": round(time.monotonic() - dispatched_at, 3),
        "collect_latency_seconds": round(collect_latency, 3),
        "retry_count": retries,
    }


def _agent_list(herdr: str, session_name: str) -> dict[str, Any]:
    payload = json.loads(_run([herdr, "--session", session_name, "agent", "list"]))
    result = payload.get("result")
    if not isinstance(result, dict) or result.get("type") != "agent_list":
        raise RuntimeError("Herdr agent list was malformed")
    return result


def _duplicate_lane_count(
    *,
    agent_list: dict[str, Any],
    requests: Sequence[DeliveryRuntimeRequest],
    lifecycle: str,
) -> int:
    agents = agent_list.get("agents")
    if not isinstance(agents, list) or any(
        not isinstance(agent, dict) for agent in agents
    ):
        raise RuntimeError("Herdr agent list was malformed")
    duplicates = 0
    for request in requests:
        registry = request.registry
        if registry is None:
            raise RuntimeError("smoke lane registry is missing")
        expected_name = CoordinatorRuntime.delivery_agent_name(
            lifecycle,
            delivery_confirmed_dispatch_id(request),
            request.lane.worker_kind,
        )
        lane_agents = [
            agent
            for agent in agents
            if agent.get("name") == expected_name
            or agent.get("cwd") == request.lane.execution_worktree
            or agent.get("pane_id") == registry.pane_id
        ]
        exact = [
            agent
            for agent in lane_agents
            if agent.get("name") == expected_name
            and agent.get("agent") == request.lane.worker_kind
            and agent.get("cwd") == request.lane.execution_worktree
            and agent.get("pane_id") == registry.pane_id
        ]
        if len(exact) != 1:
            raise RuntimeError("real Herdr smoke lane ownership is ambiguous")
        duplicates += max(0, len(lane_agents) - 1)
    return duplicates


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    if not args.confirm_isolated_real_herdr:
        raise RuntimeError("explicit --confirm-isolated-real-herdr is required")
    herdr = shutil.which(args.herdr)
    if herdr is None:
        raise RuntimeError("Herdr executable is unavailable")
    owner_skill = Path(args.implement_skill).resolve()
    if not owner_skill.is_file() or owner_skill.is_symlink():
        raise RuntimeError("implement owner Skill is unavailable")

    with tempfile.TemporaryDirectory(prefix="map-governance-real-herdr-smoke-") as raw:
        root = Path(raw).resolve()
        repository, codex_worktree, claude_worktree, base_commit = _make_repository(
            root
        )
        storage = PluginStorage(root / "plugin-storage")
        storage.check_readiness()
        lifecycle = storage.coordinator_lifecycle(created_at=_now())
        session_name = CoordinatorRuntime.session_namespace(lifecycle)
        initial_sessions = _sessions(herdr)
        preexisting = session_name in initial_sessions
        if preexisting:
            raise RuntimeError("unique smoke namespace unexpectedly already exists")

        context = CommissioningContext(
            project_id="PVT_real_smoke_900001",
            project_url="https://github.com/orgs/map-governance-smoke/projects/900001",
            repository=REPOSITORY,
            repository_path=str(repository),
            pm_profile=args.pm_profile,
            routing_policy="mixed",
            herdr_executable=herdr,
            skills=("map-governance:pm", "delivery-pipeline", "herdr"),
            supported_worker_kinds=("codex", "claude"),
            implement_skill_path=str(owner_skill),
            routing_default_worker="codex",
            routing_attribute_workers=(
                ("backend", "codex"),
                ("frontend", "claude"),
            ),
        )
        runtime = CoordinatorRuntime(
            storage=storage,
            clock=_now,
            command_timeout=45,
            startup_attempts=60,
            startup_poll_seconds=0.25,
        )
        smoke_result: dict[str, Any] | None = None
        cleanup: dict[str, Any] = {"attempted": False}
        try:
            runtime.ensure_root(
                RootRuntimeRequest(map_id=MAP_ID, map_url=MAP_URL, context=context)
            )
            storage.update_pm_runtime(
                map_id=MAP_ID,
                state="active",
                ready_record_id="real-smoke-ready",
                failure=None,
                updated_at=_now(),
            )
            lanes = (
                _lane(
                    worker_kind="codex",
                    ticket_number=900002,
                    title=(
                        "[LOCAL SMOKE] Create mixed-smoke-codex.txt containing exactly "
                        "codex, validate, review, and make one local commit"
                    ),
                    execution_worktree=codex_worktree,
                    integration_worktree=repository,
                    base_commit=base_commit,
                    owner_skill_path=owner_skill,
                    order=1,
                ),
                _lane(
                    worker_kind="claude",
                    ticket_number=900003,
                    title=(
                        "[LOCAL SMOKE] Create mixed-smoke-claude.txt containing exactly "
                        "claude, validate, review, and make one local commit"
                    ),
                    execution_worktree=claude_worktree,
                    integration_worktree=repository,
                    base_commit=base_commit,
                    owner_skill_path=owner_skill,
                    order=2,
                ),
            )
            requests: list[DeliveryRuntimeRequest] = []
            dispatch_started: dict[str, float] = {}
            for lane in lanes:
                request = DeliveryRuntimeRequest(
                    map_id=MAP_ID,
                    map_url=MAP_URL,
                    context=context,
                    lane=lane,
                    registry_timestamp=_now(),
                )
                prepared = runtime.prepare_lane(request)
                created = DeliveryLaneRegistry.from_payload(prepared["registry"])
                dispatch_started[lane.lane_id] = time.monotonic()
                dispatched = runtime.dispatch_lane(replace(request, registry=created))
                running = DeliveryLaneRegistry.from_payload(dispatched["registry"])
                requests.append(replace(request, registry=running))

            outcomes: list[dict[str, Any]] = []
            expected_head: str | None = None
            predecessor_commits: list[str] = []
            for request in requests:
                collect_request = replace(
                    request,
                    registry_timestamp=_now(),
                    integration_expected_head=expected_head,
                    integration_predecessor_commits=tuple(predecessor_commits),
                )
                result, retries, collect_latency = _collect(
                    runtime=runtime,
                    request=collect_request,
                    timeout_seconds=args.timeout_seconds,
                )
                registry = DeliveryLaneRegistry.from_payload(
                    result["integrated_registry"]
                )
                expected_head = registry.integrated_commit
                if expected_head is None:
                    raise RuntimeError("integrated smoke registry has no commit")
                predecessor_commits.append(expected_head)
                outcomes.append(
                    _completion_metric(
                        request=request,
                        result=result,
                        dispatched_at=dispatch_started[request.lane.lane_id],
                        collect_latency=collect_latency,
                        retries=retries,
                    )
                )
            duplicate_lane_count = _duplicate_lane_count(
                agent_list=_agent_list(herdr, session_name),
                requests=requests,
                lifecycle=lifecycle,
            )
            smoke_result = {
                "result": "passed",
                "session_name": session_name,
                "isolated_root": str(root),
                "integration_order": [item["lane_identity"] for item in outcomes],
                "lanes": outcomes,
                "duplicate_lane_count": duplicate_lane_count,
            }
        finally:
            record = storage.pm_runtime(MAP_ID)
            owns_expected_namespace = bool(
                record
                and record.get("session_namespace") == session_name
                and record.get("lifecycle_id") == lifecycle
                and record.get("ownership_marker")
                == CoordinatorRuntime.ownership_marker(lifecycle, MAP_ID)
            )
            cleanup = _cleanup_owned_session(
                herdr=herdr,
                session_name=session_name,
                preexisting=preexisting or not owns_expected_namespace,
            )
        if smoke_result is None:  # pragma: no cover - an exception is propagating
            raise RuntimeError("real Herdr smoke produced no result")
        smoke_result["cleanup"] = cleanup
        return smoke_result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-isolated-real-herdr", action="store_true")
    parser.add_argument("--herdr", default="herdr")
    parser.add_argument("--pm-profile", required=True)
    parser.add_argument("--implement-skill", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = run_smoke(args)
    except Exception as error:  # noqa: BLE001 - CLI must emit bounded smoke evidence
        print(
            json.dumps(
                {
                    "result": "failed",
                    "error_type": type(error).__name__,
                    "error": str(error)[:500],
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
