from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

import pytest

from map_governance import (
    GovernanceRequestIdentity,
    MapGovernanceApplication,
    MapTransitionError,
)
from map_governance.approvals import ApprovalPacket, normalized_hash
from map_governance.coordinator import (
    CommissioningAuthorizationError,
    CommissioningContext,
    CommissioningPrerequisiteError,
    CoordinatorRuntimeError,
)
from map_governance.reports import PMReportDraft, TrackerPMReportRecord
from map_governance.storage import PluginStorage
from map_governance.tracker import TrackerError, TrackerIssue, TrackerProject


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MAP_ID = "I_atlas_41"
ISSUE_URL = "https://github.com/acme/atlas/issues/41"
PROJECT_ID = "PVT_acme_7"
PROJECT_URL = "https://github.com/orgs/acme/projects/7"
CEO = GovernanceRequestIdentity("ceo", "ceo-live")


class CommissionTracker:
    def __init__(self) -> None:
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
        self.reports: list[TrackerPMReportRecord] = []
        self.transition_calls = 0
        self.fail_transition = False
        self.fail_ready_read = False

    def get_project(self, url):
        return TrackerProject(
            id=PROJECT_ID,
            owner="acme",
            owner_type="organization",
            number=7,
            title="Acme portfolio",
            url=url,
        )

    def get_issue(self, url):
        assert url == ISSUE_URL
        return self.issue

    def list_decisions(self, url):
        return []

    def list_pm_reports(self, url):
        assert url == ISSUE_URL
        if self.fail_ready_read:
            raise TrackerError("secret=DO_NOT_LEAK tracker failed")
        return list(self.reports)

    def transition_issue_stage(self, url, *, expected_stage, requested_stage):
        self.transition_calls += 1
        if self.fail_transition:
            raise TrackerError("tracker unavailable secret=DO_NOT_LEAK")
        assert expected_stage == "authorized"
        assert requested_stage == "delivery"
        self.issue = replace(
            self.issue,
            labels=("map", "map-stage/delivery"),
        )
        return self.issue


class ReadyPrerequisites:
    def __init__(self, tmp_path: Path) -> None:
        repository = tmp_path / "repository"
        repository.mkdir()
        self.context = CommissioningContext(
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
        self.calls = 0
        self.error: Exception | None = None

    def commissioning_context(self, *, project_id, repository):
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert project_id == PROJECT_ID
        assert repository == "acme/atlas"
        return self.context


class ControllableRuntime:
    def __init__(self, tracker: CommissionTracker) -> None:
        self.tracker = tracker
        self.ensure_calls = 0
        self.prompt_calls = 0
        self.resume_calls = 0
        self.resume_markers = {}
        self.activate_calls = 0
        self.confirm_calls = 0
        self.error: Exception | None = None
        self.publish_ready = True
        self._lock = Lock()
        self.record = {
            "map_id": MAP_ID,
            "state": "not_commissioned",
            "session_namespace": "mapgov-owned",
            "workspace_id": "wA",
            "window_id": "wA:t1",
            "pane_id": "wA:p1",
            "agent_id": "mapgov_pm_owned",
            "agent_session_id": "pm-session-owned",
            "ownership_marker": "map-governance:map:owned",
            "lifecycle_id": "lifecycle-owned",
            "commissioned_at": "2026-08-24T00:00:00Z",
            "failure": None,
        }

    def ensure_root(self, request):
        with self._lock:
            self.ensure_calls += 1
        if self.error is not None:
            raise self.error
        self.record.update(state="pm_ready")
        return dict(self.record)

    def prompt_ready(self, *, map_id, payload):
        with self._lock:
            self.prompt_calls += 1
        assert map_id == MAP_ID
        checkpoint = payload["ready_checkpoint"]
        if self.publish_ready and not self.tracker.reports:
            self.tracker.reports.append(
                TrackerPMReportRecord(
                    report=PMReportDraft(
                        record_id=checkpoint["record_id"],
                        report_type=checkpoint["type"],
                        summary=checkpoint["summary"],
                        timestamp=checkpoint["timestamp"],
                    ).assign_to(MAP_ID),
                    tracker_record_id="IC_ready_1",
                    tracker_record_url=f"{ISSUE_URL}#issuecomment-ready-1",
                )
            )
        self.record.update(state="awaiting_ready")
        return dict(self.record)

    def readback(self, *, map_id, turn_id):
        return self.resume_markers.get((map_id, turn_id))

    def resume(
        self,
        *,
        map_id,
        profile_name,
        session_id,
        coordinator_id,
        turn_id,
        content="",
    ):
        self.resume_calls += 1
        self.resume_markers[(map_id, turn_id)] = {
            "map_id": map_id,
            "turn_id": turn_id,
            "content_hash": "sha256:" + hashlib.sha256(content.encode()).hexdigest(),
        }

    def confirm_ready(self, *, map_id, record_id):
        self.confirm_calls += 1
        self.record.update(state="ready_confirmed", ready_record_id=record_id)
        return dict(self.record)

    def activate(self, *, map_id, record_id):
        self.activate_calls += 1
        self.record.update(state="active", ready_record_id=record_id, failure=None)
        return dict(self.record)

    def record_failure(self, *, map_id, reason, retryable, repair_required=False):
        self.record.update(
            state=("repair_required" if repair_required else self.record["state"]),
            failure={
                "reason": reason,
                "retryable": retryable,
                "repair_required": repair_required,
            },
        )
        return dict(self.record)

    def status(self, *, map_id):
        return dict(self.record)


def _packet() -> ApprovalPacket:
    return ApprovalPacket(
        request_id="approval-delivery-001",
        decision_class="delivery_authorization",
        proposed_action="transition_map",
        alternatives=("Authorize", "Revise"),
        rationale="The Map is ready.",
        cost_risk="Bounded local delivery.",
        evidence=(ISSUE_URL,),
        requested_scope={"map_id": MAP_ID},
        decision_payload={
            "expected_stage": "awaiting-approval",
            "requested_stage": "authorized",
        },
    )


def _application(tmp_path, *, authorized=True, durable_resume=False):
    tracker = CommissionTracker()
    prerequisites = ReadyPrerequisites(tmp_path)
    runtime = ControllableRuntime(tracker)
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data",
        tracker=tracker,
        profile_name="ceo",
        clock=lambda: datetime(2026, 8, 24, tzinfo=timezone.utc),
        commissioning_prerequisites=prerequisites,
        coordinator_runtime=runtime,
        coordinator_resume=runtime if durable_resume else None,
    )
    project = application.configure_project(project_url=PROJECT_URL)
    application.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    application._storage.save_ceo_session_ready(
        map_id=MAP_ID,
        profile_name="ceo",
        canonical_identity="map:atlas",
        canonical_title="Map: Atlas",
        root_session_id="ceo-root",
        live_session_id="ceo-live",
        last_activity_at=None,
        bootstrap_hash="sha256:bootstrap",
        updated_at="2026-08-24T00:00:00Z",
    )
    if authorized:
        packet = _packet()
        application._storage.save_approval_request(
            map_id=MAP_ID,
            packet=packet.payload(),
            packet_hash=packet.packet_hash,
            requested_by_profile="ceo",
            requested_by_session="ceo-live",
            requested_at="2026-08-23T22:00:00Z",
            tracker_record_id="IC_request",
            tracker_record_url=f"{ISSUE_URL}#issuecomment-request",
        )
        application._storage.apply_approval_decision(
            request_id=packet.request_id,
            decision="approved",
            actor_id="chairman-1",
            actor_profile="ceo",
            note="Approved.",
            decided_at="2026-08-23T23:00:00Z",
            expires_at="2026-08-25T00:00:00Z",
            tracker_record_id="IC_decision",
            tracker_record_url=f"{ISSUE_URL}#issuecomment-decision",
        )
        scope = {"map_id": MAP_ID}
        payload = packet.decision_payload
        application._storage.reserve_protected_mutation(
            mutation_id="authorize-map-001",
            map_id=MAP_ID,
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
            mutation_id="authorize-map-001",
            confirmed_at="2026-08-23T23:31:00Z",
        )
    return application, tracker, prerequisites, runtime


def test_commissioning_uses_one_structured_prompt_and_returns_pm_idle(tmp_path):
    application, tracker, _, runtime = _application(tmp_path, durable_resume=True)

    result = application.commission_map(map_id=MAP_ID, request_identity=CEO)

    assert result["state"] == "active"
    assert tracker.transition_calls == 1
    assert runtime.prompt_calls == 1
    assert runtime.resume_calls == 0
    assignment = application._storage.pm_assignment(MAP_ID)
    assert assignment["state"] == "idle"
    assert assignment["last_turn_id"] == f"commission-ready:{MAP_ID}"
    assert assignment["last_outcome"] == "report"
    assert assignment["last_outcome_id"] == "commission-ready-I_atlas_41"


def test_commission_fails_closed_before_runtime_when_authorization_is_missing(tmp_path):
    application, tracker, prerequisites, runtime = _application(
        tmp_path, authorized=False
    )

    with pytest.raises(CommissioningAuthorizationError) as raised:
        application.commission_map(map_id=MAP_ID, request_identity=CEO)

    assert raised.value.reason == "delivery_authorization_missing"
    assert prerequisites.calls == 0
    assert runtime.ensure_calls == 0
    assert tracker.transition_calls == 0


def test_prerequisite_failure_produces_no_herdr_mutation(tmp_path):
    application, tracker, prerequisites, runtime = _application(tmp_path)
    prerequisites.error = CommissioningPrerequisiteError(
        reason="doctor_failed", failed_checks=("herdr.binary",)
    )

    with pytest.raises(CommissioningPrerequisiteError):
        application.commission_map(map_id=MAP_ID, request_identity=CEO)

    assert runtime.ensure_calls == 0
    assert tracker.transition_calls == 0


def test_ready_checkpoint_must_be_tracker_confirmed_before_delivery(tmp_path):
    application, tracker, _, runtime = _application(tmp_path)
    runtime.publish_ready = False

    result = application.commission_map(map_id=MAP_ID, request_identity=CEO)

    assert result["state"] == "awaiting_ready"
    assert result["checkpoint"] == {
        "state": "tracker_unconfirmed",
        "record_id": "commission-ready-I_atlas_41",
    }
    assert tracker.transition_calls == 0
    assert application.map_detail(map_id=MAP_ID)["stage"] == "authorized"


def test_generic_transition_cannot_bypass_commissioning_readiness(tmp_path):
    application, tracker, _, runtime = _application(tmp_path)
    runtime.record.update(
        state="ready_confirmed",
        ready_record_id="commission-ready-I_atlas_41",
        failure=None,
    )

    with pytest.raises(MapTransitionError, match="ready checkpoint"):
        application.transition_map(
            map_id=MAP_ID,
            expected_stage="authorized",
            requested_stage="delivery",
        )

    assert tracker.transition_calls == 0


def test_tracker_ready_read_failure_is_safe_and_does_not_reprompt_or_deliver(tmp_path):
    application, tracker, _, runtime = _application(tmp_path)
    tracker.fail_ready_read = True

    result = application.commission_map(map_id=MAP_ID, request_identity=CEO)

    assert result["state"] == "pm_ready"
    assert result["checkpoint"]["state"] == "tracker_confirmation_unavailable"
    assert result["failure"]["reason"] == "tracker_ready_confirmation_unavailable"
    assert "DO_NOT_LEAK" not in str(result)
    assert runtime.prompt_calls == 0
    assert tracker.transition_calls == 0


def test_success_assigns_one_pm_and_transitions_only_after_tracker_readback(tmp_path):
    application, tracker, prerequisites, runtime = _application(tmp_path)

    result = application.commission_map(map_id=MAP_ID, request_identity=CEO)

    assert result["state"] == "active"
    assert result["checkpoint"]["state"] == "tracker_confirmed"
    assert tracker.transition_calls == 1
    assert runtime.confirm_calls == 1
    assert runtime.activate_calls == 1
    assert application.map_detail(map_id=MAP_ID)["stage"] == "delivery"
    assignment = application._storage.pm_assignment(MAP_ID)
    assert assignment["profile_name"] == "pm"
    assert assignment["session_id"] == "pm-session-owned"
    assert assignment["coordinator_id"] == "lifecycle-owned"
    handoff = PluginStorage(
        Path(prerequisites.context.pm_storage_root)
    ).pm_control_plane_for_request(
        profile_name="pm",
        session_id="pm-session-owned",
    )
    assert handoff == {
        "profile_name": "pm",
        "session_id": "pm-session-owned",
        "map_id": MAP_ID,
        "control_profile": "ceo",
        "coordinator_id": "lifecycle-owned",
        "registered_at": "2026-08-24T00:00:00Z",
    }


def test_post_start_pm_assignment_conflict_records_repair_without_delivery(tmp_path):
    application, tracker, _, runtime = _application(tmp_path)
    application._storage.save_pm_assignment(
        map_id=MAP_ID,
        profile_name="pm",
        session_id="different-pm-session",
        coordinator_id="different-lifecycle",
        assigned_at="2026-08-24T00:00:00Z",
    )

    result = application.commission_map(map_id=MAP_ID, request_identity=CEO)

    assert runtime.ensure_calls == 1
    assert result["state"] == "repair_required"
    assert result["checkpoint"] == {"state": "runtime_verification_failed"}
    assert result["failure"] == {
        "reason": "pm_profile_handoff_unavailable",
        "retryable": False,
        "repair_required": True,
    }
    assert runtime.prompt_calls == 0
    assert tracker.transition_calls == 0
    assert application.map_detail(map_id=MAP_ID)["stage"] == "authorized"


def test_post_start_ready_turn_conflict_records_repair_without_delivery(tmp_path):
    application, tracker, _, runtime = _application(tmp_path)
    application._storage.save_pm_assignment(
        map_id=MAP_ID,
        profile_name="pm",
        session_id="pm-session-owned",
        coordinator_id="lifecycle-owned",
        assigned_at="2026-08-24T00:00:00Z",
    )
    application._storage.begin_pm_turn(
        map_id=MAP_ID,
        coordinator_id="lifecycle-owned",
        turn_id="unrelated-active-turn",
        started_at="2026-08-24T00:00:00Z",
    )

    result = application.commission_map(map_id=MAP_ID, request_identity=CEO)

    assert runtime.ensure_calls == 1
    assert result["state"] == "repair_required"
    assert result["checkpoint"] == {"state": "runtime_verification_failed"}
    assert result["failure"] == {
        "reason": "pm_ready_turn_conflict",
        "retryable": False,
        "repair_required": True,
    }
    assert runtime.prompt_calls == 0
    assert tracker.transition_calls == 0
    assert application.map_detail(map_id=MAP_ID)["stage"] == "authorized"


def test_tracker_transition_failure_keeps_ready_runtime_retryable_and_not_active(
    tmp_path,
):
    application, tracker, _, runtime = _application(tmp_path)
    tracker.fail_transition = True

    result = application.commission_map(map_id=MAP_ID, request_identity=CEO)

    assert result["state"] == "ready_confirmed"
    assert result["failure"] == {
        "reason": "tracker_delivery_transition_pending",
        "retryable": True,
        "repair_required": False,
    }
    assert runtime.activate_calls == 0
    assert application.map_detail(map_id=MAP_ID)["stage"] == "authorized"
    assert "DO_NOT_LEAK" not in str(result)


def test_duplicate_and_concurrent_commission_resume_one_root_runtime(tmp_path):
    application, tracker, _, runtime = _application(tmp_path)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(
                lambda _: application.commission_map(
                    map_id=MAP_ID,
                    request_identity=CEO,
                ),
                range(6),
            )
        )

    assert {result["state"] for result in results} == {"active"}
    assert runtime.ensure_calls == 6
    assert runtime.prompt_calls == 1
    assert tracker.transition_calls == 1
    replay = application.commission_map(map_id=MAP_ID, request_identity=CEO)
    assert replay["state"] == "active"
    assert replay["idempotent"] is True
    assert runtime.ensure_calls == 7


@pytest.mark.parametrize("interrupted_state", ["ready_confirmed", "workspace_ready"])
def test_delivery_restart_revalidates_and_activates_ready_runtime(
    tmp_path, interrupted_state
):
    application, _, _, runtime = _application(tmp_path)
    application.commission_map(map_id=MAP_ID, request_identity=CEO)
    runtime.record["state"] = interrupted_state
    runtime.activate_calls = 0

    resumed = application.commission_map(map_id=MAP_ID, request_identity=CEO)

    assert resumed["state"] == "active"
    assert resumed["idempotent"] is True
    assert runtime.activate_calls == 1


def test_runtime_status_never_exposes_path_kind_agent_session(tmp_path):
    application, _, _, runtime = _application(tmp_path)
    runtime.record["agent_session_id"] = "/Users/person/.hermes/sessions/private"

    status = application.runtime_status(map_id=MAP_ID, request_identity=CEO)

    assert "agent_session_id" not in status
    assert "/Users/person" not in str(status)


def test_runtime_collision_is_returned_as_repair_evidence_without_tracker_write(
    tmp_path,
):
    application, tracker, _, runtime = _application(tmp_path)
    runtime.error = CoordinatorRuntimeError(
        reason="session_namespace_collision",
        retryable=False,
        repair_required=True,
    )

    with pytest.raises(CoordinatorRuntimeError):
        application.commission_map(map_id=MAP_ID, request_identity=CEO)

    assert tracker.transition_calls == 0
    assert runtime.prompt_calls == 0
