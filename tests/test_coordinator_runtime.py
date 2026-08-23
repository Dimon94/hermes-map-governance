from __future__ import annotations

import json
from pathlib import Path

import pytest

from map_governance.coordinator import (
    CommissioningContext,
    CoordinatorCommandResult,
    CoordinatorRuntime,
    CoordinatorRuntimeError,
    RootRuntimeRequest,
)
from map_governance.storage import PluginStorage


class ScriptedRunner:
    def __init__(self, results: list[CoordinatorCommandResult]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, ...]] = []
        self.starts: list[tuple[str, ...]] = []

    def run(self, arguments, *, timeout):
        self.calls.append(tuple(arguments))
        assert timeout > 0
        return self.results.pop(0)

    def start(self, arguments):
        self.starts.append(tuple(arguments))


def _result(payload: object, *, returncode: int = 0):
    return CoordinatorCommandResult(
        returncode=returncode,
        stdout=json.dumps(payload) if returncode == 0 else "",
        timed_out=False,
    )


def _context(tmp_path: Path) -> CommissioningContext:
    repository_path = tmp_path / "repository"
    repository_path.mkdir()
    return CommissioningContext(
        project_id="PVT_acme_7",
        project_url="https://github.com/orgs/acme/projects/7",
        repository="acme/atlas",
        repository_path=str(repository_path),
        pm_profile="pm",
        routing_policy="codex",
        herdr_executable="herdr-test",
        skills=("map-governance:pm", "delivery-pipeline", "herdr"),
    )


def _request(tmp_path: Path) -> RootRuntimeRequest:
    return RootRuntimeRequest(
        map_id="I_atlas_41",
        map_url="https://github.com/acme/atlas/issues/41",
        context=_context(tmp_path),
    )


def _session_list(*names: str):
    return _result(
        {
            "sessions": [
                {
                    "name": name,
                    "default": False,
                    "running": True,
                    "socket_path": f"/opaque/{index}",
                    "session_dir": f"/opaque/session/{index}",
                }
                for index, name in enumerate(names)
            ]
        }
    )


def _workspace_list(*workspaces: dict[str, str]):
    return _result(
        {
            "id": "cli:workspace:list",
            "result": {"type": "workspace_list", "workspaces": list(workspaces)},
        }
    )


def _workspace_created(label: str):
    return _result(
        {
            "id": "cli:workspace:create",
            "result": {
                "type": "workspace_created",
                "workspace": {"workspace_id": "wA", "label": label},
                "tab": {"tab_id": "wA:t1"},
                "root_pane": {"pane_id": "wA:p1"},
            },
        }
    )


def _pane_list(repository_path: str):
    return _result(
        {
            "id": "cli:pane:list",
            "result": {
                "type": "pane_list",
                "panes": [
                    {
                        "workspace_id": "wA",
                        "tab_id": "wA:t1",
                        "pane_id": "wA:p1",
                        "cwd": repository_path,
                    }
                ],
            },
        }
    )


def _agent_missing():
    return CoordinatorCommandResult(returncode=1, stdout="", timed_out=False)


def _agent(
    name: str, *, session_id: str = "hermes-session-opaque", session_kind: str = "id"
):
    return _result(
        {
            "id": "cli:agent",
            "result": {
                "type": "agent_info",
                "agent": {
                    "name": name,
                    "agent": "hermes",
                    "workspace_id": "wA",
                    "tab_id": "wA:t1",
                    "pane_id": "wA:p1",
                    "cwd": "/opaque/repository",
                    "agent_session": {
                        "source": "herdr:hermes",
                        "agent": "hermes",
                        "kind": session_kind,
                        "value": session_id,
                    },
                },
            },
        }
    )


def _agent_started_without_session(name: str):
    return _result(
        {
            "id": "cli:agent:start",
            "result": {
                "type": "agent_started",
                "agent": {
                    "name": name,
                    "agent": "hermes",
                    "workspace_id": "wA",
                    "tab_id": "wA:t1",
                    "pane_id": "wA:p1",
                    "cwd": "/opaque/repository",
                },
            },
        }
    )


def test_create_uses_fixed_argv_and_persists_only_returned_opaque_coordinates(
    tmp_path,
):
    storage = PluginStorage(tmp_path / "plugin-data")
    request = _request(tmp_path)
    lifecycle = storage.coordinator_lifecycle(created_at="2026-08-24T00:00:00Z")
    namespace = CoordinatorRuntime.session_namespace(lifecycle)
    label = CoordinatorRuntime.workspace_label(lifecycle, request.map_id)
    agent_name = CoordinatorRuntime.agent_name(lifecycle, request.map_id)
    runner = ScriptedRunner(
        [
            _session_list(),
            _session_list(namespace),
            _workspace_list(),
            _workspace_created(label),
            _pane_list(request.context.repository_path),
            _agent_missing(),
            _agent_started_without_session(agent_name),
            _agent(agent_name),
        ]
    )
    runtime = CoordinatorRuntime(
        storage=storage,
        runner=runner,
        clock=lambda: "2026-08-24T00:00:00Z",
    )

    record = runtime.ensure_root(request)

    assert record["state"] == "pm_ready"
    assert record["session_namespace"] == namespace
    assert record["workspace_id"] == "wA"
    assert record["window_id"] == "wA:t1"
    assert record["pane_id"] == "wA:p1"
    assert record["agent_id"] == agent_name
    assert record["agent_session_id"] == "hermes-session-opaque"
    assert runner.starts == [("herdr-test", "--session", namespace, "server")]
    assert runner.calls[2] == (
        "herdr-test",
        "--session",
        namespace,
        "workspace",
        "list",
    )
    assert runner.calls[3] == (
        "herdr-test",
        "--session",
        namespace,
        "workspace",
        "create",
        "--cwd",
        request.context.repository_path,
        "--label",
        label,
        "--env",
        f"MAP_GOVERNANCE_OWNERSHIP={record['ownership_marker']}",
        "--no-focus",
    )
    assert runner.calls[4] == (
        "herdr-test",
        "--session",
        namespace,
        "pane",
        "list",
        "--workspace",
        "wA",
    )
    assert runner.calls[5] == (
        "herdr-test",
        "--session",
        namespace,
        "agent",
        "get",
        agent_name,
    )
    assert runner.calls[6] == (
        "herdr-test",
        "--session",
        namespace,
        "agent",
        "start",
        agent_name,
        "--kind",
        "hermes",
        "--pane",
        "wA:p1",
        "--timeout",
        "30000",
        "--",
        "--profile",
        "pm",
        "--tui",
        "--skills",
        "map-governance:pm,delivery-pipeline,herdr",
    )
    assert runner.calls[7] == (
        "herdr-test",
        "--session",
        namespace,
        "agent",
        "get",
        agent_name,
    )
    persisted = storage.pm_runtime(request.map_id)
    assert persisted == record
    assert "argv" not in record
    assert "stderr" not in record
    assert "terminal" not in record


def test_restart_resumes_the_same_owned_runtime_without_creating_or_starting(
    tmp_path,
):
    storage = PluginStorage(tmp_path / "plugin-data")
    request = _request(tmp_path)
    lifecycle = storage.coordinator_lifecycle(created_at="2026-08-24T00:00:00Z")
    namespace = CoordinatorRuntime.session_namespace(lifecycle)
    label = CoordinatorRuntime.workspace_label(lifecycle, request.map_id)
    agent_name = CoordinatorRuntime.agent_name(lifecycle, request.map_id)
    storage.reserve_pm_runtime(
        map_id=request.map_id,
        session_namespace=namespace,
        workspace_label=label,
        agent_id=agent_name,
        ownership_marker=CoordinatorRuntime.ownership_marker(lifecycle, request.map_id),
        lifecycle_id=lifecycle,
        context=request.context,
        updated_at="2026-08-24T00:00:00Z",
        session_ownership_marker=CoordinatorRuntime.session_ownership_marker(lifecycle),
    )
    storage.update_pm_runtime(
        map_id=request.map_id,
        state="pm_ready",
        workspace_id="wA",
        window_id="wA:t1",
        pane_id="wA:p1",
        agent_session_id="hermes-session-opaque",
        failure=None,
        updated_at="2026-08-24T00:00:01Z",
    )
    runner = ScriptedRunner(
        [
            _session_list(namespace),
            _workspace_list({"workspace_id": "wA", "label": label}),
            _result(
                {
                    "id": "cli:workspace:get",
                    "result": {
                        "type": "workspace_info",
                        "workspace": {
                            "workspace_id": "wA",
                            "label": label,
                            "active_tab_id": "wA:t1",
                        },
                    },
                }
            ),
            _pane_list(request.context.repository_path),
            _agent(agent_name),
        ]
    )

    record = CoordinatorRuntime(
        storage=storage,
        runner=runner,
        clock=lambda: "2026-08-24T00:00:02Z",
    ).ensure_root(request)

    assert record["state"] == "pm_ready"
    assert runner.starts == []
    assert not any("create" in call or "start" in call for call in runner.calls)


def test_same_name_without_durable_ownership_proof_is_a_collision(tmp_path):
    storage = PluginStorage(tmp_path / "plugin-data")
    request = _request(tmp_path)
    lifecycle = storage.coordinator_lifecycle(created_at="2026-08-24T00:00:00Z")
    namespace = CoordinatorRuntime.session_namespace(lifecycle)
    runner = ScriptedRunner([_session_list(namespace)])

    with pytest.raises(CoordinatorRuntimeError) as raised:
        CoordinatorRuntime(
            storage=storage,
            runner=runner,
            clock=lambda: "2026-08-24T00:00:00Z",
        ).ensure_root(request)

    assert raised.value.as_dict() == {
        "type": "coordinator_runtime_error",
        "reason": "session_namespace_collision",
        "retryable": False,
        "repair_required": True,
        "resource_disposition": "no_cleanup_without_verified_ownership",
    }
    assert runner.starts == []
    assert storage.pm_runtime(request.map_id) is None


def test_same_workspace_label_without_map_ownership_proof_is_a_collision(tmp_path):
    storage = PluginStorage(tmp_path / "plugin-data")
    request = _request(tmp_path)
    lifecycle = storage.coordinator_lifecycle(created_at="2026-08-24T00:00:00Z")
    namespace = CoordinatorRuntime.session_namespace(lifecycle)
    label = CoordinatorRuntime.workspace_label(lifecycle, request.map_id)
    other_root = tmp_path / "other"
    other_root.mkdir()
    other_context = _context(other_root)
    storage.reserve_pm_runtime(
        map_id="I_other",
        session_namespace=namespace,
        workspace_label="mapgov-map-other",
        agent_id="mapgov_pm_other",
        ownership_marker=CoordinatorRuntime.ownership_marker(lifecycle, "I_other"),
        lifecycle_id=lifecycle,
        context=other_context,
        updated_at="2026-08-24T00:00:00Z",
        session_ownership_marker=CoordinatorRuntime.session_ownership_marker(lifecycle),
    )
    runner = ScriptedRunner(
        [
            _session_list(namespace),
            _workspace_list({"workspace_id": "w-unowned", "label": label}),
        ]
    )

    with pytest.raises(CoordinatorRuntimeError) as raised:
        CoordinatorRuntime(
            storage=storage,
            runner=runner,
            clock=lambda: "2026-08-24T00:00:00Z",
        ).ensure_root(request)

    assert raised.value.reason == "workspace_marker_collision"
    assert not any("create" in call or "agent" in call for call in runner.calls)


def test_restart_recovers_workspace_created_before_coordinate_commit(tmp_path):
    storage = PluginStorage(tmp_path / "plugin-data")
    request = _request(tmp_path)
    lifecycle = storage.coordinator_lifecycle(created_at="2026-08-24T00:00:00Z")
    namespace = CoordinatorRuntime.session_namespace(lifecycle)
    label = CoordinatorRuntime.workspace_label(lifecycle, request.map_id)
    agent_name = CoordinatorRuntime.agent_name(lifecycle, request.map_id)
    storage.reserve_pm_runtime(
        map_id=request.map_id,
        session_namespace=namespace,
        workspace_label=label,
        agent_id=agent_name,
        ownership_marker=CoordinatorRuntime.ownership_marker(lifecycle, request.map_id),
        lifecycle_id=lifecycle,
        context=request.context,
        updated_at="2026-08-24T00:00:00Z",
        session_ownership_marker=CoordinatorRuntime.session_ownership_marker(lifecycle),
    )
    runner = ScriptedRunner(
        [
            _session_list(namespace),
            _workspace_list({"workspace_id": "wA", "label": label}),
            _result(
                {
                    "id": "cli:workspace:get",
                    "result": {
                        "type": "workspace_info",
                        "workspace": {
                            "workspace_id": "wA",
                            "label": label,
                            "active_tab_id": "wA:t1",
                        },
                    },
                }
            ),
            _result(
                {
                    "id": "cli:pane:list",
                    "result": {
                        "type": "pane_list",
                        "panes": [
                            {
                                "workspace_id": "wA",
                                "tab_id": "wA:t1",
                                "pane_id": "wA:p1",
                                "cwd": request.context.repository_path,
                            }
                        ],
                    },
                }
            ),
            _agent_missing(),
            _agent(agent_name),
        ]
    )

    record = CoordinatorRuntime(
        storage=storage,
        runner=runner,
        clock=lambda: "2026-08-24T00:00:01Z",
    ).ensure_root(request)

    assert record["state"] == "pm_ready"
    assert record["workspace_id"] == "wA"
    assert runner.starts == []
    assert not any(call[3:5] == ("workspace", "create") for call in runner.calls)


@pytest.mark.parametrize(
    ("bad_result", "reason"),
    [
        (
            CoordinatorCommandResult(
                returncode=0,
                stdout="not-json github_pat_DO_NOT_LEAK",
                timed_out=False,
            ),
            "malformed_json",
        ),
        (
            CoordinatorCommandResult(returncode=None, stdout="", timed_out=True),
            "command_timeout",
        ),
    ],
)
def test_command_failures_are_structured_and_secret_safe(tmp_path, bad_result, reason):
    runtime = CoordinatorRuntime(
        storage=PluginStorage(tmp_path / "plugin-data"),
        runner=ScriptedRunner([bad_result]),
        clock=lambda: "2026-08-24T00:00:00Z",
    )

    with pytest.raises(CoordinatorRuntimeError) as raised:
        runtime.ensure_root(_request(tmp_path))

    assert raised.value.reason == reason
    assert "github_pat" not in json.dumps(raised.value.as_dict())


def test_returned_coordinates_must_match_the_owned_workspace(tmp_path):
    storage = PluginStorage(tmp_path / "plugin-data")
    request = _request(tmp_path)
    lifecycle = storage.coordinator_lifecycle(created_at="2026-08-24T00:00:00Z")
    namespace = CoordinatorRuntime.session_namespace(lifecycle)
    label = CoordinatorRuntime.workspace_label(lifecycle, request.map_id)
    runner = ScriptedRunner(
        [
            _session_list(),
            _session_list(namespace),
            _workspace_list(),
            _workspace_created(label),
            _pane_list(request.context.repository_path),
            _agent_missing(),
            _agent(CoordinatorRuntime.agent_name(lifecycle, request.map_id)),
        ]
    )
    runner.results[-1] = _result(
        {
            "id": "cli:agent:start",
            "result": {
                "type": "agent_started",
                "agent": {
                    "name": CoordinatorRuntime.agent_name(lifecycle, request.map_id),
                    "agent": "hermes",
                    "workspace_id": "unrelated-workspace",
                    "tab_id": "wA:t1",
                    "pane_id": "wA:p1",
                    "agent_session": {
                        "source": "herdr:hermes",
                        "agent": "hermes",
                        "kind": "id",
                        "value": "opaque",
                    },
                },
            },
        }
    )

    with pytest.raises(CoordinatorRuntimeError) as raised:
        CoordinatorRuntime(
            storage=storage,
            runner=runner,
            clock=lambda: "2026-08-24T00:00:00Z",
        ).ensure_root(request)

    assert raised.value.reason == "opaque_coordinate_mismatch"
    status = storage.pm_runtime(request.map_id)
    assert status["state"] == "repair_required"
    assert status["failure"] == {
        "reason": "opaque_coordinate_mismatch",
        "repair_required": True,
        "retryable": False,
        "resource_disposition": "no_cleanup_without_verified_ownership",
    }


def test_path_kind_agent_session_is_rejected_and_never_projected(tmp_path):
    storage = PluginStorage(tmp_path / "plugin-data")
    request = _request(tmp_path)
    lifecycle = storage.coordinator_lifecycle(created_at="2026-08-24T00:00:00Z")
    namespace = CoordinatorRuntime.session_namespace(lifecycle)
    label = CoordinatorRuntime.workspace_label(lifecycle, request.map_id)
    agent_name = CoordinatorRuntime.agent_name(lifecycle, request.map_id)
    path_agent = _agent(
        agent_name,
        session_id="/Users/person/.hermes/sessions/private",
        session_kind="path",
    )
    runner = ScriptedRunner(
        [
            _session_list(),
            _session_list(namespace),
            _workspace_list(),
            _workspace_created(label),
            _pane_list(request.context.repository_path),
            _agent_missing(),
            path_agent,
            path_agent,
        ]
    )

    with pytest.raises(CoordinatorRuntimeError) as raised:
        CoordinatorRuntime(
            storage=storage,
            runner=runner,
            clock=lambda: "2026-08-24T00:00:00Z",
        ).ensure_root(request)

    assert raised.value.reason == "agent_session_identity_unsafe"
    assert "/Users/person" not in str(raised.value.as_dict())
    assert storage.pm_runtime(request.map_id)["agent_session_id"] is None


def test_prompt_passes_one_structured_payload_without_shell_or_execution_lane_detail(
    tmp_path,
):
    storage = PluginStorage(tmp_path / "plugin-data")
    request = _request(tmp_path)
    lifecycle = storage.coordinator_lifecycle(created_at="2026-08-24T00:00:00Z")
    namespace = CoordinatorRuntime.session_namespace(lifecycle)
    label = CoordinatorRuntime.workspace_label(lifecycle, request.map_id)
    agent_name = CoordinatorRuntime.agent_name(lifecycle, request.map_id)
    storage.reserve_pm_runtime(
        map_id=request.map_id,
        session_namespace=namespace,
        workspace_label=label,
        agent_id=agent_name,
        ownership_marker=CoordinatorRuntime.ownership_marker(lifecycle, request.map_id),
        lifecycle_id=lifecycle,
        context=request.context,
        updated_at="2026-08-24T00:00:00Z",
        session_ownership_marker=CoordinatorRuntime.session_ownership_marker(lifecycle),
    )
    storage.update_pm_runtime(
        map_id=request.map_id,
        state="pm_ready",
        workspace_id="wA",
        window_id="wA:t1",
        pane_id="wA:p1",
        agent_session_id="hermes-session-opaque",
        failure=None,
        updated_at="2026-08-24T00:00:00Z",
    )
    runner = ScriptedRunner(
        [
            _session_list(namespace),
            _workspace_list({"workspace_id": "wA", "label": label}),
            _result(
                {
                    "id": "cli:workspace:get",
                    "result": {
                        "type": "workspace_info",
                        "workspace": {
                            "workspace_id": "wA",
                            "label": label,
                            "active_tab_id": "wA:t1",
                        },
                    },
                }
            ),
            _pane_list(request.context.repository_path),
            _agent(agent_name),
            _agent(agent_name),
        ]
    )
    runtime = CoordinatorRuntime(
        storage=storage,
        runner=runner,
        clock=lambda: "2026-08-24T00:00:01Z",
    )
    checkpoint = {
        "record_id": "commission-ready-I_atlas_41",
        "type": "checkpoint",
        "summary": "Hermes PM runtime is ready for governed delivery.",
        "timestamp": "2026-08-24T00:00:01Z",
    }

    runtime.prompt_ready(
        map_id=request.map_id,
        payload={
            "protocol": "map-governance/pm-commission-v1",
            "map": {"id": request.map_id, "url": request.map_url},
            "project": {"id": request.context.project_id},
            "repository": {
                "coordinate": request.context.repository,
                "path": request.context.repository_path,
            },
            "profile": request.context.pm_profile,
            "skills": list(request.context.skills),
            "routing_policy": request.context.routing_policy,
            "ready_checkpoint": checkpoint,
        },
    )

    prompt_call = runner.calls[-1]
    assert prompt_call[:7] == (
        "herdr-test",
        "--session",
        namespace,
        "agent",
        "prompt",
        agent_name,
        prompt_call[6],
    )
    assert prompt_call[7:] == ("--wait", "--timeout", "30000")
    payload = json.loads(prompt_call[6])
    assert payload["ready_checkpoint"] == checkpoint
    assert "ticket" not in json.dumps(payload).lower()
    assert "worktree" not in json.dumps(payload).lower()
    assert "credential" not in json.dumps(payload).lower()
    assert "secret" not in json.dumps(payload).lower()
