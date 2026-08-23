from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from map_governance import GovernanceRequestIdentity, MapGovernanceApplication
from map_governance.approvals import ApprovalPacket, normalized_hash
from map_governance.coordinator import (
    CommissioningContext,
    CoordinatorRuntime,
)
from map_governance.reports import PMReportDraft, TrackerPMReportRecord
from map_governance.storage import PluginStorage
from map_governance.tracker import TrackerIssue, TrackerProject


def _fake_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


class StaticCommissioningPrerequisites:
    def __init__(self, context: CommissioningContext) -> None:
        self.context = context

    def commissioning_context(self, *, project_id, repository):
        assert project_id == self.context.project_id
        assert repository == self.context.repository
        return self.context


class FakeCLITracker:
    """Public tracker boundary backed by the isolated fake provider CLI."""

    def __init__(self, executable: Path) -> None:
        self.executable = executable

    def _run(self, *arguments: str):
        completed = subprocess.run(
            [str(self.executable), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return json.loads(completed.stdout)

    def get_project(self, url: str) -> TrackerProject:
        payload = self._run("project", "get", url)
        return TrackerProject(**payload)

    def get_issue(self, url: str) -> TrackerIssue:
        payload = self._run("issue", "get", url)
        payload["labels"] = tuple(payload["labels"])
        return TrackerIssue(**payload)

    def list_decisions(self, _url: str):
        return []

    def list_pm_reports(self, url: str):
        records = []
        for payload in self._run("reports", "list", url):
            report = PMReportDraft(
                record_id=payload["record_id"],
                report_type=payload["report_type"],
                summary=payload["summary"],
                timestamp=payload["timestamp"],
            ).assign_to(payload["map_id"])
            records.append(
                TrackerPMReportRecord(
                    report=report,
                    tracker_record_id=payload["tracker_record_id"],
                    tracker_record_url=payload["tracker_record_url"],
                )
            )
        return records

    def transition_issue_stage(self, url, *, expected_stage, requested_stage):
        payload = self._run(
            "stage",
            "transition",
            url,
            expected_stage,
            requested_stage,
        )
        payload["labels"] = tuple(payload["labels"])
        return TrackerIssue(**payload)


def _authorize_delivery(application: MapGovernanceApplication, issue_url: str) -> None:
    packet = ApprovalPacket(
        request_id="approval-e2e-delivery",
        decision_class="delivery_authorization",
        proposed_action="transition_map",
        alternatives=("Authorize", "Revise"),
        rationale="The isolated Map is ready.",
        cost_risk="Test-owned resources only.",
        evidence=(issue_url,),
        requested_scope={"map_id": "I_test"},
        decision_payload={
            "expected_stage": "awaiting-approval",
            "requested_stage": "authorized",
        },
    )
    application._storage.save_approval_request(
        map_id="I_test",
        packet=packet.payload(),
        packet_hash=packet.packet_hash,
        requested_by_profile="ceo",
        requested_by_session="ceo-test-session",
        requested_at="2026-08-23T22:00:00Z",
        tracker_record_id="IC_request",
        tracker_record_url=f"{issue_url}#issuecomment-request",
    )
    application._storage.apply_approval_decision(
        request_id=packet.request_id,
        decision="approved",
        actor_id="chairman-test",
        actor_profile="ceo",
        note="Approved for isolated E2E.",
        decided_at="2026-08-23T23:00:00Z",
        expires_at="2026-08-25T00:00:00Z",
        tracker_record_id="IC_decision",
        tracker_record_url=f"{issue_url}#issuecomment-decision",
    )
    scope = {"map_id": "I_test"}
    payload = packet.decision_payload
    application._storage.reserve_protected_mutation(
        mutation_id="authorize-e2e-map",
        map_id="I_test",
        request_id=packet.request_id,
        action="transition_map",
        scope=scope,
        payload=payload,
        payload_hash=normalized_hash(
            {"action": "transition_map", "scope": scope, "payload": payload}
        ),
        reserved_at="2026-08-23T23:30:00Z",
    )
    application._storage.confirm_protected_mutation(
        mutation_id="authorize-e2e-map",
        confirmed_at="2026-08-23T23:31:00Z",
    )


def test_temporary_home_e2e_creates_and_resumes_only_test_owned_herdr_resources(
    tmp_path, monkeypatch
):
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))
    state_path = tmp_path / "fake-herdr-state.json"
    log_path = tmp_path / "fake-herdr-calls.jsonl"
    monkeypatch.setenv("MAP_GOVERNANCE_FAKE_HERDR_STATE", str(state_path))
    monkeypatch.setenv("MAP_GOVERNANCE_FAKE_HERDR_LOG", str(log_path))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    fake_hermes_log = tmp_path / "fake-hermes-calls.jsonl"
    fake_gh_log = tmp_path / "fake-gh-calls.jsonl"
    fake_provider_state = tmp_path / "fake-provider-state.json"
    monkeypatch.setenv("MAP_GOVERNANCE_FAKE_HERMES_LOG", str(fake_hermes_log))
    monkeypatch.setenv("MAP_GOVERNANCE_FAKE_GH_LOG", str(fake_gh_log))
    monkeypatch.setenv("MAP_GOVERNANCE_FAKE_PROVIDER_STATE", str(fake_provider_state))
    _fake_executable(
        bin_dir / "hermes",
        """#!/usr/bin/env python3
import json, os, subprocess, sys
with open(os.environ["MAP_GOVERNANCE_FAKE_HERMES_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
if len(sys.argv) != 3 or sys.argv[1] != "ready":
    raise SystemExit(2)
subprocess.run(
    ["gh", "report", "publish", sys.argv[2]],
    check=True,
    capture_output=True,
    text=True,
)
""",
    )
    _fake_executable(
        bin_dir / "gh",
        """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

state_path = Path(os.environ["MAP_GOVERNANCE_FAKE_PROVIDER_STATE"])
with open(os.environ["MAP_GOVERNANCE_FAKE_GH_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
try:
    state = json.loads(state_path.read_text(encoding="utf-8"))
except FileNotFoundError:
    state = {
        "stage": "authorized",
        "reports": [],
        "transitions": 0,
    }

project_url = "https://github.test/orgs/acme/projects/1"
issue_url = "https://github.test/acme/test/issues/1"

def save():
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

def issue():
    return {
        "id": "I_test", "repository": "acme/test", "number": 1,
        "title": "Isolated commissioning Map", "url": issue_url,
        "state": "open", "state_reason": None,
        "labels": ["map", f"map-stage/{state['stage']}"],
    }

args = sys.argv[1:]
if args[:2] == ["project", "get"] and args[2] == project_url:
    print(json.dumps({
        "id": "PVT_test", "owner": "acme", "owner_type": "organization",
        "number": 1, "title": "Isolated portfolio", "url": project_url,
    }))
elif args[:2] == ["issue", "get"] and args[2] == issue_url:
    print(json.dumps(issue()))
elif args[:2] == ["reports", "list"] and args[2] == issue_url:
    print(json.dumps(state["reports"]))
elif args[:2] == ["report", "publish"]:
    payload = json.loads(args[2])
    checkpoint = payload["ready_checkpoint"]
    if not any(item["record_id"] == checkpoint["record_id"] for item in state["reports"]):
        state["reports"].append({
            "map_id": payload["map"]["id"],
            "record_id": checkpoint["record_id"],
            "report_type": checkpoint["type"],
            "summary": checkpoint["summary"],
            "timestamp": checkpoint["timestamp"],
            "tracker_record_id": "IC_ready_e2e",
            "tracker_record_url": issue_url + "#issuecomment-ready-e2e",
        })
        save()
    print(json.dumps({"published": checkpoint["record_id"]}))
elif args[:2] == ["stage", "transition"] and args[2] == issue_url:
    expected, requested = args[3:5]
    if state["stage"] != expected:
        raise SystemExit(3)
    state["stage"] = requested
    state["transitions"] += 1
    save()
    print(json.dumps(issue()))
else:
    raise SystemExit(2)
""",
    )
    herdr = _fake_executable(
        bin_dir / "herdr",
        """#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

state_path = Path(os.environ["MAP_GOVERNANCE_FAKE_HERDR_STATE"])
log_path = Path(os.environ["MAP_GOVERNANCE_FAKE_HERDR_LOG"])
args = sys.argv[1:]
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
try:
    state = json.loads(state_path.read_text(encoding="utf-8"))
except FileNotFoundError:
    state = {"sessions": {}, "workspace_creates": 0, "agent_starts": 0, "prompts": []}

def save():
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

if args == ["session", "list", "--json"]:
    print(json.dumps({"sessions": [
        {"name": name, "default": False, "running": True,
         "socket_path": f"/fake/{name}", "session_dir": f"/fake/{name}/state"}
        for name in sorted(state["sessions"])
    ]}))
    raise SystemExit(0)

if len(args) >= 3 and args[0] == "--session":
    namespace = args[1]
    command = args[2:]
else:
    raise SystemExit(2)

if command == ["server"]:
    state["sessions"].setdefault(namespace, {"workspace": None, "agent": None})
    save()
    raise SystemExit(0)

session = state["sessions"].get(namespace)
if session is None:
    raise SystemExit(1)

if command == ["workspace", "list"]:
    workspaces = []
    if session["workspace"] is not None:
        workspaces.append({
            "workspace_id": session["workspace"]["workspace_id"],
            "label": session["workspace"]["label"],
        })
    print(json.dumps({"id": "fake", "result": {
        "type": "workspace_list", "workspaces": workspaces
    }}))
elif command[:2] == ["workspace", "create"]:
    cwd = command[command.index("--cwd") + 1]
    label = command[command.index("--label") + 1]
    marker = command[command.index("--env") + 1]
    assert marker.startswith("MAP_GOVERNANCE_OWNERSHIP=map-governance:map:")
    session["workspace"] = {
        "workspace_id": "w-test-owned", "label": label, "cwd": cwd,
        "tab_id": "w-test-owned:t1", "pane_id": "w-test-owned:p1"
    }
    state["workspace_creates"] += 1
    save()
    print(json.dumps({"id": "fake", "result": {
        "type": "workspace_created",
        "workspace": {"workspace_id": "w-test-owned", "label": label},
        "tab": {"tab_id": "w-test-owned:t1"},
        "root_pane": {"pane_id": "w-test-owned:p1"},
    }}))
elif command[:2] == ["workspace", "get"]:
    workspace = session["workspace"]
    assert workspace is not None and command[2] == workspace["workspace_id"]
    print(json.dumps({"id": "fake", "result": {
        "type": "workspace_info",
        "workspace": {
            "workspace_id": workspace["workspace_id"],
            "label": workspace["label"],
            "active_tab_id": workspace["tab_id"],
        },
    }}))
elif command[:2] == ["pane", "list"]:
    workspace = session["workspace"]
    assert workspace is not None
    assert command == ["pane", "list", "--workspace", workspace["workspace_id"]]
    print(json.dumps({"id": "fake", "result": {
        "type": "pane_list",
        "panes": [{
            "workspace_id": workspace["workspace_id"],
            "tab_id": workspace["tab_id"],
            "pane_id": workspace["pane_id"],
            "cwd": workspace["cwd"],
        }],
    }}))
elif command[:2] == ["agent", "get"]:
    if session["agent"] is None or command[2] != session["agent"]["name"]:
        raise SystemExit(1)
    print(json.dumps({"id": "fake", "result": {
        "type": "agent_info", "agent": session["agent"]
    }}))
elif command[:2] == ["agent", "start"]:
    name = command[2]
    pane = command[command.index("--pane") + 1]
    passthrough = command[command.index("--") + 1:]
    assert passthrough == [
        "--profile", "pm", "--tui", "--skills",
        "map-governance:pm,delivery-pipeline,herdr"
    ]
    workspace = session["workspace"]
    session["agent"] = {
        "name": name, "agent": "hermes",
        "workspace_id": workspace["workspace_id"],
        "tab_id": workspace["tab_id"], "pane_id": pane,
        "agent_status": "idle",
        "agent_session": {"source": "herdr:hermes", "agent": "hermes",
                          "kind": "id", "value": "hermes-test-session"},
    }
    state["agent_starts"] += 1
    save()
    print(json.dumps({"id": "fake", "result": {
        "type": "agent_started", "agent": session["agent"]
    }}))
elif command[:2] == ["agent", "prompt"]:
    assert command[-3:] == ["--wait", "--timeout", "30000"]
    payload = json.loads(command[3])
    subprocess.run(
        ["hermes", "ready", command[3]],
        check=True,
        capture_output=True,
        text=True,
    )
    state["prompts"].append(payload)
    save()
    print(json.dumps({"id": "fake", "result": {
        "type": "agent_prompted", "agent": session["agent"]
    }}))
else:
    raise SystemExit(2)
""",
    )
    project_url = "https://github.test/orgs/acme/projects/1"
    issue_url = "https://github.test/acme/test/issues/1"
    repository = tmp_path / "repository"
    repository.mkdir()
    storage_root = tmp_path / "plugin-data"
    storage = PluginStorage(storage_root)
    pm_storage_root = tmp_path / "pm-profile" / "plugin-data"
    context = CommissioningContext(
        project_id="PVT_test",
        project_url=project_url,
        repository="acme/test",
        repository_path=str(repository),
        pm_profile="pm",
        routing_policy="codex",
        herdr_executable=str(herdr),
        skills=("map-governance:pm", "delivery-pipeline", "herdr"),
        ceo_profile="ceo",
        pm_storage_root=str(pm_storage_root),
    )
    clock_values = iter(f"2026-08-24T00:00:{second:02d}Z" for second in range(30))
    runtime = CoordinatorRuntime(
        storage=storage,
        clock=lambda: next(clock_values),
        startup_poll_seconds=0.01,
    )
    tracker = FakeCLITracker(bin_dir / "gh")
    prerequisites = StaticCommissioningPrerequisites(context)
    application = MapGovernanceApplication(
        plugin_root=Path(__file__).resolve().parents[1],
        storage_root=storage_root,
        tracker=tracker,
        profile_name="ceo",
        clock=lambda: datetime(2026, 8, 24, tzinfo=timezone.utc),
        commissioning_prerequisites=prerequisites,
        coordinator_runtime=runtime,
    )
    project = application.configure_project(project_url=project_url)
    application.bind_map(project_id=project["id"], issue_url=issue_url)
    application._storage.save_ceo_session_ready(
        map_id="I_test",
        profile_name="ceo",
        canonical_identity="map:e2e",
        canonical_title="Map: isolated E2E",
        root_session_id="ceo-test-root",
        live_session_id="ceo-test-session",
        last_activity_at=None,
        bootstrap_hash="sha256:e2e-bootstrap",
        updated_at="2026-08-24T00:00:00Z",
    )
    _authorize_delivery(application, issue_url)

    first = application.commission_map(
        map_id="I_test",
        request_identity=GovernanceRequestIdentity("ceo", "ceo-test-session"),
    )
    restarted_runtime = CoordinatorRuntime(
        storage=storage,
        clock=lambda: next(clock_values),
        startup_poll_seconds=0.01,
    )
    restarted_application = MapGovernanceApplication(
        plugin_root=Path(__file__).resolve().parents[1],
        storage_root=storage_root,
        tracker=tracker,
        profile_name="ceo",
        clock=lambda: datetime(2026, 8, 24, tzinfo=timezone.utc),
        commissioning_prerequisites=prerequisites,
        coordinator_runtime=restarted_runtime,
    )
    restarted = restarted_application.commission_map(
        map_id="I_test",
        request_identity=GovernanceRequestIdentity("ceo", "ceo-test-session"),
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    provider_state = json.loads(fake_provider_state.read_text(encoding="utf-8"))
    calls = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    hermes_calls = [
        json.loads(line)
        for line in fake_hermes_log.read_text(encoding="utf-8").splitlines()
    ]
    gh_calls = [
        json.loads(line)
        for line in fake_gh_log.read_text(encoding="utf-8").splitlines()
    ]
    assert first["state"] == restarted["state"] == "active"
    assert (
        first["checkpoint"]
        == restarted["checkpoint"]
        == {
            "state": "tracker_confirmed",
            "record_id": "commission-ready-I_test",
        }
    )
    assert restarted["idempotent"] is True
    assert first["workspace_id"] == restarted["workspace_id"] == "w-test-owned"
    assert state["workspace_creates"] == 1
    assert state["agent_starts"] == 1
    assert len(state["prompts"]) == 1
    assert sum(call[-1:] == ["server"] for call in calls) == 1
    assert provider_state["stage"] == "delivery"
    assert provider_state["transitions"] == 1
    assert [report["record_id"] for report in provider_state["reports"]] == [
        "commission-ready-I_test"
    ]
    assert len(hermes_calls) == 1 and hermes_calls[0][0] == "ready"
    assert ["report", "publish"] in [call[:2] for call in gh_calls]
    assert ["stage", "transition"] in [call[:2] for call in gh_calls]
    assignment = restarted_application._storage.pm_assignment("I_test")
    assert assignment["session_id"] == "hermes-test-session"
    handoff = PluginStorage(pm_storage_root).pm_control_plane_for_request(
        profile_name="pm",
        session_id="hermes-test-session",
    )
    assert handoff["map_id"] == "I_test"
    assert handoff["control_profile"] == "ceo"
    assert list(isolated_home.iterdir()) == []
