from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from multiprocessing import get_context
from pathlib import Path
from typing import Any

import pytest
import yaml

from map_governance.approvals import ApprovalPacket
from map_governance.coordinator import CommissioningContext, CoordinatorRuntime
from map_governance.outbox import OutboxRepository
from map_governance.sessions import CEO_SESSION_MODEL_CONFIG, CEO_SYSTEM_PROMPT
from map_governance.storage import PluginStorage


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugin"
MAP_ID = "I_atlas_41"
PROJECT_ID = "PVT_acme_7"
PROJECT_URL = "https://github.com/orgs/acme/projects/7"
ISSUE_URL = "https://github.com/acme/atlas/issues/41"


def _write_skill(root: Path, name: str) -> Path:
    skill = root / name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        f"---\nname: {name}\ndescription: isolated {name}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return skill


def _write_profile(
    hermes_home: Path,
    *,
    name: str,
    toolset: str,
    skill_roots: list[Path],
) -> Path:
    profile_home = hermes_home / "profiles" / name
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "enabled": ["map-governance"],
                    "entries": {
                        "map-governance": {
                            "settings": {
                                "outbox": {
                                    "lease_seconds": 1,
                                    "poll_seconds": 60,
                                    "base_retry_seconds": 1,
                                    "max_retry_seconds": 2,
                                    "max_attempts": 3,
                                }
                            }
                        }
                    },
                },
                "toolsets": [toolset],
                "skills": {"external_dirs": [str(path) for path in skill_roots]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return profile_home


def _write_fake_executable(path: Path, body: str) -> Path:
    path.write_text(f"#!/usr/bin/env python3\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def _write_external_boundaries(root: Path) -> tuple[Path, Path, Path]:
    bin_dir = root / "fake-bin"
    bin_dir.mkdir()
    github_state = root / "fake-github-state.json"
    github_state.write_text(
        json.dumps(
            {
                "stage": "authorized",
                "comments": [],
                "mutation_calls": 0,
                "semantic_transitions": 0,
                "block_first_transition": True,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    _write_fake_executable(
        bin_dir / "gh",
        r"""
import json
import os
import sys
import time
from pathlib import Path

state_path = Path(os.environ["MAP_GOVERNANCE_RESTART_GH_STATE"])
log_path = Path(os.environ["MAP_GOVERNANCE_RESTART_GH_LOG"])
blocked_path = Path(os.environ["MAP_GOVERNANCE_RESTART_GH_BLOCKED"])
args = sys.argv[1:]
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")
state = json.loads(state_path.read_text(encoding="utf-8"))

def save():
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

def variable(name, default=None):
    prefixes = (f"{name}=",)
    for value in args:
        if value.startswith(prefixes):
            return value.split("=", 1)[1]
    return default

def issue():
    return {
        "__typename": "Issue",
        "id": "I_atlas_41",
        "number": 41,
        "title": "Map the Atlas launch",
        "body": "",
        "url": "https://github.com/acme/atlas/issues/41",
        "state": "OPEN",
        "stateReason": None,
        "repository": {
            "nameWithOwner": "acme/atlas",
            "label": {
                "id": "LA_requested",
                "name": variable("label", "map-stage/parked"),
            },
        },
        "labels": {
            "nodes": [
                {"id": "LA_map", "name": "map"},
                {"id": "LA_stage", "name": f"map-stage/{state['stage']}"},
            ],
            "pageInfo": {"hasNextPage": False},
        },
    }

if args[:2] == ["auth", "status"]:
    payload = {"hosts": {"github.com": [{
        "state": "success", "active": True, "login": "governance",
        "scopes": "read:project, repo",
    }]}}
elif args[:2] == ["repo", "view"]:
    payload = {
        "nameWithOwner": "acme/atlas",
        "url": "https://github.com/acme/atlas",
        "viewerPermission": "WRITE",
        "isArchived": False,
    }
else:
    query = variable("query", "")
    if "updateIssue" in query:
        state["mutation_calls"] += 1
        should_block = bool(state["block_first_transition"])
        state["block_first_transition"] = False
        save()
        if should_block:
            parent_pid = os.getppid()
            blocked_path.write_text(str(parent_pid), encoding="utf-8")
            while os.getppid() == parent_pid:
                time.sleep(0.02)
            raise SystemExit(75)
        state["stage"] = "parked"
        state["semantic_transitions"] += 1
        save()
        payload = {"data": {"updateIssue": {"issue": {"id": "I_atlas_41"}}}}
    elif "addComment" in query:
        body = variable("body", "")
        record = {
            "id": f"IC_{len(state['comments']) + 1}",
            "url": f"https://github.com/acme/atlas/issues/41#issuecomment-{len(state['comments']) + 1}",
            "body": body,
        }
        state["comments"].append(record)
        save()
        payload = {"data": {"addComment": {"commentEdge": {"node": record}}}}
    elif "History" in query:
        payload = {"data": {"resource": {
            "__typename": "Issue", "id": "I_atlas_41",
            "url": "https://github.com/acme/atlas/issues/41",
            "comments": {
                "nodes": state["comments"],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }}}
    elif "projectV2" in query:
        project = {
            "id": "PVT_acme_7", "number": 7,
            "title": "Acme CEO portfolio",
            "url": "https://github.com/orgs/acme/projects/7",
            "viewerCanUpdate": True,
            "owner": {"__typename": "Organization", "login": "acme"},
        }
        payload = {"data": {"organization": {"projectV2": project}}}
    else:
        payload = {"data": {"resource": issue()}}
print(json.dumps(payload))
""".lstrip(),
    )
    _write_fake_executable(
        bin_dir / "git",
        r"""
import os
import sys

args = sys.argv[1:]
if args[-2:] == ["rev-parse", "--show-toplevel"]:
    print(os.environ["MAP_GOVERNANCE_RESTART_REPOSITORY"])
else:
    print("git@github.com:acme/atlas.git")
""".lstrip(),
    )
    herdr_state = root / "fake-herdr-state.json"
    _write_fake_executable(
        bin_dir / "herdr",
        r"""
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["MAP_GOVERNANCE_RESTART_HERDR_STATE"])
log_path = Path(os.environ["MAP_GOVERNANCE_RESTART_HERDR_LOG"])
args = sys.argv[1:]
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")
if args == ["--version"]:
    print("herdr 0.8.2")
    raise SystemExit(0)
if args == ["integration", "list"]:
    print("hermes: current (v5) (/isolated/hermes)")
    print("claude: current (v8) (/isolated/claude)")
    raise SystemExit(0)
state = json.loads(state_path.read_text(encoding="utf-8"))
runtime = state["runtime"]
if args == ["session", "list", "--json"]:
    print(json.dumps({"sessions": [{
        "name": runtime["namespace"], "default": False, "running": True,
        "socket_path": "/isolated/herdr.sock",
        "session_dir": "/isolated/herdr-state",
    }]}))
    raise SystemExit(0)
if len(args) < 3 or args[:2] != ["--session", runtime["namespace"]]:
    raise SystemExit(2)
command = args[2:]
if command == ["workspace", "list"]:
    result = {"type": "workspace_list", "workspaces": [{
        "workspace_id": runtime["workspace_id"],
        "label": runtime["workspace_label"],
    }]}
elif command == ["workspace", "get", runtime["workspace_id"]]:
    result = {"type": "workspace_info", "workspace": {
        "workspace_id": runtime["workspace_id"],
        "label": runtime["workspace_label"],
        "active_tab_id": runtime["window_id"],
    }}
elif command == ["pane", "list", "--workspace", runtime["workspace_id"]]:
    result = {"type": "pane_list", "panes": [{
        "workspace_id": runtime["workspace_id"],
        "tab_id": runtime["window_id"],
        "pane_id": runtime["pane_id"],
        "cwd": runtime["repository_path"],
    }]}
elif command == ["agent", "get", runtime["agent_id"]]:
    result = {"type": "agent_info", "agent": {
        "name": runtime["agent_id"], "agent": "hermes",
        "workspace_id": runtime["workspace_id"],
        "tab_id": runtime["window_id"], "pane_id": runtime["pane_id"],
        "agent_status": "idle",
        "agent_session": {
            "source": "herdr:hermes", "agent": "hermes", "kind": "id",
            "value": runtime["agent_session_id"],
        },
    }}
else:
    raise SystemExit(2)
print(json.dumps({"id": "isolated", "result": result}))
""".lstrip(),
    )
    return bin_dir, github_state, herdr_state


def _session_evidence(state_database: Path, root_session_id: str) -> dict[str, Any]:
    from hermes_state import SessionDB

    with SessionDB(db_path=state_database) as database:
        root = database.get_session(root_session_id)
        live_session_id = database.resolve_resume_session_id(root_session_id)
        live = database.get_session(live_session_id)
        messages = database.get_messages_as_conversation(
            live_session_id,
            include_ancestors=True,
        )
    assert root is not None
    assert live is not None
    return {
        "root_session_id": root_session_id,
        "live_session_id": live_session_id,
        "root_system_prompt": root["system_prompt"],
        "root_model_config": root["model_config"],
        "live_system_prompt": live["system_prompt"],
        "live_model_config": live["model_config"],
        "messages": [
            {
                "role": message["role"],
                "content": message["content"],
                "platform_message_id": message.get("platform_message_id"),
            }
            for message in messages
        ],
    }


def _commissioning_context_from_environment() -> CommissioningContext:
    hermes_home = Path(os.environ["HERMES_HOME"])
    storage_root = Path(os.environ["MAP_GOVERNANCE_RESTART_STORAGE_ROOT"])
    repository = Path(os.environ["MAP_GOVERNANCE_RESTART_REPOSITORY"])
    implement_skill = Path(os.environ["MAP_GOVERNANCE_RESTART_IMPLEMENT_SKILL"])
    return CommissioningContext(
        project_id=PROJECT_ID,
        project_url=PROJECT_URL,
        repository="acme/atlas",
        repository_path=str(repository),
        pm_profile="pm",
        routing_policy="claude",
        herdr_executable=str(
            Path(os.environ["MAP_GOVERNANCE_RESTART_FAKE_BIN"]) / "herdr"
        ),
        skills=("map-governance:pm", "delivery-pipeline", "herdr"),
        ceo_profile="ceo",
        pm_storage_root=str(
            hermes_home / "profiles" / "pm" / "plugin-data" / storage_root.name
        ),
        supported_worker_kinds=(),
        implement_skill_path=str(implement_skill),
    )


def _seed_backend(seed_report_path: str) -> None:
    from map_governance import GovernanceRequestIdentity
    from map_governance.runtime import application_for_profile

    application = application_for_profile("ceo")
    application.stop_outbox_runtime()
    project = application.configure_project(project_url=PROJECT_URL)
    application.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    canonical = application.open_map(map_id=MAP_ID)["ceo_session"]

    packet = ApprovalPacket(
        request_id="approval-restart-pending",
        decision_class="scope_expansion",
        proposed_action="expand Map scope after restart",
        alternatives=("Keep current scope",),
        rationale="Preserve a pending chairman decision across backend restart.",
        cost_risk="No external mutation until the chairman decides.",
        evidence=(ISSUE_URL,),
        requested_scope={"map_id": MAP_ID},
        decision_payload={"scope": "restart-matrix"},
    )
    application.request_approval(
        map_id=MAP_ID,
        packet=packet,
        request_identity=GovernanceRequestIdentity(
            profile_name="ceo",
            session_id=canonical["live_session_id"],
        ),
    )

    storage = application._storage
    context = _commissioning_context_from_environment()
    lifecycle = storage.coordinator_lifecycle(created_at="2026-08-24T01:00:00Z")
    storage.reserve_pm_runtime(
        map_id=MAP_ID,
        session_namespace=CoordinatorRuntime.session_namespace(lifecycle),
        workspace_label=CoordinatorRuntime.workspace_label(lifecycle, MAP_ID),
        agent_id=CoordinatorRuntime.agent_name(lifecycle, MAP_ID),
        ownership_marker=CoordinatorRuntime.ownership_marker(lifecycle, MAP_ID),
        lifecycle_id=lifecycle,
        context=context,
        updated_at="2026-08-24T01:00:00Z",
        session_ownership_marker=CoordinatorRuntime.session_ownership_marker(lifecycle),
    )
    storage.update_pm_runtime(
        map_id=MAP_ID,
        state="active",
        workspace_id="workspace-owned",
        window_id="window-owned",
        pane_id="pane-owned",
        agent_session_id="pm-session-owned",
        ready_record_id="commission-ready-I_atlas_41",
        updated_at="2026-08-24T01:01:00Z",
    )
    state_database = Path(os.environ["HERMES_HOME"]) / "profiles" / "ceo" / "state.db"
    seed_report = {
        "canonical": canonical,
        "session": _session_evidence(
            state_database,
            canonical["root_session_id"],
        ),
        "runtime": storage.pm_runtime(MAP_ID),
    }
    Path(seed_report_path).write_text(
        json.dumps(seed_report, sort_keys=True),
        encoding="utf-8",
    )

    application.transition_map(
        map_id=MAP_ID,
        expected_stage="authorized",
        requested_stage="parked",
        mutation_id="restart-profile-matrix",
    )


def _recovery_result(application, recovery: dict[str, Any]) -> dict[str, Any]:
    plan = None
    applied = None
    if recovery["state"] == "repair_required":
        plan = application.preview_repairs()
        selected = [action["id"] for action in plan["actions"]]
        applied = application.apply_repairs(
            plan=plan,
            selected_action_ids=selected,
            authorizer="integration:restart-operator",
        )
    storage = PluginStorage(Path(os.environ["MAP_GOVERNANCE_RESTART_STORAGE_ROOT"]))
    binding = storage.ceo_session_binding(MAP_ID)
    assert binding is not None
    state_database = Path(os.environ["HERMES_HOME"]) / "profiles" / "ceo" / "state.db"
    return {
        "backend_pid": os.getpid(),
        "recovery": recovery,
        "board": application.board(),
        "session": _session_evidence(
            state_database,
            binding["root_session_id"],
        ),
        "runtime": storage.pm_runtime(MAP_ID),
        "outbox": application.outbox_status(
            effect_id="stage-transition:restart-profile-matrix"
        ),
        "repair_plan": plan,
        "repair_apply": applied,
        "repair_history": application.identity_repair_history(map_id=MAP_ID),
    }


def _recover_backend(result_queue) -> None:
    from map_governance.runtime import application_for_profile

    application = application_for_profile("ceo")
    application.stop_outbox_runtime()
    result_queue.put(_recovery_result(application, application.recover_restart()))


def _surviving_backend(command_queue, result_queue) -> None:
    from map_governance.runtime import application_for_profile

    application = application_for_profile("ceo")
    application.stop_outbox_runtime()
    application.recover_restart()
    result_queue.put({"phase": "ready", "pid": os.getpid()})
    command = command_queue.get(timeout=20)
    if command != "recover":
        raise ValueError(f"unexpected backend command: {command}")
    result_queue.put(_recovery_result(application, application.recover_restart()))


def _write_herdr_runtime_state(
    path: Path,
    *,
    runtime: dict[str, Any],
    repository: Path,
    coordinates: dict[str, str],
    restarted: bool,
) -> None:
    path.write_text(
        json.dumps(
            {
                "server_generation": "restarted" if restarted else "original",
                "runtime": {
                    "namespace": runtime["session_namespace"],
                    "workspace_label": runtime["workspace_label"],
                    "agent_id": runtime["agent_id"],
                    "repository_path": str(repository),
                    **coordinates,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _compress_desktop_lineage(state_database: Path, *, root: str, tip: str) -> None:
    from hermes_state import SessionDB

    with SessionDB(db_path=state_database) as database:
        root_record = database.get_session(root)
        assert root_record is not None
        database.end_session(root, "compression")
        database.create_session(
            tip,
            source="desktop",
            profile_name="ceo",
            parent_session_id=root,
            system_prompt=CEO_SYSTEM_PROMPT,
            model_config=CEO_SESSION_MODEL_CONFIG,
        )
        database.set_session_title(tip, root_record["title"])


def _wait_for_expired_lease(storage_root: Path) -> None:
    intent = OutboxRepository(storage_root).intent(
        "stage-transition:restart-profile-matrix"
    )
    assert intent is not None and intent.state == "leased"
    assert intent.lease_expires_at is not None
    expires = datetime.fromisoformat(intent.lease_expires_at.replace("Z", "+00:00"))
    deadline = time.monotonic() + 5
    while datetime.now(timezone.utc) <= expires:
        assert time.monotonic() < deadline
        time.sleep(0.02)


@pytest.mark.parametrize(
    "restart_boundary",
    ["desktop-only", "backend-only", "pm-process", "herdr-server", "full-machine"],
)
def test_profile_backend_restart_matrix_recovers_real_temporary_home(
    tmp_path,
    monkeypatch,
    hermes_host_root,
    restart_boundary,
):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["map-governance"]}}),
        encoding="utf-8",
    )
    skill_root = tmp_path / "external-skills"
    delivery_skill = _write_skill(skill_root, "delivery-pipeline")
    implement_skill = _write_skill(skill_root, "implement")
    ceo_home = _write_profile(
        hermes_home,
        name="ceo",
        toolset="map-governance-ceo",
        skill_roots=[skill_root],
    )
    _write_profile(
        hermes_home,
        name="pm",
        toolset="map-governance-pm",
        skill_roots=[skill_root],
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    bin_dir, github_state_path, herdr_state_path = _write_external_boundaries(tmp_path)
    github_log = tmp_path / "fake-github-calls.jsonl"
    herdr_log = tmp_path / "fake-herdr-calls.jsonl"
    blocked_marker = tmp_path / "fake-github-blocked"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv(
        "PATH",
        f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    )
    monkeypatch.setenv("MAP_GOVERNANCE_RESTART_FAKE_BIN", str(bin_dir))
    monkeypatch.setenv("MAP_GOVERNANCE_RESTART_GH_STATE", str(github_state_path))
    monkeypatch.setenv("MAP_GOVERNANCE_RESTART_GH_LOG", str(github_log))
    monkeypatch.setenv("MAP_GOVERNANCE_RESTART_GH_BLOCKED", str(blocked_marker))
    monkeypatch.setenv("MAP_GOVERNANCE_RESTART_HERDR_STATE", str(herdr_state_path))
    monkeypatch.setenv("MAP_GOVERNANCE_RESTART_HERDR_LOG", str(herdr_log))
    monkeypatch.setenv("MAP_GOVERNANCE_RESTART_REPOSITORY", str(repository))
    monkeypatch.setenv(
        "MAP_GOVERNANCE_RESTART_IMPLEMENT_SKILL",
        str(implement_skill),
    )

    from hermes_cli.plugins import PluginState
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(ceo_home)
    try:
        storage_root = PluginState("map-governance").data_dir
    finally:
        reset_hermes_home_override(token)
    monkeypatch.setenv(
        "MAP_GOVERNANCE_RESTART_STORAGE_ROOT",
        str(storage_root),
    )
    prerequisites = {
        "schema_version": 1,
        "profiles": {"ceo": "ceo", "pm": "pm", "publisher": "default"},
        "skills": {
            "plugin": ["map-governance:ceo", "map-governance:pm"],
            "external": [
                {"id": "delivery-pipeline", "path": str(delivery_skill)},
                {"id": "implement", "path": str(implement_skill)},
            ],
        },
        "routing": {"policy": "claude"},
        "github": {
            "hostname": "github.com",
            "projects": [
                {
                    "id": PROJECT_ID,
                    "url": PROJECT_URL,
                    "owner": "acme",
                    "owner_type": "organization",
                    "number": 7,
                    "required_capability": "write",
                }
            ],
            "repositories": [
                {
                    "coordinate": "acme/atlas",
                    "path": str(repository),
                    "worker_write_required": True,
                    "publication_required": False,
                }
            ],
        },
        "authorities": {
            "worker": {"kind": "local_git", "credential_ref": "local-git"},
            "publisher": {
                "kind": "gh",
                "credential_ref": "gh:github.com:governance",
                "account": "governance",
                "required": False,
            },
        },
        "herdr": {"executable": str(bin_dir / "herdr")},
    }
    storage_root.mkdir(parents=True)
    (storage_root / "prerequisites.yaml").write_text(
        yaml.safe_dump({"prerequisites": prerequisites}, sort_keys=False),
        encoding="utf-8",
    )

    process_context = get_context("spawn")
    seed_report_path = tmp_path / "seed-report.json"
    seed = process_context.Process(
        target=_seed_backend,
        args=(str(seed_report_path),),
    )
    seed.start()
    deadline = time.monotonic() + 20
    while not blocked_marker.exists():
        assert seed.is_alive(), f"seed backend exited with {seed.exitcode}"
        assert time.monotonic() < deadline, "seed backend did not enter fake gh"
        time.sleep(0.02)
    seed.terminate()
    seed.join(timeout=10)
    assert seed.exitcode is not None and seed.exitcode != 0
    seed_report = json.loads(seed_report_path.read_text(encoding="utf-8"))
    _wait_for_expired_lease(storage_root)

    runtime = seed_report["runtime"]
    stable_coordinates = {
        "workspace_id": "workspace-owned",
        "window_id": "window-owned",
        "pane_id": "pane-owned",
        "agent_session_id": "pm-session-owned",
    }
    coordinates = dict(stable_coordinates)
    if restart_boundary == "pm-process":
        coordinates["agent_session_id"] = "pm-session-restarted"
    elif restart_boundary in {"herdr-server", "full-machine"}:
        coordinates = {
            "workspace_id": f"workspace-{restart_boundary}",
            "window_id": f"window-{restart_boundary}",
            "pane_id": f"pane-{restart_boundary}",
            "agent_session_id": f"pm-session-{restart_boundary}",
        }
    state_database = ceo_home / "state.db"
    expected_live = seed_report["canonical"]["live_session_id"]
    results = process_context.Queue()

    if restart_boundary in {"desktop-only", "pm-process", "herdr-server"}:
        _write_herdr_runtime_state(
            herdr_state_path,
            runtime=runtime,
            repository=repository,
            coordinates=stable_coordinates,
            restarted=False,
        )
        commands = process_context.Queue()
        recovered_process = process_context.Process(
            target=_surviving_backend,
            args=(commands, results),
        )
        recovered_process.start()
        ready = results.get(timeout=30)
        assert ready["phase"] == "ready"
        assert recovered_process.is_alive()
        if restart_boundary == "desktop-only":
            expected_live = f"desktop-tip-{restart_boundary}"
            _compress_desktop_lineage(
                state_database,
                root=seed_report["canonical"]["root_session_id"],
                tip=expected_live,
            )
        else:
            _write_herdr_runtime_state(
                herdr_state_path,
                runtime=runtime,
                repository=repository,
                coordinates=coordinates,
                restarted=restart_boundary == "herdr-server",
            )
        commands.put("recover")
        recovered_process.join(timeout=30)
        assert recovered_process.exitcode == 0
        recovered = results.get(timeout=2)
        assert recovered["backend_pid"] == ready["pid"]
    else:
        _write_herdr_runtime_state(
            herdr_state_path,
            runtime=runtime,
            repository=repository,
            coordinates=coordinates,
            restarted=restart_boundary == "full-machine",
        )
        if restart_boundary == "full-machine":
            expected_live = f"desktop-tip-{restart_boundary}"
            _compress_desktop_lineage(
                state_database,
                root=seed_report["canonical"]["root_session_id"],
                tip=expected_live,
            )
        recovered_process = process_context.Process(
            target=_recover_backend,
            args=(results,),
        )
        recovered_process.start()
        recovered_process.join(timeout=30)
        assert recovered_process.exitcode == 0
        recovered = results.get(timeout=2)

    map_card = recovered["board"]["maps"][0]
    assert map_card["stage"] == "parked"
    assert map_card["approval_summary"]["pending_count"] == 1
    assert (
        recovered["session"]["root_session_id"]
        == seed_report["session"]["root_session_id"]
    )
    assert recovered["session"]["live_session_id"] == expected_live
    assert recovered["session"]["root_system_prompt"] == CEO_SYSTEM_PROMPT
    assert (
        recovered["session"]["root_model_config"]
        == seed_report["session"]["root_model_config"]
    )
    assert recovered["session"]["live_system_prompt"] == CEO_SYSTEM_PROMPT
    assert json.loads(recovered["session"]["live_model_config"]) == (
        CEO_SESSION_MODEL_CONFIG
    )
    assert recovered["session"]["messages"] == seed_report["session"]["messages"]
    assert len(recovered["session"]["messages"]) == 2
    assert recovered["outbox"]["state"] == "succeeded"
    assert recovered["outbox"]["attempt_count"] == 2

    github_state = json.loads(github_state_path.read_text(encoding="utf-8"))
    assert github_state["mutation_calls"] == 2
    assert github_state["semantic_transitions"] == 1
    assert github_state["stage"] == "parked"
    herdr_calls = [
        json.loads(line) for line in herdr_log.read_text(encoding="utf-8").splitlines()
    ]
    assert not any(
        operation in call for call in herdr_calls for operation in ("kill", "delete")
    )

    if restart_boundary in {"herdr-server", "full-machine"}:
        assert recovered["recovery"]["state"] == "repair_required"
        assert len(recovered["repair_plan"]["actions"]) == 1
        assert recovered["repair_apply"]["applied_action_ids"] == [
            recovered["repair_plan"]["actions"][0]["id"]
        ]
        assert recovered["runtime"]["state"] == "pm_ready"
        assert recovered["runtime"]["workspace_id"] == coordinates["workspace_id"]
        assert recovered["repair_history"][0]["authorizer"] == (
            "integration:restart-operator"
        )
    else:
        assert recovered["recovery"]["state"] == "recovered"
        assert recovered["repair_plan"] is None
        assert recovered["runtime"]["state"] == "active"
        assert (
            recovered["runtime"]["agent_session_id"] == coordinates["agent_session_id"]
        )
