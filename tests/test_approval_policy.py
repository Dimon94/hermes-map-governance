from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Lock

import pytest

from map_governance import (
    ApprovalEnforcementError,
    ApprovalPacket,
    ApprovalRequestConflict,
    AuthorityEnvelopePolicy,
    GovernanceActorIdentity,
    GovernanceAuthorizationError,
    GovernanceRequestIdentity,
    MapGovernanceApplication,
    TrackerApprovalConfirmationError,
)
from map_governance.approvals import ApprovalHistoryEvent
from map_governance.sessions import CanonicalSession
from map_governance.outbox import OutboxRepository
from map_governance.tracker import (
    StructuredDecision,
    TrackerApprovalRecord,
    TrackerConflictError,
    TrackerDecisionRecord,
    TrackerIssue,
    TrackerProject,
)


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MAP_ID = "I_atlas_41"
OTHER_MAP_ID = "I_atlas_42"
PROFILE = "ceo"
ROOT_SESSION_ID = "mapgov-root"
LIVE_SESSION_ID = "mapgov-live"
PROJECT_URL = "https://github.com/orgs/acme/projects/7"
ISSUE_URL = "https://github.com/acme/atlas/issues/41"
OTHER_ISSUE_URL = "https://github.com/acme/atlas/issues/42"


class ApprovalTracker:
    def __init__(self) -> None:
        self.projects = {
            PROJECT_URL: TrackerProject(
                id="PVT_acme_7",
                owner="acme",
                owner_type="organization",
                number=7,
                title="Acme CEO portfolio",
                url=PROJECT_URL,
            )
        }
        self.issues = {
            ISSUE_URL: TrackerIssue(
                id=MAP_ID,
                repository="acme/atlas",
                number=41,
                title="Map the Atlas launch",
                url=ISSUE_URL,
                state="open",
                state_reason=None,
                labels=("map", "map-stage/awaiting-approval"),
            ),
            OTHER_ISSUE_URL: TrackerIssue(
                id=OTHER_MAP_ID,
                repository="acme/atlas",
                number=42,
                title="Map the Atlas billing model",
                url=OTHER_ISSUE_URL,
                state="open",
                state_reason=None,
                labels=("map", "map-stage/awaiting-approval"),
            ),
        }
        self.approval_records: dict[str, list[TrackerApprovalRecord]] = {
            ISSUE_URL: [],
            OTHER_ISSUE_URL: [],
        }
        self.approval_writes = 0
        self.decision_records: dict[str, list[TrackerDecisionRecord]] = {
            ISSUE_URL: [],
            OTHER_ISSUE_URL: [],
        }
        self.decision_writes = 0
        self.transition_calls = 0
        self.confirm_approval_writes = True
        self.before_approval_append = None
        self.transition_error: Exception | None = None
        self._lock = Lock()

    def get_project(self, url: str) -> TrackerProject:
        return self.projects[url]

    def get_issue(self, url: str) -> TrackerIssue:
        return self.issues[url]

    def list_decisions(self, url: str):
        return list(self.decision_records[url])

    def list_pm_reports(self, url: str):
        return []

    def append_decision(self, url: str, *, issue_id: str, decision):
        self.decision_writes += 1
        record = TrackerDecisionRecord(
            decision=decision,
            tracker_record_id=f"IC_decision_{self.decision_writes}",
            tracker_record_url=f"{url}#issuecomment-decision-{self.decision_writes}",
        )
        self.decision_records[url].append(record)
        return record

    def append_approval_event(
        self,
        url: str,
        *,
        issue_id: str,
        event: ApprovalHistoryEvent,
    ) -> TrackerApprovalRecord:
        if self.before_approval_append is not None:
            self.before_approval_append(event)
        with self._lock:
            self.approval_writes += 1
            record = TrackerApprovalRecord(
                event=event,
                tracker_record_id=f"IC_approval_{self.approval_writes}",
                tracker_record_url=f"{url}#issuecomment-approval-{self.approval_writes}",
            )
            if self.confirm_approval_writes:
                self.approval_records[url].append(record)
            return record

    def list_approval_events(self, url: str) -> list[TrackerApprovalRecord]:
        with self._lock:
            return list(self.approval_records[url])

    def transition_issue_stage(
        self,
        url: str,
        *,
        expected_stage: str,
        requested_stage: str,
    ) -> TrackerIssue:
        with self._lock:
            self.transition_calls += 1
            if self.transition_error is not None:
                raise self.transition_error
            issue = self.issues[url]
            current = next(
                label.removeprefix("map-stage/")
                for label in issue.labels
                if label.startswith("map-stage/")
            )
            if current != expected_stage:
                raise TrackerConflictError(
                    current_stage=current,
                    requested_stage=requested_stage,
                )
            committed = replace(
                issue,
                labels=tuple(
                    label
                    for label in issue.labels
                    if not label.startswith("map-stage/")
                )
                + (f"map-stage/{requested_stage}",),
            )
            self.issues[url] = committed
            return committed


class SessionRunner:
    def __init__(self) -> None:
        self.session = None
        self.resume_markers = set()

    def find_exact(self, *, title: str):
        return (
            [self.session]
            if self.session is not None and self.session.title == title
            else []
        )

    def mint(self, *, title: str, **_kwargs):
        self.session = CanonicalSession(
            root_session_id=ROOT_SESSION_ID,
            live_session_id=LIVE_SESSION_ID,
            title=title,
            last_activity_at="2026-08-23T09:00:00Z",
            bootstrap_sent=True,
        )
        return self.session

    def initialize(self, session, **_kwargs):
        return session

    def resolve(self, *, root_session_id: str):
        if root_session_id != ROOT_SESSION_ID or self.session is None:
            return None
        return self.session

    def has_resume_marker(self, *, root_session_id, idempotency_key):
        return (root_session_id, idempotency_key) in self.resume_markers

    def resume_once(self, *, root_session_id, idempotency_key, **_kwargs):
        self.resume_markers.add((root_session_id, idempotency_key))
        return self.resolve(root_session_id=root_session_id)

    def load_skill(self, session, **_kwargs):
        return session


class SimulatedApprovalProcessCrash(BaseException):
    pass


def _clock_box():
    return [datetime(2026, 8, 23, 9, 30, tzinfo=timezone.utc)]


def _application(tmp_path, tracker=None, *, clock=None, policy=None):
    tracker = tracker or ApprovalTracker()
    policy = policy or AuthorityEnvelopePolicy.from_settings(
        {"chairman_actor_ids": ["chairman-1"]}
    )
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
        tracker=tracker,
        session_runner=SessionRunner(),
        profile_name=PROFILE,
        clock=(lambda: clock[0]) if clock is not None else None,
        authority_policy=policy,
    )
    project = application.configure_project(project_url=PROJECT_URL)
    application.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    application.open_map(map_id=MAP_ID)
    return application, tracker


def _ceo_identity():
    return GovernanceRequestIdentity(PROFILE, LIVE_SESSION_ID)


def _chairman_identity(*, profile=PROFILE, actor_id="chairman-1"):
    return GovernanceActorIdentity(
        role="chairman",
        profile_name=profile,
        actor_id=actor_id,
        session_id="dashboard-request",
    )


def _packet(*, request_id="approval-delivery-001", map_id=MAP_ID, **changes):
    values = {
        "request_id": request_id,
        "decision_class": "delivery_authorization",
        "proposed_action": "transition_map",
        "alternatives": (
            "Authorize delivery now",
            "Return to discovery for more evidence",
        ),
        "rationale": "The validated scope is ready for funded delivery.",
        "cost_risk": "Two engineering weeks; reversible before publication.",
        "evidence": ("https://github.com/acme/atlas/issues/41#issuecomment-7",),
        "requested_scope": {"map_id": map_id},
        "decision_payload": {
            "expected_stage": "awaiting-approval",
            "requested_stage": "authorized",
        },
    }
    values.update(changes)
    return ApprovalPacket(**values)


def _request_and_approve(application, *, packet=None):
    packet = packet or _packet()
    requested = application.request_approval(
        map_id=MAP_ID,
        request_identity=_ceo_identity(),
        packet=packet,
    )
    approved = application.decide_approval(
        map_id=MAP_ID,
        request_id=packet.request_id,
        actor_identity=_chairman_identity(),
        decision="approved",
        note="Approved for the declared delivery scope.",
    )
    return requested, approved


def test_authority_policy_defaults_and_settings_classify_decisions():
    defaults = AuthorityEnvelopePolicy.from_settings(None)

    assert defaults.classify("product") == "ceo"
    assert defaults.classify("operational") == "ceo"
    assert defaults.classify("delivery_authorization") == "chairman"
    assert defaults.classify("remote_publication") == "chairman"
    assert defaults.classify("invented_authority") == "unconfigured"

    configured = AuthorityEnvelopePolicy.from_settings(
        {
            "ceo_autonomous_decision_classes": ["product", "schedule_change"],
            "chairman_required_decision_classes": [
                "delivery_authorization",
                "operational",
            ],
            "approval_ttl_seconds": 3600,
        }
    )
    assert configured.classify("schedule_change") == "ceo"
    assert configured.classify("operational") == "chairman"
    assert configured.approval_ttl == timedelta(hours=1)

    with pytest.raises(ValueError, match="both CEO and chairman"):
        AuthorityEnvelopePolicy.from_settings(
            {
                "ceo_autonomous_decision_classes": ["product"],
                "chairman_required_decision_classes": ["product"],
            }
        )


def test_authority_thresholds_are_behavioral_and_fail_closed():
    policy = AuthorityEnvelopePolicy.from_settings(
        {
            "ceo_autonomous_decision_classes": ["product", "operational"],
            "chairman_required_decision_classes": [
                "budget_increase",
                "schedule_change",
                "scope_expansion",
            ],
            "authority_thresholds": {
                "budget_increase": {
                    "field": "amount",
                    "maximum": 1_000,
                },
                "schedule_change": {
                    "field": "days",
                    "maximum": 5,
                },
                "scope_expansion": {
                    "source": "requested_scope",
                    "field": "area",
                    "allowed_values": ["existing-map"],
                },
            },
        }
    )

    assert (
        policy.classify("budget_increase", decision_payload={"amount": 1_000}) == "ceo"
    )
    assert (
        policy.classify("budget_increase", decision_payload={"amount": 1_001})
        == "chairman"
    )
    assert policy.classify("schedule_change", decision_payload={}) == "chairman"
    assert (
        policy.classify(
            "scope_expansion",
            requested_scope={"area": "existing-map"},
        )
        == "ceo"
    )
    assert (
        policy.classify(
            "scope_expansion",
            requested_scope={"area": "new-product"},
        )
        == "chairman"
    )


def test_approval_request_inside_configured_threshold_is_rejected_as_ceo_authority(
    tmp_path,
):
    policy = AuthorityEnvelopePolicy.from_settings(
        {
            "chairman_required_decision_classes": ["budget_increase"],
            "authority_thresholds": {
                "budget_increase": {"field": "amount", "maximum": 1_000}
            },
            "chairman_actor_ids": ["chairman-1"],
        }
    )
    application, tracker = _application(tmp_path, policy=policy)

    with pytest.raises(ApprovalEnforcementError) as raised:
        application.request_approval(
            map_id=MAP_ID,
            request_identity=_ceo_identity(),
            packet=_packet(
                decision_class="budget_increase",
                decision_payload={"amount": 500},
            ),
        )

    assert raised.value.reason == "decision_within_ceo_authority"
    assert tracker.approval_writes == 0


def test_threshold_authorized_ceo_decision_records_context_in_tracker_audit(tmp_path):
    policy = AuthorityEnvelopePolicy.from_settings(
        {
            "chairman_required_decision_classes": ["budget_increase"],
            "authority_thresholds": {
                "budget_increase": {"field": "amount", "maximum": 1_000}
            },
            "chairman_actor_ids": ["chairman-1"],
        }
    )
    application, tracker = _application(tmp_path, policy=policy)
    within_envelope = StructuredDecision(
        decision_id="budget-001",
        type="budget_increase",
        rationale="Fund the validated research cohort.",
        authority="ceo",
        affected_stage="awaiting-approval",
        timestamp="2026-08-23T09:30:00Z",
        authority_context={
            "decision_payload": {"amount": 750, "currency": "USD"},
            "requested_scope": {"map_id": MAP_ID},
        },
    )

    result = application.record_decision(
        map_id=MAP_ID,
        request_identity=_ceo_identity(),
        decision=within_envelope,
    )

    assert result["decision"]["authority_context"] == (
        within_envelope.authority_context
    )
    assert tracker.decision_records[ISSUE_URL][0].decision == within_envelope
    assert tracker.decision_writes == 1

    over_ceiling = replace(
        within_envelope,
        decision_id="budget-002",
        authority_context={"decision_payload": {"amount": 1_001}},
    )
    with pytest.raises(GovernanceAuthorizationError) as raised:
        application.record_decision(
            map_id=MAP_ID,
            request_identity=_ceo_identity(),
            decision=over_ceiling,
        )
    assert raised.value.reason == "chairman_approval_required"
    assert tracker.decision_writes == 1


def test_ceo_submits_complete_packet_with_normalized_payload_hash_and_tracker_confirmation(
    tmp_path,
):
    application, tracker = _application(tmp_path)

    first = application.request_approval(
        map_id=MAP_ID,
        request_identity=_ceo_identity(),
        packet=_packet(),
    )
    replay = application.request_approval(
        map_id=MAP_ID,
        request_identity=_ceo_identity(),
        packet=_packet(
            requested_scope={"map_id": MAP_ID},
            decision_payload={
                "requested_stage": "authorized",
                "expected_stage": "awaiting-approval",
            },
        ),
    )

    item = first["approval"]
    assert item["request_id"] == "approval-delivery-001"
    assert item["status"] == "pending"
    assert item["payload_hash"].startswith("sha256:")
    assert item["tracker"]["request"]["id"] == "IC_approval_1"
    assert replay == {**first, "idempotent": True}
    assert tracker.approval_writes == 1
    assert application.map_detail(map_id=MAP_ID)["approvals"] == {
        "count": 1,
        "items": [item],
    }


def test_each_approval_event_is_durable_before_tracker_append(tmp_path):
    application, tracker = _application(tmp_path)
    observed = []

    def observe(event):
        effect_id = f"tracker-approval:{MAP_ID}:{event.event_id}"
        observed.append(
            (event.event_type, application.outbox_status(effect_id=effect_id)["state"])
        )

    tracker.before_approval_append = observe
    application.request_approval(
        map_id=MAP_ID,
        request_identity=_ceo_identity(),
        packet=_packet(),
    )
    application.decide_approval(
        map_id=MAP_ID,
        request_id="approval-delivery-001",
        actor_identity=_chairman_identity(),
        decision="approved",
        note="Approve after durable transport intent.",
    )

    assert observed == [("requested", "leased"), ("approved", "leased")]
    statuses = [
        application.outbox_status(
            effect_id=f"tracker-approval:{MAP_ID}:approval:approval-delivery-001:{suffix}"
        )
        for suffix in ("request", "decision")
    ]
    assert [status["state"] for status in statuses] == ["succeeded", "succeeded"]
    assert [status["effect_type"] for status in statuses] == [
        "tracker.approval-event",
        "tracker.approval-event",
    ]


def test_request_id_cannot_be_reused_for_changed_payload_or_another_map(tmp_path):
    application, tracker = _application(tmp_path)
    application.request_approval(
        map_id=MAP_ID,
        request_identity=_ceo_identity(),
        packet=_packet(),
    )

    with pytest.raises(ApprovalRequestConflict):
        application.request_approval(
            map_id=MAP_ID,
            request_identity=_ceo_identity(),
            packet=_packet(
                decision_payload={
                    "expected_stage": "awaiting-approval",
                    "requested_stage": "parked",
                }
            ),
        )

    project = application.configure_project(project_url=PROJECT_URL)
    application.bind_map(project_id=project["id"], issue_url=OTHER_ISSUE_URL)
    with pytest.raises(GovernanceAuthorizationError) as raised:
        application.request_approval(
            map_id=OTHER_MAP_ID,
            request_identity=_ceo_identity(),
            packet=_packet(map_id=OTHER_MAP_ID),
        )
    assert raised.value.reason == "canonical_session_missing"
    assert tracker.approval_writes == 1


def test_within_envelope_decision_does_not_create_chairman_request(tmp_path):
    application, tracker = _application(tmp_path)

    with pytest.raises(ApprovalEnforcementError) as raised:
        application.request_approval(
            map_id=MAP_ID,
            request_identity=_ceo_identity(),
            packet=_packet(decision_class="product"),
        )

    assert raised.value.reason == "decision_within_ceo_authority"
    assert tracker.approval_writes == 0
    assert application.map_detail(map_id=MAP_ID)["approvals"]["count"] == 0


def test_ceo_cannot_record_a_chairman_required_class_as_self_approved(tmp_path):
    application, tracker = _application(tmp_path)

    with pytest.raises(GovernanceAuthorizationError) as raised:
        application.record_decision(
            map_id=MAP_ID,
            request_identity=_ceo_identity(),
            decision=StructuredDecision(
                decision_id="decision-publication-001",
                type="remote_publication",
                rationale="A model assertion cannot grant publication authority.",
                authority="ceo",
                affected_stage="acceptance",
                timestamp="2026-08-23T09:25:00Z",
            ),
        )

    assert raised.value.reason == "chairman_approval_required"
    assert tracker.approval_writes == 0
    assert application.authorization_denials(map_id=MAP_ID)[-1]["reason"] == (
        "chairman_approval_required"
    )


@pytest.mark.parametrize("decision", ["approved", "rejected", "revision"])
def test_chairman_explicit_decision_is_confirmed_before_ledger_projection(
    tmp_path,
    decision,
):
    clock = _clock_box()
    application, tracker = _application(tmp_path, clock=clock)
    application.request_approval(
        map_id=MAP_ID,
        request_identity=_ceo_identity(),
        packet=_packet(),
    )

    result = application.decide_approval(
        map_id=MAP_ID,
        request_id="approval-delivery-001",
        actor_identity=_chairman_identity(),
        decision=decision,
        note=f"Chairman chose {decision}.",
    )

    approval = result["approval"]
    assert approval["status"] == decision
    assert approval["decision"]["actor_id"] == "chairman-1"
    assert approval["tracker"]["decision"]["id"] == "IC_approval_2"
    if decision == "approved":
        assert approval["expires_at"] == "2026-08-24T09:30:00Z"
    else:
        assert approval["expires_at"] is None
    assert tracker.approval_writes == 2
    assert [event["event_type"] for event in approval["history"]] == [
        "requested",
        decision,
    ]
    assert any(
        event["type"] == "approval.upserted"
        and event["payload"]["approval"]["status"] == decision
        for event in application.board_events(cursor=0, limit=100)["events"]
    )

    replay = application.decide_approval(
        map_id=MAP_ID,
        request_id="approval-delivery-001",
        actor_identity=_chairman_identity(),
        decision=decision,
        note=f"Chairman chose {decision}.",
    )
    assert replay == {**result, "idempotent": True}
    assert tracker.approval_writes == 2


def test_tracker_failure_or_missing_readback_never_projects_chairman_decision(
    tmp_path,
):
    application, tracker = _application(tmp_path)
    application.request_approval(
        map_id=MAP_ID,
        request_identity=_ceo_identity(),
        packet=_packet(),
    )
    tracker.confirm_approval_writes = False

    with pytest.raises(TrackerApprovalConfirmationError):
        application.decide_approval(
            map_id=MAP_ID,
            request_id="approval-delivery-001",
            actor_identity=_chairman_identity(),
            decision="approved",
            note="This write is not confirmed.",
        )

    approval = application.map_detail(map_id=MAP_ID)["approvals"]["items"][0]
    assert approval["status"] == "pending"
    assert approval["tracker"]["decision"] is None


def test_unconfirmed_request_is_not_inserted_into_the_enforcement_ledger(tmp_path):
    tracker = ApprovalTracker()
    tracker.confirm_approval_writes = False
    application, _ = _application(tmp_path, tracker)

    with pytest.raises(TrackerApprovalConfirmationError):
        application.request_approval(
            map_id=MAP_ID,
            request_identity=_ceo_identity(),
            packet=_packet(),
        )

    assert application.map_detail(map_id=MAP_ID)["approvals"] == {
        "count": 0,
        "items": [],
    }


def test_restart_adopts_tracker_confirmed_request_after_crash_before_ledger_write(
    tmp_path,
):
    tracker = ApprovalTracker()
    packet = _packet()
    event = ApprovalHistoryEvent(
        event_id="approval:approval-delivery-001:request",
        request_id=packet.request_id,
        event_type="requested",
        occurred_at="2026-08-23T08:00:00Z",
        payload_hash=packet.payload_hash,
        details={**packet.payload(), "packet_hash": packet.packet_hash},
    )
    tracker.approval_records[ISSUE_URL].append(
        TrackerApprovalRecord(
            event=event,
            tracker_record_id="IC_crash_request",
            tracker_record_url=f"{ISSUE_URL}#issuecomment-crash-request",
        )
    )
    application, _ = _application(tmp_path, tracker)

    recovered = application.request_approval(
        map_id=MAP_ID,
        request_identity=_ceo_identity(),
        packet=packet,
    )

    assert recovered["idempotent"] is True
    assert recovered["approval"]["requested_at"] == "2026-08-23T08:00:00Z"
    assert recovered["approval"]["tracker"]["request"]["id"] == ("IC_crash_request")
    assert tracker.approval_writes == 0


def test_restart_adopts_tracker_confirmed_decision_after_crash_before_ledger_update(
    tmp_path,
):
    clock = _clock_box()
    application, tracker = _application(tmp_path, clock=clock)
    application.request_approval(
        map_id=MAP_ID,
        request_identity=_ceo_identity(),
        packet=_packet(),
    )
    tracker.approval_records[ISSUE_URL].append(
        TrackerApprovalRecord(
            event=ApprovalHistoryEvent(
                event_id="approval:approval-delivery-001:decision",
                request_id="approval-delivery-001",
                event_type="approved",
                occurred_at="2026-08-23T09:15:00Z",
                payload_hash=_packet().payload_hash,
                details={
                    "decision": "approved",
                    "actor_id": "chairman-1",
                    "actor_profile": PROFILE,
                    "note": "Approved before the simulated crash.",
                    "expires_at": "2026-08-24T09:15:00Z",
                },
            ),
            tracker_record_id="IC_crash_decision",
            tracker_record_url=f"{ISSUE_URL}#issuecomment-crash-decision",
        )
    )
    clock[0] += timedelta(hours=1)

    recovered = application.decide_approval(
        map_id=MAP_ID,
        request_id="approval-delivery-001",
        actor_identity=_chairman_identity(),
        decision="approved",
        note="Approved before the simulated crash.",
    )

    assert recovered["approval"]["decision"]["decided_at"] == ("2026-08-23T09:15:00Z")
    assert recovered["approval"]["expires_at"] == "2026-08-24T09:15:00Z"
    assert recovered["approval"]["tracker"]["decision"]["id"] == ("IC_crash_decision")
    assert tracker.approval_writes == 1


def test_bind_rebuilds_approval_ledger_from_authoritative_issue_history(tmp_path):
    tracker = ApprovalTracker()
    packet = _packet()
    tracker.approval_records[ISSUE_URL].extend(
        [
            TrackerApprovalRecord(
                event=ApprovalHistoryEvent(
                    event_id=f"approval:{packet.request_id}:request",
                    request_id=packet.request_id,
                    event_type="requested",
                    occurred_at="2026-08-23T08:00:00Z",
                    payload_hash=packet.payload_hash,
                    details={**packet.payload(), "packet_hash": packet.packet_hash},
                ),
                tracker_record_id="IC_rebuild_request",
                tracker_record_url=f"{ISSUE_URL}#issuecomment-rebuild-request",
            ),
            TrackerApprovalRecord(
                event=ApprovalHistoryEvent(
                    event_id=f"approval:{packet.request_id}:decision",
                    request_id=packet.request_id,
                    event_type="approved",
                    occurred_at="2026-08-23T08:30:00Z",
                    payload_hash=packet.payload_hash,
                    details={
                        "decision": "approved",
                        "actor_id": "chairman-1",
                        "actor_profile": PROFILE,
                        "note": "Authoritative approval.",
                        "expires_at": "2026-08-24T08:30:00Z",
                    },
                ),
                tracker_record_id="IC_rebuild_decision",
                tracker_record_url=f"{ISSUE_URL}#issuecomment-rebuild-decision",
            ),
        ]
    )

    application, _ = _application(tmp_path, tracker)

    approval = application.map_detail(map_id=MAP_ID)["approvals"]["items"][0]
    assert approval["status"] == "approved"
    assert approval["decision"]["note"] == "Authoritative approval."
    assert [event["event_type"] for event in approval["history"]] == [
        "requested",
        "approved",
    ]


def test_authoritative_reconcile_repairs_stale_approval_projection(tmp_path):
    clock = _clock_box()
    application, tracker = _application(tmp_path, clock=clock)
    _request_and_approve(application)
    database = tmp_path / "plugin-data" / "map-governance" / "registry.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE approval_ledger SET status = 'pending', decided_by = NULL, "
            "decision_note = NULL WHERE request_id = ?",
            ("approval-delivery-001",),
        )
        connection.execute(
            "DELETE FROM approval_ledger_events WHERE event_type = 'approved'"
        )

    clock[0] += timedelta(minutes=5)
    reconciled = application.reconcile_project(project_id="PVT_acme_7")

    approval = reconciled["maps"][0]["approval_summary"]["latest"]
    assert approval["status"] == "approved"
    assert approval["decision"]["note"] == ("Approved for the declared delivery scope.")
    assert application.board()["projects"][0]["authority"]["state"] == "healthy"


def test_reconcile_stays_stale_when_authoritative_approval_history_is_incomplete(
    tmp_path,
):
    application, tracker = _application(tmp_path)
    _request_and_approve(application)
    tracker.approval_records[ISSUE_URL].clear()

    reconciled = application.reconcile_project(project_id="PVT_acme_7")

    assert reconciled["projects"][0]["authority"]["state"] == "stale"
    assert reconciled["maps"][0]["approval_summary"]["latest"]["status"] == ("approved")


def test_concurrent_chairman_decisions_commit_exactly_one_result(tmp_path):
    application, tracker = _application(tmp_path)
    application.request_approval(
        map_id=MAP_ID,
        request_identity=_ceo_identity(),
        packet=_packet(),
    )
    barrier = Barrier(2)

    def decide(decision):
        barrier.wait(timeout=2)
        try:
            return application.decide_approval(
                map_id=MAP_ID,
                request_id="approval-delivery-001",
                actor_identity=_chairman_identity(),
                decision=decision,
                note=f"Concurrent {decision} decision.",
            )
        except ApprovalEnforcementError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(decide, ["approved", "rejected"]))

    assert len([result for result in results if isinstance(result, dict)]) == 1
    assert len([result for result in results if isinstance(result, Exception)]) == 1
    assert tracker.approval_writes == 2
    assert application.map_detail(map_id=MAP_ID)["approvals"]["items"][0]["status"] in {
        "approved",
        "rejected",
    }


def test_ceo_self_approval_cross_profile_and_environment_spoof_are_denied_and_audited(
    tmp_path,
    monkeypatch,
):
    application, tracker = _application(tmp_path)
    application.request_approval(
        map_id=MAP_ID,
        request_identity=_ceo_identity(),
        packet=_packet(),
    )
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "chairman")
    monkeypatch.setenv("HERMES_SESSION_ID", "chairman-forgery")

    identities = [
        GovernanceActorIdentity(
            role="ceo",
            profile_name=PROFILE,
            actor_id="model-claim",
            session_id=LIVE_SESSION_ID,
        ),
        _chairman_identity(profile="other-profile"),
        GovernanceActorIdentity(
            role="chairman",
            profile_name=PROFILE,
            actor_id="",
            session_id="dashboard-request",
        ),
        _chairman_identity(actor_id="intruder"),
    ]
    expected_reasons = [
        "chairman_actor_required",
        "profile_mismatch",
        "chairman_actor_missing",
        "chairman_actor_not_authorized",
    ]
    for identity, reason in zip(identities, expected_reasons, strict=True):
        with pytest.raises(GovernanceAuthorizationError) as raised:
            application.decide_approval(
                map_id=MAP_ID,
                request_id="approval-delivery-001",
                actor_identity=identity,
                decision="approved",
                note="Unauthorized.",
            )
        assert raised.value.reason == reason

    assert tracker.approval_writes == 1
    assert [row["reason"] for row in application.authorization_denials(map_id=MAP_ID)][
        -4:
    ] == expected_reasons


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("missing", "approval_missing"),
        ("rejected", "approval_rejected"),
        ("revision", "approval_revision_required"),
        ("revoked", "approval_revoked"),
        ("expired", "approval_expired"),
        ("consumed", "approval_consumed"),
    ],
)
def test_protected_transition_denies_every_invalid_ledger_state(
    tmp_path,
    state,
    reason,
):
    clock = _clock_box()
    application, tracker = _application(tmp_path / state, clock=clock)
    request_id = "unknown" if state == "missing" else "approval-delivery-001"
    if state != "missing":
        application.request_approval(
            map_id=MAP_ID,
            request_identity=_ceo_identity(),
            packet=_packet(),
        )
    if state in {"rejected", "revision"}:
        application.decide_approval(
            map_id=MAP_ID,
            request_id=request_id,
            actor_identity=_chairman_identity(),
            decision=state,
            note="Not approved.",
        )
    elif state in {"revoked", "expired", "consumed"}:
        application.decide_approval(
            map_id=MAP_ID,
            request_id=request_id,
            actor_identity=_chairman_identity(),
            decision="approved",
            note="Initially approved.",
        )
        if state == "revoked":
            application.revoke_approval(
                map_id=MAP_ID,
                request_id=request_id,
                actor_identity=_chairman_identity(),
                note="Capital allocation withdrawn.",
            )
        elif state == "expired":
            clock[0] += timedelta(days=2)
        else:
            application.transition_map(
                map_id=MAP_ID,
                expected_stage="awaiting-approval",
                requested_stage="authorized",
                approval_request_id=request_id,
                mutation_id="transition-001",
            )
            tracker.issues[ISSUE_URL] = replace(
                tracker.issues[ISSUE_URL],
                labels=("map", "map-stage/awaiting-approval"),
            )

    with pytest.raises(ApprovalEnforcementError) as raised:
        application.transition_map(
            map_id=MAP_ID,
            expected_stage="awaiting-approval",
            requested_stage="authorized",
            approval_request_id=request_id,
            mutation_id="transition-invalid-state",
        )

    assert raised.value.reason == reason
    assert tracker.transition_calls == (1 if state == "consumed" else 0)
    assert application.authorization_denials(map_id=MAP_ID)[-1]["reason"] == reason


@pytest.mark.parametrize(
    ("packet_change", "reason"),
    [
        (
            {"proposed_action": "remote_publication"},
            "approval_action_mismatch",
        ),
        (
            {"requested_scope": {"map_id": MAP_ID, "repository": "acme/other"}},
            "approval_scope_mismatch",
        ),
        (
            {
                "decision_payload": {
                    "expected_stage": "discovery",
                    "requested_stage": "authorized",
                }
            },
            "approval_payload_mismatch",
        ),
    ],
)
def test_approved_action_scope_and_content_are_exactly_bound(
    tmp_path,
    packet_change,
    reason,
):
    application, tracker = _application(tmp_path)
    _request_and_approve(application, packet=_packet(**packet_change))

    with pytest.raises(ApprovalEnforcementError) as raised:
        application.transition_map(
            map_id=MAP_ID,
            expected_stage="awaiting-approval",
            requested_stage="authorized",
            approval_request_id="approval-delivery-001",
            mutation_id="transition-mismatch",
        )

    assert raised.value.reason == reason
    assert tracker.transition_calls == 0


def test_valid_approval_is_consumed_atomically_and_same_mutation_replays_idempotently(
    tmp_path,
):
    application, tracker = _application(tmp_path)
    _request_and_approve(application)

    first = application.transition_map(
        map_id=MAP_ID,
        expected_stage="awaiting-approval",
        requested_stage="authorized",
        approval_request_id="approval-delivery-001",
        mutation_id="transition-001",
    )
    replay = application.transition_map(
        map_id=MAP_ID,
        expected_stage="awaiting-approval",
        requested_stage="authorized",
        approval_request_id="approval-delivery-001",
        mutation_id="transition-001",
    )

    assert first["stage"] == "authorized"
    assert first["protected_mutation"]["idempotent"] is False
    assert replay["stage"] == "authorized"
    assert replay["protected_mutation"]["idempotent"] is True
    assert tracker.transition_calls == 1
    approval = application.map_detail(map_id=MAP_ID)["approvals"]["items"][0]
    assert approval["status"] == "consumed"
    assert approval["consumption"]["mutation_id"] == "transition-001"
    assert [event["event_type"] for event in approval["history"]] == [
        "requested",
        "approved",
        "consumed",
    ]


def test_protected_reservation_rolls_back_when_outbox_enqueue_conflicts(tmp_path):
    application, tracker = _application(tmp_path)
    _request_and_approve(application)
    storage_root = tmp_path / "plugin-data" / "map-governance"
    OutboxRepository(storage_root).enqueue(
        effect_id="stage-transition:transition-atomic-001",
        effect_type="tracker.stage-transition",
        map_id=MAP_ID,
        payload={"different": "payload"},
        created_at="2026-08-23T09:30:00Z",
    )

    with pytest.raises(ApprovalRequestConflict, match="stable mutation identity"):
        application.transition_map(
            map_id=MAP_ID,
            expected_stage="awaiting-approval",
            requested_stage="authorized",
            approval_request_id="approval-delivery-001",
            mutation_id="transition-atomic-001",
        )

    approval = application.map_detail(map_id=MAP_ID)["approvals"]["items"][0]
    assert approval["status"] == "approved"
    assert [event["event_type"] for event in approval["history"]] == [
        "requested",
        "approved",
    ]
    assert tracker.transition_calls == 0


def test_unresolved_confirmed_revocation_blocks_approval_consumption(tmp_path):
    clock = _clock_box()
    application, tracker = _application(tmp_path, clock=clock)
    _request_and_approve(application)

    def crash_after_revocation(point, intent):
        event = intent.payload.get("event")
        if (
            point == "after_external_call"
            and isinstance(event, dict)
            and event.get("event_type") == "revoked"
        ):
            raise SimulatedApprovalProcessCrash()

    crashing_process = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
        tracker=tracker,
        profile_name=PROFILE,
        clock=lambda: clock[0],
        authority_policy=AuthorityEnvelopePolicy.from_settings(
            {"chairman_actor_ids": ["chairman-1"]}
        ),
        outbox_crash_injector=crash_after_revocation,
    )

    with pytest.raises(SimulatedApprovalProcessCrash):
        crashing_process.revoke_approval(
            map_id=MAP_ID,
            request_id="approval-delivery-001",
            actor_identity=_chairman_identity(),
            note="Withdrawn before delivery authorization was consumed.",
        )

    approval = application.map_detail(map_id=MAP_ID)["approvals"]["items"][0]
    assert approval["status"] == "approved"
    assert tracker.approval_records[ISSUE_URL][-1].event.event_type == "revoked"

    with pytest.raises(ApprovalEnforcementError) as raised:
        application.transition_map(
            map_id=MAP_ID,
            expected_stage="awaiting-approval",
            requested_stage="authorized",
            approval_request_id="approval-delivery-001",
            mutation_id="transition-after-confirmed-revocation",
        )

    assert raised.value.reason == "approval_revocation_pending"
    assert tracker.transition_calls == 0
    assert (
        application.map_detail(map_id=MAP_ID)["approvals"]["items"][0]["status"]
        == "approved"
    )


def test_tracker_transition_failure_keeps_projection_prior_and_reserves_only_same_mutation(
    tmp_path,
):
    application, tracker = _application(tmp_path)
    _request_and_approve(application)
    tracker.transition_error = RuntimeError("simulated ambiguous tracker failure")

    with pytest.raises(RuntimeError, match="ambiguous tracker failure"):
        application.transition_map(
            map_id=MAP_ID,
            expected_stage="awaiting-approval",
            requested_stage="authorized",
            approval_request_id="approval-delivery-001",
            mutation_id="transition-recoverable",
        )

    assert application.board()["maps"][0]["stage"] == "awaiting-approval"
    approval = application.map_detail(map_id=MAP_ID)["approvals"]["items"][0]
    assert approval["status"] == "consumed"
    assert approval["consumption"]["mutation_id"] == "transition-recoverable"

    tracker.transition_error = None
    application.reconcile_project(project_id="PVT_acme_7")
    recovered = application.transition_map(
        map_id=MAP_ID,
        expected_stage="awaiting-approval",
        requested_stage="authorized",
        approval_request_id="approval-delivery-001",
        mutation_id="transition-recoverable",
    )
    assert recovered["stage"] == "authorized"
    assert recovered["protected_mutation"]["idempotent"] is True
    assert tracker.transition_calls == 2


def test_concurrent_consumers_allow_exactly_one_unrelated_mutation(tmp_path):
    application, tracker = _application(tmp_path)
    _request_and_approve(application)
    barrier = Barrier(2)

    def consume(mutation_id):
        barrier.wait(timeout=2)
        try:
            return application.transition_map(
                map_id=MAP_ID,
                expected_stage="awaiting-approval",
                requested_stage="authorized",
                approval_request_id="approval-delivery-001",
                mutation_id=mutation_id,
            )
        except (ApprovalEnforcementError, TrackerConflictError) as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(consume, ["transition-a", "transition-b"]))

    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ApprovalEnforcementError)
    assert failures[0].reason == "approval_consumed"
    assert tracker.transition_calls == 1


def test_restart_preserves_pending_approved_and_consumed_ledger_state(tmp_path):
    clock = _clock_box()
    application, tracker = _application(tmp_path, clock=clock)
    _request_and_approve(application)
    application.transition_map(
        map_id=MAP_ID,
        expected_stage="awaiting-approval",
        requested_stage="authorized",
        approval_request_id="approval-delivery-001",
        mutation_id="transition-001",
    )

    restarted = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
        tracker=tracker,
        profile_name=PROFILE,
        clock=lambda: clock[0],
    )

    approval = restarted.map_detail(map_id=MAP_ID)["approvals"]["items"][0]
    assert approval["status"] == "consumed"
    assert approval["consumption"]["mutation_id"] == "transition-001"


def test_schema_v4_database_migrates_without_losing_existing_map_projection(tmp_path):
    application, tracker = _application(tmp_path)
    database = tmp_path / "plugin-data" / "map-governance" / "registry.db"
    import sqlite3

    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE approval_ledger_events")
        connection.execute("DROP TABLE protected_mutations")
        connection.execute("DROP TABLE approval_ledger")
        connection.execute(
            "UPDATE plugin_metadata SET schema_version = 4 WHERE namespace = ?",
            ("map-governance",),
        )

    restarted = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
        tracker=tracker,
        profile_name=PROFILE,
    )

    assert restarted.health()["status"] == "ready"
    assert restarted.board()["maps"][0]["id"] == MAP_ID
    assert restarted.map_detail(map_id=MAP_ID)["approvals"] == {
        "count": 0,
        "items": [],
    }
