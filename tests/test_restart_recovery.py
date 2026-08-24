from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from map_governance import CEOSessionRepairRequired, MapGovernanceApplication
from map_governance.coordinator import (
    CommissioningContext,
    CoordinatorCommandResult,
    CoordinatorRuntime,
    CoordinatorRuntimeError,
)
from map_governance.sessions import CanonicalSession, canonical_session_title
from map_governance.storage import PluginStorage
from map_governance.tracker import TrackerError, TrackerIssue, TrackerProject


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MAP_ID = "I_atlas_41"
PROJECT_ID = "PVT_acme_7"
PROJECT_URL = "https://github.com/orgs/acme/projects/7"
ISSUE_URL = "https://github.com/acme/atlas/issues/41"
NOW = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)


class RecoveryTracker:
    def __init__(self) -> None:
        self.project = TrackerProject(
            id=PROJECT_ID,
            owner="acme",
            owner_type="organization",
            number=7,
            title="Acme portfolio",
            url=PROJECT_URL,
        )
        self.issue = TrackerIssue(
            id=MAP_ID,
            repository="acme/atlas",
            number=41,
            title="Map the Atlas launch",
            url=ISSUE_URL,
            state="open",
            state_reason=None,
            labels=("map", "map-stage/authorized"),
        )
        self.transition_calls = []

    def get_project(self, url: str) -> TrackerProject:
        assert url == PROJECT_URL
        return self.project

    def get_issue(self, url: str) -> TrackerIssue:
        assert url == ISSUE_URL
        return self.issue

    def list_decisions(self, url: str):
        assert url == ISSUE_URL
        return []

    def list_approval_events(self, url: str):
        assert url == ISSUE_URL
        return []

    def list_pm_reports(self, url: str):
        assert url == ISSUE_URL
        return []

    def transition_issue_stage(self, url, *, expected_stage, requested_stage):
        assert url == ISSUE_URL
        self.transition_calls.append((expected_stage, requested_stage))
        current = next(
            label.removeprefix("map-stage/")
            for label in self.issue.labels
            if label.startswith("map-stage/")
        )
        assert current == expected_stage
        self.issue = replace(
            self.issue,
            labels=tuple(
                label
                for label in self.issue.labels
                if not label.startswith("map-stage/")
            )
            + (f"map-stage/{requested_stage}",),
        )
        return self.issue


class RecoverySessionRunner:
    def __init__(self) -> None:
        self.sessions: dict[str, CanonicalSession] = {}
        self.markers: set[tuple[str, str]] = set()
        self.bootstrap_markers: set[tuple[str, str]] = set()
        self.mint_count = 0
        self.resume_error: RuntimeError | None = None

    def find_exact(self, *, title: str) -> list[CanonicalSession]:
        return [session for session in self.sessions.values() if session.title == title]

    def initialize(self, session, *, bootstrap, idempotency_key):
        self.bootstrap_markers.add((session.root_session_id, idempotency_key))
        return session

    def mint(
        self,
        *,
        identity: str,
        title: str,
        profile_name: str,
        bootstrap: str,
        idempotency_key: str,
    ) -> CanonicalSession:
        self.mint_count += 1
        session = CanonicalSession(
            root_session_id="ceo-root",
            live_session_id="ceo-root",
            title=title,
            last_activity_at="2026-08-24T02:00:00Z",
            bootstrap_sent=True,
        )
        self.sessions[session.root_session_id] = session
        self.bootstrap_markers.add((session.root_session_id, idempotency_key))
        return session

    def resolve(self, *, root_session_id: str) -> CanonicalSession | None:
        return self.sessions.get(root_session_id)

    def load_skill(self, session, *, content, idempotency_key):
        self.markers.add((session.root_session_id, idempotency_key))
        return session

    def has_bootstrap_marker(
        self,
        *,
        root_session_id: str,
        idempotency_key: str,
    ) -> bool:
        session = self.sessions.get(root_session_id)
        return bool(
            session is not None
            and (root_session_id, idempotency_key) in self.bootstrap_markers
        )

    def has_resume_marker(self, *, root_session_id: str, idempotency_key: str) -> bool:
        return (root_session_id, idempotency_key) in self.markers

    def resume_once(
        self,
        *,
        root_session_id: str,
        content: str,
        idempotency_key: str,
    ) -> CanonicalSession:
        if self.resume_error is not None:
            raise self.resume_error
        session = self.sessions[root_session_id]
        self.markers.add((root_session_id, idempotency_key))
        return session


class RecoveryPrerequisites:
    def __init__(self, context: CommissioningContext) -> None:
        self.context = context

    def commissioning_context(self, *, project_id: str, repository: str):
        assert project_id == self.context.project_id
        assert repository == self.context.repository
        return self.context


class RecoveryCoordinator:
    def __init__(self, storage_root: Path) -> None:
        self.storage = PluginStorage(storage_root)
        self.ensure_requests = []
        self.repair_candidates = {}
        self.ensure_error: CoordinatorRuntimeError | None = None
        self.rediscovered_coordinates: dict[str, str] | None = None

    def status(self, *, map_id: str):
        return self.storage.pm_runtime(map_id) or {
            "map_id": map_id,
            "state": "not_commissioned",
        }

    def ensure_root(self, request):
        self.ensure_requests.append(request)
        if self.ensure_error is not None:
            raise self.ensure_error
        if self.rediscovered_coordinates is not None:
            self.storage.update_pm_runtime(
                map_id=request.map_id,
                state="active",
                failure=None,
                updated_at="2026-08-24T02:01:00Z",
                **self.rediscovered_coordinates,
            )
        return self.storage.pm_runtime(request.map_id)

    def record_failure(self, *, map_id, reason, retryable, repair_required=False):
        self.storage.update_pm_runtime(
            map_id=map_id,
            state="repair_required" if repair_required else "active",
            failure={
                "reason": reason,
                "retryable": retryable,
                "repair_required": repair_required,
                "resource_disposition": "no_cleanup_without_verified_ownership",
            },
            updated_at="2026-08-24T02:00:00Z",
        )
        return self.storage.pm_runtime(map_id)

    def preview_repair(self, *, map_id):
        candidate = self.repair_candidates.get(map_id)
        if candidate is None:
            raise CoordinatorRuntimeError(
                reason="runtime_repair_evidence_unavailable",
                retryable=True,
                repair_required=True,
            )
        return candidate


class ScriptedHerdrRecoveryRunner:
    def __init__(self, results: list[CoordinatorCommandResult]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, ...]] = []
        self.starts: list[tuple[str, ...]] = []

    def run(self, arguments, *, timeout):
        assert timeout > 0
        self.calls.append(tuple(arguments))
        return self.results.pop(0)

    def start(self, arguments):
        self.starts.append(tuple(arguments))


def _herdr_result(payload: dict) -> CoordinatorCommandResult:
    return CoordinatorCommandResult(
        returncode=0,
        stdout=json.dumps(payload),
        timed_out=False,
    )


def _herdr_session_list(*names: str) -> CoordinatorCommandResult:
    return _herdr_result(
        {
            "sessions": [
                {"name": name, "default": False, "running": True} for name in names
            ]
        }
    )


def _herdr_workspace_list(
    *,
    label: str,
    workspace_id: str,
) -> CoordinatorCommandResult:
    return _herdr_result(
        {"result": {"workspaces": [{"workspace_id": workspace_id, "label": label}]}}
    )


def _herdr_workspace(
    *,
    label: str,
    workspace_id: str,
    window_id: str,
) -> CoordinatorCommandResult:
    return _herdr_result(
        {
            "result": {
                "workspace": {
                    "workspace_id": workspace_id,
                    "label": label,
                    "active_tab_id": window_id,
                }
            }
        }
    )


def _herdr_pane(
    *,
    repository_path: str,
    workspace_id: str,
    window_id: str,
    pane_id: str,
) -> CoordinatorCommandResult:
    return _herdr_result(
        {
            "result": {
                "panes": [
                    {
                        "workspace_id": workspace_id,
                        "tab_id": window_id,
                        "pane_id": pane_id,
                        "cwd": repository_path,
                    }
                ]
            }
        }
    )


def _herdr_agent(
    *,
    agent_id: str,
    workspace_id: str,
    window_id: str,
    pane_id: str,
    agent_session_id: str,
) -> CoordinatorCommandResult:
    return _herdr_result(
        {
            "result": {
                "agent": {
                    "name": agent_id,
                    "agent": "hermes",
                    "workspace_id": workspace_id,
                    "tab_id": window_id,
                    "pane_id": pane_id,
                    "agent_session": {
                        "agent": "hermes",
                        "kind": "id",
                        "value": agent_session_id,
                    },
                }
            }
        }
    )


class SimulatedRecoveryCrash(BaseException):
    pass


def _application(storage_root, tracker, sessions):
    return MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        session_runner=sessions,
        profile_name="ceo",
        clock=lambda: NOW,
    )


def test_backend_restart_rebuilds_projection_and_reconnects_canonical_lineage(
    tmp_path,
):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    tracker = RecoveryTracker()
    sessions = RecoverySessionRunner()
    first = _application(storage_root, tracker, sessions)
    project = first.configure_project(project_url=PROJECT_URL)
    first.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    opened = first.open_map(map_id=MAP_ID)

    tracker.issue = replace(
        tracker.issue,
        title="Map the recovered Atlas launch",
        labels=("map", "map-stage/delivery"),
    )
    restarted = _application(storage_root, tracker, sessions)

    recovered = restarted.recover_restart()

    assert recovered["state"] == "recovered"
    assert recovered["projections"] == {
        "project_count": 1,
        "map_count": 1,
        "state": "rebuilt",
    }
    assert recovered["ceo_sessions"] == [
        {
            "map_id": MAP_ID,
            "state": "reconnected",
            "root_session_id": opened["ceo_session"]["root_session_id"],
            "live_session_id": opened["ceo_session"]["live_session_id"],
        }
    ]
    card = restarted.board()["maps"][0]
    assert card["title"] == "Map the recovered Atlas launch"
    assert card["stage"] == "delivery"
    assert sessions.mint_count == 1


def test_missing_ceo_lineage_requires_preview_and_selected_audited_repair(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    tracker = RecoveryTracker()
    sessions = RecoverySessionRunner()
    first = _application(storage_root, tracker, sessions)
    project = first.configure_project(project_url=PROJECT_URL)
    first.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    opened = first.open_map(map_id=MAP_ID)
    title = sessions.sessions[opened["ceo_session"]["root_session_id"]].title

    sessions.sessions.clear()
    sessions.sessions["recovered-root"] = CanonicalSession(
        root_session_id="recovered-root",
        live_session_id="recovered-tip",
        title=title,
        last_activity_at="2026-08-24T02:01:00Z",
        bootstrap_sent=True,
    )
    bootstrap_marker = next(
        marker
        for root, marker in sessions.bootstrap_markers
        if root == opened["ceo_session"]["root_session_id"]
    )
    sessions.bootstrap_markers.add(("recovered-root", bootstrap_marker))
    restarted = _application(storage_root, tracker, sessions)

    recovery = restarted.recover_restart()
    plan = restarted.preview_repairs()

    assert recovery["state"] == "repair_required"
    assert plan["blocked"] == []
    assert plan["actions"] == [
        {
            "id": f"ceo-session:{MAP_ID}:rebind:recovered-root",
            "kind": "ceo_session.rebind",
            "map_id": MAP_ID,
            "safe": True,
            "before": {
                "root_session_id": opened["ceo_session"]["root_session_id"],
                "live_session_id": opened["ceo_session"]["live_session_id"],
                "state": "repair_required",
                "repair_reason": "recorded_session_lineage_missing",
            },
            "after": {
                "root_session_id": "recovered-root",
                "live_session_id": "recovered-tip",
                "state": "ready",
            },
            "evidence": {
                "profile_name": "ceo",
                "canonical_title": title,
                "exact_candidate_count": 1,
                "bootstrap_marker_verified": True,
                "candidate_last_activity_at": "2026-08-24T02:01:00Z",
            },
        }
    ]
    action_id = plan["actions"][0]["id"]

    applied = restarted.apply_repairs(
        plan=plan,
        selected_action_ids=[action_id],
        authorizer="basic:local-operator",
    )

    assert applied["state"] == "applied"
    assert applied["applied_action_ids"] == [action_id]
    assert restarted.map_detail(map_id=MAP_ID)["ceo_session"]["state"] == "ready"
    assert restarted.identity_repair_history(map_id=MAP_ID) == [
        {
            "repair_id": f"{plan['plan_id']}:{action_id}",
            "plan_id": plan["plan_id"],
            "action_id": action_id,
            "map_id": MAP_ID,
            "resource_type": "ceo_session",
            "authorizer": "basic:local-operator",
            "before": plan["actions"][0]["before"],
            "after": plan["actions"][0]["after"],
            "evidence": plan["actions"][0]["evidence"],
            "applied_at": "2026-08-24T02:00:00Z",
        }
    ]
    assert sessions.mint_count == 1

    replayed = restarted.apply_repairs(
        plan=plan,
        selected_action_ids=[action_id],
        authorizer="basic:local-operator",
    )

    assert replayed["applied_action_ids"] == []
    assert replayed["idempotent_action_ids"] == [action_id]
    assert len(restarted.identity_repair_history(map_id=MAP_ID)) == 1


def test_uninitialized_ceo_candidate_is_not_offered_as_a_safe_repair(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    tracker = RecoveryTracker()
    sessions = RecoverySessionRunner()
    first = _application(storage_root, tracker, sessions)
    project = first.configure_project(project_url=PROJECT_URL)
    first.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    opened = first.open_map(map_id=MAP_ID)
    title = sessions.sessions[opened["ceo_session"]["root_session_id"]].title
    sessions.sessions = {
        "uninitialized-root": CanonicalSession(
            root_session_id="uninitialized-root",
            live_session_id="uninitialized-tip",
            title=title,
            last_activity_at="2026-08-24T02:01:00Z",
            bootstrap_sent=False,
        )
    }
    restarted = _application(storage_root, tracker, sessions)
    restarted.recover_restart()

    plan = restarted.preview_repairs()

    assert plan["actions"] == []
    assert plan["blocked"] == [
        {
            "resource": "ceo_session",
            "map_id": MAP_ID,
            "reason": "canonical_session_bootstrap_unverified",
            "evidence": {
                "profile_name": "ceo",
                "canonical_title": title,
                "exact_candidate_count": 1,
                "bootstrap_marker_verified": False,
            },
        }
    ]


def test_open_map_cannot_bypass_explicit_repair_for_a_replacement_root(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    tracker = RecoveryTracker()
    sessions = RecoverySessionRunner()
    first = _application(storage_root, tracker, sessions)
    project = first.configure_project(project_url=PROJECT_URL)
    first.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    opened = first.open_map(map_id=MAP_ID)
    old_root = opened["ceo_session"]["root_session_id"]
    title = sessions.sessions.pop(old_root).title
    marker = next(
        value for root, value in sessions.bootstrap_markers if root == old_root
    )
    sessions.sessions["replacement-root"] = CanonicalSession(
        root_session_id="replacement-root",
        live_session_id="replacement-tip",
        title=title,
        last_activity_at="2026-08-24T02:01:00Z",
        bootstrap_sent=True,
    )
    sessions.bootstrap_markers.add(("replacement-root", marker))
    restarted = _application(storage_root, tracker, sessions)
    restarted.recover_restart()

    with pytest.raises(CEOSessionRepairRequired):
        restarted.open_map(map_id=MAP_ID)

    binding = PluginStorage(storage_root).ceo_session_binding(MAP_ID)
    assert binding["root_session_id"] == old_root
    assert binding["state"] == "repair_required"
    assert restarted.identity_repair_history(map_id=MAP_ID) == []


def test_bootstrap_marker_remains_repairable_after_projection_changes(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    tracker = RecoveryTracker()
    sessions = RecoverySessionRunner()
    first = _application(storage_root, tracker, sessions)
    project = first.configure_project(project_url=PROJECT_URL)
    first.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    opened = first.open_map(map_id=MAP_ID)
    old_root = opened["ceo_session"]["root_session_id"]
    original_hash = PluginStorage(storage_root).ceo_session_binding(MAP_ID)[
        "bootstrap_hash"
    ]
    original_marker = next(
        value for root, value in sessions.bootstrap_markers if root == old_root
    )
    tracker.issue = replace(
        tracker.issue,
        title="Map the renamed Atlas launch",
        labels=("map", "map-stage/delivery"),
    )
    _application(storage_root, tracker, sessions).recover_restart()
    sessions.sessions.pop(old_root)
    sessions.sessions["known-initialized-root"] = CanonicalSession(
        root_session_id="known-initialized-root",
        live_session_id="known-initialized-tip",
        title=canonical_session_title(MAP_ID),
        last_activity_at="2026-08-24T02:05:00Z",
        bootstrap_sent=True,
    )
    sessions.bootstrap_markers.add(("known-initialized-root", original_marker))
    restarted = _application(storage_root, tracker, sessions)
    restarted.recover_restart()

    plan = restarted.preview_repairs()

    assert (
        PluginStorage(storage_root).ceo_session_binding(MAP_ID)["bootstrap_hash"]
        == original_hash
    )
    assert [action["after"]["root_session_id"] for action in plan["actions"]] == [
        "known-initialized-root"
    ]


def test_profile_conflict_is_durable_and_visible_to_recovery_and_preview(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    tracker = RecoveryTracker()
    sessions = RecoverySessionRunner()
    first = _application(storage_root, tracker, sessions)
    project = first.configure_project(project_url=PROJECT_URL)
    first.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    first.open_map(map_id=MAP_ID)
    with sqlite3.connect(storage_root / "registry.db") as connection:
        connection.execute(
            """
            UPDATE ceo_session_bindings
            SET profile_name = ?, canonical_identity = ?
            WHERE map_id = ?
            """,
            ("other-ceo", "map-governance:ceo:other-ceo:I_atlas_41", MAP_ID),
        )
    restarted = _application(storage_root, tracker, sessions)

    recovered = restarted.recover_restart()
    plan = restarted.preview_repairs()

    assert recovered["state"] == "repair_required"
    assert recovered["ceo_sessions"] == [
        {
            "map_id": MAP_ID,
            "state": "repair_required",
            "reason": "canonical_profile_mismatch",
            "evidence": {
                "recorded_profile_name": "other-ceo",
                "requested_profile_name": "ceo",
            },
        }
    ]
    assert plan["actions"] == []
    assert plan["blocked"] == [
        {
            "resource": "ceo_session",
            "map_id": MAP_ID,
            "reason": "canonical_profile_mismatch",
            "evidence": {
                "recorded_profile_name": "other-ceo",
                "requested_profile_name": "ceo",
            },
        }
    ]
    binding = PluginStorage(storage_root).ceo_session_binding(MAP_ID)
    assert binding["profile_name"] == "other-ceo"
    assert binding["state"] == "repair_required"


def test_repair_apply_fails_closed_when_preview_evidence_drifts(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    tracker = RecoveryTracker()
    sessions = RecoverySessionRunner()
    first = _application(storage_root, tracker, sessions)
    project = first.configure_project(project_url=PROJECT_URL)
    first.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    opened = first.open_map(map_id=MAP_ID)
    title = sessions.sessions[opened["ceo_session"]["root_session_id"]].title
    sessions.sessions = {
        "recovered-root": CanonicalSession(
            root_session_id="recovered-root",
            live_session_id="recovered-tip",
            title=title,
            last_activity_at="2026-08-24T02:01:00Z",
            bootstrap_sent=True,
        )
    }
    bootstrap_marker = next(
        marker
        for root, marker in sessions.bootstrap_markers
        if root == opened["ceo_session"]["root_session_id"]
    )
    sessions.bootstrap_markers.add(("recovered-root", bootstrap_marker))
    restarted = _application(storage_root, tracker, sessions)
    restarted.recover_restart()
    plan = restarted.preview_repairs()
    sessions.sessions["recovered-root"] = replace(
        sessions.sessions["recovered-root"],
        last_activity_at="2026-08-24T02:02:00Z",
    )

    with pytest.raises(ValueError, match="preview is stale"):
        restarted.apply_repairs(
            plan=plan,
            selected_action_ids=[plan["actions"][0]["id"]],
            authorizer="basic:local-operator",
        )

    assert restarted.identity_repair_history(map_id=MAP_ID) == []


def test_transient_ceo_resume_failure_stays_pending_without_minting(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    tracker = RecoveryTracker()
    sessions = RecoverySessionRunner()
    first = _application(storage_root, tracker, sessions)
    project = first.configure_project(project_url=PROJECT_URL)
    first.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    opened = first.open_map(map_id=MAP_ID)
    root = opened["ceo_session"]["root_session_id"]
    sessions.sessions[root] = replace(
        sessions.sessions[root],
        live_session_id="ceo-restarted-tip",
    )
    sessions.markers.clear()
    sessions.resume_error = RuntimeError("Hermes session backend unavailable")

    recovered = _application(storage_root, tracker, sessions).recover_restart()

    assert recovered["state"] == "pending"
    assert recovered["ceo_sessions"] == [
        {
            "map_id": MAP_ID,
            "state": "pending",
            "reason": "session_reconnect_pending",
        }
    ]
    assert sessions.mint_count == 1
    assert PluginStorage(storage_root).ceo_session_binding(MAP_ID)["state"] == "ready"


def test_tracker_unavailability_reports_stale_projection_from_durable_state(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    tracker = RecoveryTracker()
    first = _application(storage_root, tracker, RecoverySessionRunner())
    project = first.configure_project(project_url=PROJECT_URL)
    first.bind_map(project_id=project["id"], issue_url=ISSUE_URL)

    def unavailable(_url: str):
        raise TrackerError("tracker unavailable")

    tracker.get_project = unavailable
    recovered = _application(
        storage_root, tracker, RecoverySessionRunner()
    ).recover_restart()

    assert recovered["state"] == "stale"
    assert recovered["projections"] == {
        "project_count": 1,
        "map_count": 1,
        "state": "stale",
    }


def test_duplicate_ceo_candidates_fail_closed_without_a_repair_action(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    tracker = RecoveryTracker()
    sessions = RecoverySessionRunner()
    first = _application(storage_root, tracker, sessions)
    project = first.configure_project(project_url=PROJECT_URL)
    first.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    opened = first.open_map(map_id=MAP_ID)
    title = sessions.sessions[opened["ceo_session"]["root_session_id"]].title
    sessions.sessions.clear()
    for root in ("duplicate-a", "duplicate-b"):
        sessions.sessions[root] = CanonicalSession(
            root_session_id=root,
            live_session_id=root,
            title=title,
            last_activity_at="2026-08-24T02:01:00Z",
        )
    restarted = _application(storage_root, tracker, sessions)

    recovery = restarted.recover_restart()
    plan = restarted.preview_repairs()

    assert recovery["state"] == "repair_required"
    assert restarted.map_detail(map_id=MAP_ID)["ceo_session"] == {
        "state": "repair_required",
        "repair": {
            "reason": "multiple_exact_canonical_sessions",
            "candidate_count": 2,
        },
    }
    assert plan["actions"] == []
    assert plan["blocked"] == [
        {
            "resource": "ceo_session",
            "map_id": MAP_ID,
            "reason": "canonical_session_ambiguous",
            "evidence": {
                "profile_name": "ceo",
                "canonical_title": title,
                "exact_candidate_count": 2,
            },
        }
    ]
    assert sessions.mint_count == 1


def test_duplicate_ceo_candidate_is_detected_even_when_recorded_root_resolves(
    tmp_path,
):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    tracker = RecoveryTracker()
    sessions = RecoverySessionRunner()
    first = _application(storage_root, tracker, sessions)
    project = first.configure_project(project_url=PROJECT_URL)
    first.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    opened = first.open_map(map_id=MAP_ID)
    title = sessions.sessions[opened["ceo_session"]["root_session_id"]].title
    sessions.sessions["duplicate-root"] = CanonicalSession(
        root_session_id="duplicate-root",
        live_session_id="duplicate-root",
        title=title,
        last_activity_at="2026-08-24T02:04:00Z",
        bootstrap_sent=True,
    )

    recovered = _application(storage_root, tracker, sessions).recover_restart()

    assert recovered["state"] == "repair_required"
    assert recovered["ceo_sessions"] == [
        {
            "map_id": MAP_ID,
            "state": "repair_required",
            "reason": "multiple_exact_canonical_sessions",
        }
    ]
    binding = PluginStorage(storage_root).ceo_session_binding(MAP_ID)
    assert binding["repair_candidate_count"] == 2
    assert sessions.mint_count == 1


def test_recorded_ceo_root_reconnects_when_the_same_lineage_returns(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    tracker = RecoveryTracker()
    sessions = RecoverySessionRunner()
    first = _application(storage_root, tracker, sessions)
    project = first.configure_project(project_url=PROJECT_URL)
    first.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    opened = first.open_map(map_id=MAP_ID)
    root = opened["ceo_session"]["root_session_id"]
    recorded = sessions.sessions.pop(root)

    missing = _application(storage_root, tracker, sessions).recover_restart()
    sessions.sessions[root] = replace(
        recorded,
        live_session_id="ceo-restored-tip",
        last_activity_at="2026-08-24T02:03:00Z",
    )
    restored = _application(storage_root, tracker, sessions).recover_restart()

    assert missing["state"] == "repair_required"
    assert restored["state"] == "recovered"
    assert restored["ceo_sessions"] == [
        {
            "map_id": MAP_ID,
            "state": "reconnected",
            "root_session_id": root,
            "live_session_id": "ceo-restored-tip",
        }
    ]
    assert PluginStorage(storage_root).ceo_session_binding(MAP_ID)["state"] == "ready"
    assert sessions.mint_count == 1


def test_pm_restart_rediscovers_only_the_recorded_map_owned_runtime(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    repository = tmp_path / "repository"
    repository.mkdir()
    tracker = RecoveryTracker()
    first = _application(storage_root, tracker, RecoverySessionRunner())
    project = first.configure_project(project_url=PROJECT_URL)
    first.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    context = CommissioningContext(
        project_id=PROJECT_ID,
        project_url=PROJECT_URL,
        repository="acme/atlas",
        repository_path=str(repository),
        pm_profile="pm",
        routing_policy="codex",
        herdr_executable="herdr-test",
        skills=("map-governance:pm", "delivery-pipeline", "herdr"),
        ceo_profile="ceo",
        pm_storage_root=str(tmp_path / "pm-profile" / "plugin-data"),
    )
    storage = PluginStorage(storage_root)
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
    runtime = RecoveryCoordinator(storage_root)
    restarted = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        profile_name="ceo",
        clock=lambda: NOW,
        commissioning_prerequisites=RecoveryPrerequisites(context),
        coordinator_runtime=runtime,
    )

    recovered = restarted.recover_restart()

    assert recovered["state"] == "recovered"
    assert recovered["pm_runtimes"] == [
        {
            "map_id": MAP_ID,
            "state": "rediscovered",
            "runtime_state": "active",
        }
    ]
    assert [request.map_id for request in runtime.ensure_requests] == [MAP_ID]
    assert runtime.ensure_requests[0].context == context


def test_herdr_ownership_conflict_is_durably_marked_for_repair(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    repository = tmp_path / "repository"
    repository.mkdir()
    tracker = RecoveryTracker()
    first = _application(storage_root, tracker, RecoverySessionRunner())
    project = first.configure_project(project_url=PROJECT_URL)
    first.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    context = CommissioningContext(
        project_id=PROJECT_ID,
        project_url=PROJECT_URL,
        repository="acme/atlas",
        repository_path=str(repository),
        pm_profile="pm",
        routing_policy="codex",
        herdr_executable="herdr-test",
        skills=("map-governance:pm", "delivery-pipeline", "herdr"),
        ceo_profile="ceo",
        pm_storage_root=str(tmp_path / "pm-profile" / "plugin-data"),
    )
    storage = PluginStorage(storage_root)
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
        updated_at="2026-08-24T01:01:00Z",
    )
    runtime = RecoveryCoordinator(storage_root)
    runtime.ensure_error = CoordinatorRuntimeError(
        reason="session_ownership_mismatch",
        retryable=False,
        repair_required=True,
    )
    restarted = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        profile_name="ceo",
        clock=lambda: NOW,
        commissioning_prerequisites=RecoveryPrerequisites(context),
        coordinator_runtime=runtime,
    )

    recovered = restarted.recover_restart()

    assert recovered["state"] == "repair_required"
    assert runtime.status(map_id=MAP_ID)["state"] == "repair_required"
    assert runtime.status(map_id=MAP_ID)["failure"] == {
        "reason": "session_ownership_mismatch",
        "retryable": False,
        "repair_required": True,
        "resource_disposition": "no_cleanup_without_verified_ownership",
    }
    assert restarted.preview_repairs()["blocked"] != []


def test_runtime_binding_conflict_stops_before_any_herdr_resume(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    recorded_repository = tmp_path / "recorded-repository"
    selected_repository = tmp_path / "selected-repository"
    recorded_repository.mkdir()
    selected_repository.mkdir()
    tracker = RecoveryTracker()
    first = _application(storage_root, tracker, RecoverySessionRunner())
    project = first.configure_project(project_url=PROJECT_URL)
    first.bind_map(project_id=project["id"], issue_url=ISSUE_URL)

    def context(repository_path: Path) -> CommissioningContext:
        return CommissioningContext(
            project_id=PROJECT_ID,
            project_url=PROJECT_URL,
            repository="acme/atlas",
            repository_path=str(repository_path),
            pm_profile="pm",
            routing_policy="codex",
            herdr_executable="herdr-test",
            skills=("map-governance:pm", "delivery-pipeline", "herdr"),
            ceo_profile="ceo",
            pm_storage_root=str(tmp_path / "pm-profile" / "plugin-data"),
        )

    recorded = context(recorded_repository)
    storage = PluginStorage(storage_root)
    lifecycle = storage.coordinator_lifecycle(created_at="2026-08-24T01:00:00Z")
    storage.reserve_pm_runtime(
        map_id=MAP_ID,
        session_namespace=CoordinatorRuntime.session_namespace(lifecycle),
        workspace_label=CoordinatorRuntime.workspace_label(lifecycle, MAP_ID),
        agent_id=CoordinatorRuntime.agent_name(lifecycle, MAP_ID),
        ownership_marker=CoordinatorRuntime.ownership_marker(lifecycle, MAP_ID),
        lifecycle_id=lifecycle,
        context=recorded,
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
        updated_at="2026-08-24T01:01:00Z",
    )
    runtime = RecoveryCoordinator(storage_root)
    restarted = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        profile_name="ceo",
        clock=lambda: NOW,
        commissioning_prerequisites=RecoveryPrerequisites(context(selected_repository)),
        coordinator_runtime=runtime,
    )

    recovered = restarted.recover_restart()

    assert recovered["state"] == "repair_required"
    assert recovered["pm_runtimes"] == [
        {
            "map_id": MAP_ID,
            "state": "repair_required",
            "reason": "runtime_map_binding_conflict",
        }
    ]
    assert runtime.ensure_requests == []
    assert runtime.status(map_id=MAP_ID)["failure"] == {
        "reason": "runtime_map_binding_conflict",
        "retryable": False,
        "repair_required": True,
        "resource_disposition": "no_cleanup_without_verified_ownership",
    }


def test_owned_runtime_coordinate_repair_uses_preview_selection_and_audit(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    repository = tmp_path / "repository"
    repository.mkdir()
    tracker = RecoveryTracker()
    first = _application(storage_root, tracker, RecoverySessionRunner())
    project = first.configure_project(project_url=PROJECT_URL)
    first.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    context = CommissioningContext(
        project_id=PROJECT_ID,
        project_url=PROJECT_URL,
        repository="acme/atlas",
        repository_path=str(repository),
        pm_profile="pm",
        routing_policy="codex",
        herdr_executable="herdr-test",
        skills=("map-governance:pm", "delivery-pipeline", "herdr"),
        ceo_profile="ceo",
        pm_storage_root=str(tmp_path / "pm-profile" / "plugin-data"),
    )
    storage = PluginStorage(storage_root)
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
        state="repair_required",
        workspace_id="workspace-stale",
        window_id="window-stale",
        pane_id="pane-stale",
        agent_session_id="pm-session-stale",
        failure={
            "reason": "workspace_ownership_mismatch",
            "retryable": False,
            "repair_required": True,
        },
        updated_at="2026-08-24T01:01:00Z",
    )
    runtime = RecoveryCoordinator(storage_root)
    runtime.repair_candidates[MAP_ID] = {
        "before": {
            "workspace_id": "workspace-stale",
            "window_id": "window-stale",
            "pane_id": "pane-stale",
            "agent_session_id": "pm-session-stale",
            "state": "repair_required",
            "repair_reason": "workspace_ownership_mismatch",
        },
        "after": {
            "workspace_id": "workspace-owned",
            "window_id": "window-owned",
            "pane_id": "pane-owned",
            "agent_session_id": "pm-session-owned",
            "state": "pm_ready",
        },
        "evidence": {
            "session_namespace": CoordinatorRuntime.session_namespace(lifecycle),
            "workspace_label": CoordinatorRuntime.workspace_label(lifecycle, MAP_ID),
            "ownership_marker": CoordinatorRuntime.ownership_marker(lifecycle, MAP_ID),
            "exact_workspace_count": 1,
            "exact_pane_count": 1,
            "exact_agent_count": 1,
            "map_binding_verified": True,
        },
    }
    app = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        profile_name="ceo",
        clock=lambda: NOW,
        commissioning_prerequisites=RecoveryPrerequisites(context),
        coordinator_runtime=runtime,
    )

    plan = app.preview_repairs()

    assert len(plan["actions"]) == 1
    action = plan["actions"][0]
    assert action["kind"] == "pm_runtime.rebind_coordinates"
    assert action["safe"] is True
    assert action["before"] == runtime.repair_candidates[MAP_ID]["before"]
    assert action["after"] == runtime.repair_candidates[MAP_ID]["after"]

    applied = app.apply_repairs(
        plan=plan,
        selected_action_ids=[action["id"]],
        authorizer="basic:local-operator",
    )

    assert applied["applied_action_ids"] == [action["id"]]
    repaired = runtime.status(map_id=MAP_ID)
    assert repaired["state"] == "pm_ready"
    assert repaired["workspace_id"] == "workspace-owned"
    assert repaired["pane_id"] == "pane-owned"
    assert app.identity_repair_history(map_id=MAP_ID)[0]["resource_type"] == (
        "pm_runtime"
    )
    assert app.identity_repair_history(map_id=MAP_ID)[0]["authorizer"] == (
        "basic:local-operator"
    )
    assert runtime.ensure_requests == []


def test_leased_outbox_work_survives_restart_and_reclaims_once_after_expiry(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    tracker = RecoveryTracker()

    def crash_before_external_call(point, intent):
        if (
            point == "before_external_call"
            and intent.effect_type == "tracker.stage-transition"
        ):
            raise SimulatedRecoveryCrash()

    first = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        clock=lambda: NOW,
        outbox_crash_injector=crash_before_external_call,
    )
    project = first.configure_project(project_url=PROJECT_URL)
    first.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    with pytest.raises(SimulatedRecoveryCrash):
        first.transition_map(
            map_id=MAP_ID,
            expected_stage="authorized",
            requested_stage="parked",
            mutation_id="restart-leased-effect",
        )

    before_expiry = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        clock=lambda: NOW,
    ).recover_restart()

    assert before_expiry["outbox"]["processed_count"] == 0
    assert before_expiry["outbox"]["unfinished"] == [
        {
            "effect_id": "stage-transition:restart-leased-effect",
            "effect_type": "tracker.stage-transition",
            "state": "leased",
            "attempt_count": 1,
        }
    ]
    assert tracker.transition_calls == []

    after_expiry = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        clock=lambda: datetime(2026, 8, 24, 2, 1, tzinfo=timezone.utc),
    ).recover_restart()

    assert after_expiry["outbox"]["processed_count"] == 1
    assert after_expiry["outbox"]["unfinished"] == []
    assert tracker.transition_calls == [("authorized", "parked")]
    assert after_expiry["outbox"]["outcomes"][0]["attempt_number"] == 2


@pytest.mark.parametrize(
    (
        "restart_boundary",
        "restart_session_adapter",
        "rediscovered_coordinates",
    ),
    [
        ("desktop-only", True, None),
        ("backend-only", False, None),
        (
            "pm-process",
            False,
            {
                "workspace_id": "workspace-owned",
                "window_id": "window-owned",
                "pane_id": "pane-owned",
                "agent_session_id": "pm-session-restarted",
            },
        ),
        (
            "herdr-server",
            False,
            {
                "workspace_id": "workspace-rediscovered",
                "window_id": "window-rediscovered",
                "pane_id": "pane-rediscovered",
                "agent_session_id": "pm-session-rediscovered",
            },
        ),
        (
            "full-machine",
            True,
            {
                "workspace_id": "workspace-full-restart",
                "window_id": "window-full-restart",
                "pane_id": "pane-full-restart",
                "agent_session_id": "pm-session-full-restart",
            },
        ),
    ],
)
def test_restart_matrix_recovers_populated_durable_state_once(
    tmp_path,
    restart_boundary,
    restart_session_adapter,
    rediscovered_coordinates,
):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    repository = tmp_path / "repository"
    repository.mkdir()
    tracker = RecoveryTracker()
    sessions = RecoverySessionRunner()

    def crash_before_external_call(point, intent):
        if (
            point == "before_external_call"
            and intent.effect_type == "tracker.stage-transition"
        ):
            raise SimulatedRecoveryCrash()

    first = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        session_runner=sessions,
        profile_name="ceo",
        clock=lambda: NOW,
        outbox_crash_injector=crash_before_external_call,
    )
    project = first.configure_project(project_url=PROJECT_URL)
    first.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    canonical = first.open_map(map_id=MAP_ID)["ceo_session"]
    context = CommissioningContext(
        project_id=PROJECT_ID,
        project_url=PROJECT_URL,
        repository="acme/atlas",
        repository_path=str(repository),
        pm_profile="pm",
        routing_policy="codex",
        herdr_executable="herdr-test",
        skills=("map-governance:pm", "delivery-pipeline", "herdr"),
        ceo_profile="ceo",
        pm_storage_root=str(tmp_path / "pm-profile" / "plugin-data"),
    )
    storage = PluginStorage(storage_root)
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
    with pytest.raises(SimulatedRecoveryCrash):
        first.transition_map(
            map_id=MAP_ID,
            expected_stage="authorized",
            requested_stage="parked",
            mutation_id=f"matrix-{restart_boundary}",
        )

    restarted_sessions = sessions
    if restart_session_adapter:
        restarted_sessions = RecoverySessionRunner()
        restarted_sessions.sessions = dict(sessions.sessions)
        restarted_sessions.markers = set(sessions.markers)
        restarted_sessions.bootstrap_markers = set(sessions.bootstrap_markers)
    production_repair = restart_boundary in {"herdr-server", "full-machine"}
    herdr_runner = None
    if production_repair:
        assert rediscovered_coordinates is not None
        namespace = CoordinatorRuntime.session_namespace(lifecycle)
        label = CoordinatorRuntime.workspace_label(lifecycle, MAP_ID)
        agent_id = CoordinatorRuntime.agent_name(lifecycle, MAP_ID)
        preview_results = [
            _herdr_session_list(namespace),
            _herdr_workspace_list(
                label=label,
                workspace_id=rediscovered_coordinates["workspace_id"],
            ),
            _herdr_workspace(
                label=label,
                workspace_id=rediscovered_coordinates["workspace_id"],
                window_id=rediscovered_coordinates["window_id"],
            ),
            _herdr_pane(
                repository_path=context.repository_path,
                workspace_id=rediscovered_coordinates["workspace_id"],
                window_id=rediscovered_coordinates["window_id"],
                pane_id=rediscovered_coordinates["pane_id"],
            ),
            _herdr_agent(
                agent_id=agent_id,
                workspace_id=rediscovered_coordinates["workspace_id"],
                window_id=rediscovered_coordinates["window_id"],
                pane_id=rediscovered_coordinates["pane_id"],
                agent_session_id=rediscovered_coordinates["agent_session_id"],
            ),
        ]
        herdr_runner = ScriptedHerdrRecoveryRunner(
            [
                _herdr_session_list(),
                _herdr_session_list(namespace),
                _herdr_workspace_list(
                    label=label,
                    workspace_id=rediscovered_coordinates["workspace_id"],
                ),
                *preview_results,
                *preview_results,
            ]
        )
        runtime = CoordinatorRuntime(
            storage=storage,
            runner=herdr_runner,
            clock=lambda: "2026-08-24T02:01:00Z",
            startup_attempts=2,
            sleeper=lambda _seconds: None,
        )
    else:
        runtime = RecoveryCoordinator(storage_root)
        runtime.rediscovered_coordinates = rediscovered_coordinates
    restarted = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        session_runner=restarted_sessions,
        profile_name="ceo",
        clock=lambda: datetime(2026, 8, 24, 2, 1, tzinfo=timezone.utc),
        commissioning_prerequisites=RecoveryPrerequisites(context),
        coordinator_runtime=runtime,
    )

    recovered = restarted.recover_restart()

    assert recovered["state"] == (
        "repair_required" if production_repair else "recovered"
    ), restart_boundary
    assert restarted.board()["maps"][0]["stage"] == "parked"
    assert (
        recovered["ceo_sessions"][0]["root_session_id"]
        == (canonical["root_session_id"])
    )
    if production_repair:
        assert recovered["pm_runtimes"] == [
            {
                "map_id": MAP_ID,
                "state": "repair_required",
                "reason": "workspace_ownership_mismatch",
            }
        ]
        plan = restarted.preview_repairs()
        assert plan["blocked"] == []
        assert len(plan["actions"]) == 1
        applied = restarted.apply_repairs(
            plan=plan,
            selected_action_ids=[plan["actions"][0]["id"]],
            authorizer=f"test:{restart_boundary}",
        )
        assert applied["applied_action_ids"] == [plan["actions"][0]["id"]]
        assert herdr_runner is not None
        assert len(herdr_runner.starts) == 1
        assert not any(
            operation in call
            for call in herdr_runner.calls
            for operation in ("kill", "delete")
        )
    else:
        assert recovered["pm_runtimes"] == [
            {
                "map_id": MAP_ID,
                "state": "rediscovered",
                "runtime_state": "active",
            }
        ]
    assert recovered["outbox"]["unfinished"] == []
    assert tracker.transition_calls == [("authorized", "parked")]
    if isinstance(runtime, RecoveryCoordinator):
        assert len(runtime.ensure_requests) == 1
    expected_coordinates = rediscovered_coordinates or {
        "workspace_id": "workspace-owned",
        "window_id": "window-owned",
        "pane_id": "pane-owned",
        "agent_session_id": "pm-session-owned",
    }
    durable_runtime = runtime.status(map_id=MAP_ID)
    assert {
        name: durable_runtime[name]
        for name in (
            "workspace_id",
            "window_id",
            "pane_id",
            "agent_session_id",
        )
    } == expected_coordinates
    assert durable_runtime["state"] == ("pm_ready" if production_repair else "active")
    assert sessions.mint_count == 1
    assert restarted_sessions.mint_count == (0 if restart_session_adapter else 1)


def test_populated_schema_v15_migrates_to_repair_audit_without_identity_loss(
    tmp_path,
):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    tracker = RecoveryTracker()
    sessions = RecoverySessionRunner()
    first = _application(storage_root, tracker, sessions)
    project = first.configure_project(project_url=PROJECT_URL)
    first.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    canonical = first.open_map(map_id=MAP_ID)["ceo_session"]
    with sqlite3.connect(storage_root / "registry.db") as connection:
        connection.execute("DROP TABLE identity_repair_audit")
        connection.execute(
            "UPDATE plugin_metadata SET schema_version = 15 WHERE namespace = ?",
            ("map-governance",),
        )

    migrated = _application(storage_root, tracker, sessions)

    assert migrated.health()["status"] == "ready"
    assert migrated.identity_repair_history(map_id=MAP_ID) == []
    assert migrated.board()["maps"][0]["id"] == MAP_ID
    assert (
        migrated.open_map(map_id=MAP_ID)["ceo_session"]["root_session_id"]
        == (canonical["root_session_id"])
    )
    assert sessions.mint_count == 1
