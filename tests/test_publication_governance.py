from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from map_governance import (
    AcceptanceEvidence,
    ApprovalEnforcementError,
    ApprovalPacket,
    AuthorityEnvelopePolicy,
    GovernanceActorIdentity,
    GovernanceAuthorizationError,
    GovernanceRequestIdentity,
    MapGovernanceApplication,
    MapTransitionError,
    PMReportDraft,
    PublicationAction,
    PublicationRepairRequired,
    PublicationPartialFailure,
    RemotePublicationEvidence,
)
from map_governance.approvals import ApprovalHistoryEvent
from map_governance.effects import TRACKER_ISSUE_CLOSE, TrackerEffectAdapter
from map_governance.outbox import EffectTerminalError, OutboxIntent, OutboxSettings
from map_governance.publication import PublicationRecord
from map_governance.reports import PMReport, TrackerPMReportRecord
from map_governance.sessions import CanonicalSession
from map_governance.tracker import (
    TrackerApprovalRecord,
    TrackerPublicationRecord,
    TrackerIssue,
    TrackerProject,
    TrackerError,
)


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MAP_ID = "I_atlas_41"
PROJECT_ID = "PVT_acme_7"
PROJECT_URL = "https://github.com/orgs/acme/projects/7"
ISSUE_URL = "https://github.com/acme/atlas/issues/41"
PM_IDENTITY = GovernanceRequestIdentity("pm", "pm-atlas")
REVISION = "a" * 40
CEO_IDENTITY = GovernanceRequestIdentity("ceo", "ceo-live-atlas")


class PublicationTracker:
    def __init__(self) -> None:
        self.issue = TrackerIssue(
            id=MAP_ID,
            repository="acme/atlas",
            number=41,
            title="Map the Atlas launch",
            url=ISSUE_URL,
            state="open",
            state_reason=None,
            labels=("map", "map-stage/delivery"),
        )
        self.reports: list[TrackerPMReportRecord] = []
        self.approvals: list[TrackerApprovalRecord] = []
        self.publications: list[TrackerPublicationRecord] = []
        self.effect_order: list[str] = []

    def get_project(self, url: str) -> TrackerProject:
        return TrackerProject(
            id=PROJECT_ID,
            owner="acme",
            owner_type="organization",
            number=7,
            title="Acme CEO portfolio",
            url=url,
        )

    def get_issue(self, url: str) -> TrackerIssue:
        assert url == ISSUE_URL
        return self.issue

    def list_decisions(self, _url: str) -> list:
        return []

    def list_pm_reports(self, _url: str) -> list[TrackerPMReportRecord]:
        return list(self.reports)

    def list_approval_events(self, _url: str) -> list[TrackerApprovalRecord]:
        return list(self.approvals)

    def append_approval_event(
        self,
        url: str,
        *,
        issue_id: str,
        event: ApprovalHistoryEvent,
    ) -> TrackerApprovalRecord:
        assert (url, issue_id) == (ISSUE_URL, MAP_ID)
        record = TrackerApprovalRecord(
            event=event,
            tracker_record_id=f"IC_approval_{len(self.approvals) + 1}",
            tracker_record_url=(
                f"{ISSUE_URL}#issuecomment-approval-{len(self.approvals) + 1}"
            ),
        )
        self.approvals.append(record)
        return record

    def list_publication_records(self, _url: str) -> list[TrackerPublicationRecord]:
        return list(self.publications)

    def append_publication_record(
        self,
        url: str,
        *,
        issue_id: str,
        record,
    ) -> TrackerPublicationRecord:
        assert (url, issue_id) == (ISSUE_URL, MAP_ID)
        self.effect_order.append("publication_evidence")
        stored = TrackerPublicationRecord(
            record=record,
            tracker_record_id=f"IC_publication_{len(self.publications) + 1}",
            tracker_record_url=(
                f"{ISSUE_URL}#issuecomment-publication-{len(self.publications) + 1}"
            ),
        )
        self.publications.append(stored)
        return stored

    def close_issue(
        self,
        url: str,
        *,
        issue_id: str,
        state_reason: str,
    ) -> TrackerIssue:
        assert (url, issue_id) == (ISSUE_URL, MAP_ID)
        assert state_reason in {"completed", "not_planned"}
        self.effect_order.append("issue_close")
        self.issue = replace(
            self.issue,
            state="closed",
            state_reason=state_reason,
            labels=("map",),
        )
        return self.issue

    def append_pm_report(
        self,
        url: str,
        *,
        issue_id: str,
        report: PMReport,
    ) -> TrackerPMReportRecord:
        assert (url, issue_id) == (ISSUE_URL, MAP_ID)
        record = TrackerPMReportRecord(
            report=report,
            tracker_record_id=f"IC_pm_{len(self.reports) + 1}",
            tracker_record_url=f"{ISSUE_URL}#issuecomment-pm-{len(self.reports) + 1}",
        )
        self.reports.append(record)
        return record

    def transition_issue_stage(
        self,
        url: str,
        *,
        expected_stage: str,
        requested_stage: str,
    ) -> TrackerIssue:
        assert url == ISSUE_URL
        current_stage = next(
            label.removeprefix("map-stage/")
            for label in self.issue.labels
            if label.startswith("map-stage/")
        )
        assert current_stage == expected_stage
        self.issue = replace(
            self.issue,
            labels=("map", f"map-stage/{requested_stage}"),
        )
        return self.issue


class PublicationSessionRunner:
    def __init__(self) -> None:
        self.session: CanonicalSession | None = None
        self.resume_markers: set[tuple[str, str]] = set()

    def find_exact(self, *, title: str):
        return [self.session] if self.session and self.session.title == title else []

    def mint(self, *, title: str, **_kwargs):
        self.session = CanonicalSession(
            root_session_id="ceo-root-atlas",
            live_session_id=CEO_IDENTITY.session_id,
            title=title,
            last_activity_at="2026-08-24T08:00:00Z",
            bootstrap_sent=True,
        )
        return self.session

    def initialize(self, session, **_kwargs):
        return session

    def resolve(self, *, root_session_id: str):
        if self.session and self.session.root_session_id == root_session_id:
            return self.session
        return None

    def load_skill(self, session, **_kwargs):
        return session

    def has_resume_marker(self, *, root_session_id: str, idempotency_key: str):
        return (root_session_id, idempotency_key) in self.resume_markers

    def resume_once(self, *, root_session_id: str, idempotency_key: str, **_kwargs):
        self.resume_markers.add((root_session_id, idempotency_key))
        return self.session


class FakePublisher:
    authority_ref = "gh:github.com:publisher"
    profile_name = "publisher"

    def __init__(self) -> None:
        self.calls = []
        self.evidence: dict[str, RemotePublicationEvidence] = {}
        self.before_execute = None
        self.failure: Exception | None = None
        self.omit_evidence = False
        self.validate_action_calls = 0
        self.validation_failure_on: int | None = None
        self.after_validate_action = None
        self.absence_confirmed = True
        self.readback_failure: Exception | None = None

    def validate_authority(self) -> None:
        return None

    def validate_action(self, _action) -> None:
        self.validate_action_calls += 1
        if self.validation_failure_on == self.validate_action_calls:
            raise ValueError("approved target drifted during final preflight")
        if self.after_validate_action is not None:
            self.after_validate_action()
        return None

    def readback(self, action):
        if self.calls and self.readback_failure is not None:
            raise self.readback_failure
        return self.evidence.get(action.action_id)

    def confirms_absence(self, _action) -> bool:
        return self.absence_confirmed

    def execute(self, action) -> None:
        if self.before_execute is not None:
            self.before_execute(action)
        self.calls.append(action)
        if self.failure is not None:
            raise self.failure
        if self.omit_evidence:
            return
        self.evidence[action.action_id] = RemotePublicationEvidence(
            action_id=action.action_id,
            revision=action.revision,
            action=action.action,
            target=action.target,
            provider="fake-github",
            remote_id="remote-main-a",
            remote_url="https://github.com/acme/atlas/commit/" + action.revision,
            published_at="2026-08-24T08:05:00Z",
        )


class FakeCoordinator:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.acknowledgments: dict[tuple[str, str], dict] = {}

    def readback(self, *, map_id: str, turn_id: str):
        return self.acknowledgments.get((map_id, turn_id))

    def resume(self, *, map_id: str, turn_id: str, content: str, **arguments):
        self.calls.append(
            {"map_id": map_id, "turn_id": turn_id, "content": content, **arguments}
        )
        self.acknowledgments[(map_id, turn_id)] = {
            "content_hash": "sha256:" + hashlib.sha256(content.encode()).hexdigest()
        }


def _application(tmp_path) -> tuple[MapGovernanceApplication, PublicationTracker]:
    tracker = PublicationTracker()
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data",
        tracker=tracker,
        profile_name="pm",
        clock=lambda: datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc),
    )
    project = application.configure_project(project_url=PROJECT_URL)
    application.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    application.assign_pm(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="acceptance-turn",
    )
    return application, tracker


def _acceptance() -> AcceptanceEvidence:
    return AcceptanceEvidence(
        revision=REVISION,
        delivered_scope=("Chairman-gated publication and closeout",),
        validations=("Focused tests and static checks passed",),
        known_limitations=("A privileged publisher is still required",),
        rollback_considerations=("Restore the previous remote ref",),
        requested_publication_action=PublicationAction(
            action="push",
            target={
                "repository": "acme/atlas",
                "ref": "refs/heads/main",
            },
        ),
    )


def _governed_application(
    tmp_path,
    *,
    publisher=None,
    outbox_crash_injector=None,
    clock=None,
    coordinator=None,
    outbox_settings=None,
    publication_handoff=None,
) -> tuple[MapGovernanceApplication, PublicationTracker]:
    tracker = PublicationTracker()
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data",
        tracker=tracker,
        session_runner=PublicationSessionRunner(),
        profile_name="ceo",
        clock=clock or (lambda: datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)),
        authority_policy=AuthorityEnvelopePolicy.from_settings(
            {"chairman_actor_ids": ["basic:chairman"]}
        ),
        publisher=publisher,
        publication_handoff=publication_handoff,
        publication_authority_ref=(
            publisher.authority_ref
            if publisher is not None
            else FakePublisher.authority_ref
        ),
        coordinator_resume=coordinator or FakeCoordinator(),
        outbox_crash_injector=outbox_crash_injector,
        outbox_settings=outbox_settings or OutboxSettings(),
    )
    project = application.configure_project(project_url=PROJECT_URL)
    application.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    application.open_map(map_id=MAP_ID)
    application.assign_pm(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="acceptance-turn",
    )
    application.report_pm(
        request_identity=PM_IDENTITY,
        report=PMReportDraft(
            record_id="acceptance-publication-1",
            report_type="acceptance",
            summary="The exact local revision is ready for chairman review.",
            timestamp="2026-08-24T08:00:00Z",
            evidence=("All declared local outcomes passed.",),
            acceptance=_acceptance(),
        ),
    )
    return application, tracker


def _chairman_identity() -> GovernanceActorIdentity:
    return GovernanceActorIdentity(
        role="chairman",
        profile_name="ceo",
        actor_id="basic:chairman",
        session_id="dashboard:chairman",
    )


def _request_and_approve_publication(application):
    packet = _publication_packet_from_detail(
        application,
        request_id="publish-atlas-main-a",
    )
    application.request_approval(
        map_id=MAP_ID,
        request_identity=CEO_IDENTITY,
        packet=packet,
    )
    application.decide_approval(
        map_id=MAP_ID,
        request_id=packet.request_id,
        actor_identity=_chairman_identity(),
        decision="approved",
        note="Publish only this revision to the declared main ref.",
    )
    return packet


def _publication_packet_from_detail(application, *, request_id: str) -> ApprovalPacket:
    approval = application.map_detail(map_id=MAP_ID)["publication_approval"]
    return ApprovalPacket(
        request_id=request_id,
        decision_class=approval["decision_class"],
        proposed_action=approval["proposed_action"],
        alternatives=("Publish the exact revision", "Request changes"),
        rationale="The locally validated outcome is ready for publication.",
        cost_risk="Remote publication changes the declared target.",
        evidence=(f"{ISSUE_URL}#issuecomment-pm-1",),
        requested_scope=approval["requested_scope"],
        decision_payload=approval["decision_payload"],
    )


def _publication_packet(
    *,
    request_id: str,
    acceptance: AcceptanceEvidence,
    acceptance_report_id: str = "acceptance-publication-1",
):
    publication = acceptance.requested_publication_action
    return ApprovalPacket(
        request_id=request_id,
        decision_class="remote_publication",
        proposed_action="publish_map",
        alternatives=("Publish the exact revision", "Request changes"),
        rationale="The locally validated outcome is ready for publication.",
        cost_risk="Remote publication changes the declared target.",
        evidence=(f"{ISSUE_URL}#issuecomment-pm-1",),
        requested_scope={
            "map_id": MAP_ID,
            "publication_target": publication.target,
            "publisher_authority_ref": FakePublisher.authority_ref,
        },
        decision_payload={
            "revision": acceptance.revision,
            "publication_action": publication.action,
            "publication_target": publication.target,
            "publisher_authority_ref": FakePublisher.authority_ref,
            "acceptance_evidence_hash": acceptance.content_hash,
            "acceptance_report_id": acceptance_report_id,
        },
    )


def _cancellation_packet() -> ApprovalPacket:
    return ApprovalPacket(
        request_id="cancel-atlas-not-planned",
        decision_class="cancellation",
        proposed_action="cancel_map",
        alternatives=("Cancel as not planned", "Keep the Map open"),
        rationale="The initiative is no longer planned.",
        cost_risk="Cancellation closes the governance Issue without publication.",
        evidence=(ISSUE_URL,),
        requested_scope={"map_id": MAP_ID},
        decision_payload={"state_reason": "not_planned"},
    )


def test_local_acceptance_exposes_a_structured_publication_packet_without_publishing(
    tmp_path,
):
    application, tracker = _application(tmp_path)
    acceptance = _acceptance()

    application.report_pm(
        request_identity=PM_IDENTITY,
        report=PMReportDraft(
            record_id="acceptance-publication-1",
            report_type="acceptance",
            summary="The exact local revision is ready for chairman review.",
            timestamp="2026-08-24T08:00:00Z",
            evidence=("All declared local outcomes passed.",),
            acceptance=acceptance,
        ),
    )

    detail = application.map_detail(map_id=MAP_ID)
    assert detail["stage"] == "acceptance"
    assert detail["acceptance"] == {
        **acceptance.payload(),
        "acceptance_evidence_hash": acceptance.content_hash,
        "report": {
            "id": "acceptance-publication-1",
            "tracker_url": f"{ISSUE_URL}#issuecomment-pm-1",
        },
    }
    assert tracker.issue.state == "open"
    assert not hasattr(tracker, "publication_calls")


def test_local_acceptance_prepares_passive_publication_handoff(tmp_path):
    class RecordingHandoff:
        def __init__(self):
            self.calls = []

        def prepare(self, evidence):
            self.calls.append(evidence)

    handoff = RecordingHandoff()
    application, tracker = _governed_application(tmp_path, publication_handoff=handoff)

    assert handoff.calls == [_acceptance()]
    assert tracker.issue.state == "open"
    assert application.map_detail(map_id=MAP_ID)["stage"] == "acceptance"


def test_map_detail_exposes_server_derived_publication_approval_binding(tmp_path):
    application, _tracker = _governed_application(tmp_path)

    detail = application.map_detail(map_id=MAP_ID)
    inspected = application.executive_state(
        map_id=MAP_ID,
        request_identity=CEO_IDENTITY,
    )

    assert detail["publication_approval"] == {
        "decision_class": "remote_publication",
        "proposed_action": "publish_map",
        "requested_scope": {
            "map_id": MAP_ID,
            "publication_target": {
                "repository": "acme/atlas",
                "ref": "refs/heads/main",
            },
            "publisher_authority_ref": "gh:github.com:publisher",
        },
        "decision_payload": {
            "revision": REVISION,
            "publication_action": "push",
            "publication_target": {
                "repository": "acme/atlas",
                "ref": "refs/heads/main",
            },
            "publisher_authority_ref": "gh:github.com:publisher",
            "acceptance_evidence_hash": _acceptance().content_hash,
            "acceptance_report_id": "acceptance-publication-1",
        },
    }
    assert inspected["map"]["publication_approval"] == detail["publication_approval"]


def test_publication_approval_must_match_the_tracker_confirmed_acceptance_packet(
    tmp_path,
):
    application, tracker = _governed_application(tmp_path)

    approved_packet = _publication_packet_from_detail(
        application,
        request_id="publish-atlas-main-a",
    )
    requested = application.request_approval(
        map_id=MAP_ID,
        request_identity=CEO_IDENTITY,
        packet=approved_packet,
    )

    assert requested["approval"]["payload_hash"] == approved_packet.payload_hash
    changed_revision = replace(_acceptance(), revision="b" * 40)
    changed_target = replace(
        _acceptance(),
        requested_publication_action=PublicationAction(
            action="push",
            target={"repository": "acme/atlas", "ref": "refs/heads/beta"},
        ),
    )
    for request_id, changed in (
        ("publish-changed-revision", changed_revision),
        ("publish-changed-target", changed_target),
    ):
        with pytest.raises(
            ApprovalEnforcementError,
            match="publication_acceptance_mismatch",
        ):
            application.request_approval(
                map_id=MAP_ID,
                request_identity=CEO_IDENTITY,
                packet=_publication_packet(
                    request_id=request_id,
                    acceptance=changed,
                ),
            )
    assert len(tracker.approvals) == 1


def test_approved_publisher_records_remote_evidence_before_completed_closeout(
    tmp_path,
):
    publisher = FakePublisher()
    application, tracker = _governed_application(tmp_path, publisher=publisher)
    packet = _request_and_approve_publication(application)
    observed = []
    publisher.before_execute = lambda action: observed.append(
        {
            "action": action.payload(),
            "issue_state": tracker.issue.state,
            "publication_records": len(tracker.publications),
            "approval_status": application.map_detail(map_id=MAP_ID)["approvals"][
                "items"
            ][0]["status"],
        }
    )

    result = application.publish_map(
        map_id=MAP_ID,
        approval_request_id=packet.request_id,
        mutation_id="publish-atlas-main-a-action",
    )

    assert observed == [
        {
            "action": {
                "action_id": "publish-atlas-main-a-action",
                "map_id": MAP_ID,
                "revision": REVISION,
                "action": "push",
                "target": {
                    "repository": "acme/atlas",
                    "ref": "refs/heads/main",
                },
            },
            "issue_state": "open",
            "publication_records": 0,
            "approval_status": "consumed",
        }
    ]
    assert tracker.effect_order == ["publication_evidence", "issue_close"]
    assert len(publisher.calls) == 1
    assert result["stage"] == "done"
    detail = application.map_detail(map_id=MAP_ID)
    assert detail["publication"]["state"] == "published"
    assert detail["publication"]["records"][0]["status"] == "succeeded"
    assert tracker.issue.state_reason == "completed"


def test_fresh_projection_rebuilds_publication_only_from_tracker_issue_history(
    tmp_path,
):
    publisher = FakePublisher()
    application, tracker = _governed_application(tmp_path, publisher=publisher)
    packet = _request_and_approve_publication(application)
    application.publish_map(
        map_id=MAP_ID,
        approval_request_id=packet.request_id,
        mutation_id="publish-for-rebuild-action",
    )

    rebuilt = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "rebuilt-plugin-data",
        tracker=tracker,
        clock=lambda: datetime(2026, 8, 24, 8, 10, tzinfo=timezone.utc),
    )
    project = rebuilt.configure_project(project_url=PROJECT_URL)
    rebuilt.bind_map(project_id=project["id"], issue_url=ISSUE_URL)

    detail = rebuilt.map_detail(map_id=MAP_ID)
    assert detail["stage"] == "done"
    assert detail["publication"]["state"] == "published"
    assert detail["publication"]["records"][0]["tracker"]["id"] == ("IC_publication_1")


def test_unapproved_publication_and_worker_credential_escalation_are_denied(tmp_path):
    publisher = FakePublisher()
    application, _tracker = _governed_application(tmp_path, publisher=publisher)

    with pytest.raises(ApprovalEnforcementError, match="approval_missing"):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id="missing-publication-approval",
            mutation_id="unapproved-publication-action",
        )

    packet = _request_and_approve_publication(application)
    worker_application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data",
        tracker=_tracker,
        profile_name="pm",
        clock=lambda: datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc),
    )
    with pytest.raises(
        GovernanceAuthorizationError,
        match="publisher_capability_unavailable",
    ):
        worker_application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id="worker-escalation-action",
        )

    assert publisher.calls == []
    detail = application.map_detail(map_id=MAP_ID)
    assert detail["approvals"]["items"][0]["status"] == "approved"


def test_publication_target_rejects_nested_credential_material():
    with pytest.raises(ValueError, match="must not contain publisher credentials"):
        PublicationAction(
            action="push",
            target={
                "repository": "acme/atlas",
                "provider": {"access_token": "github_pat_never-copy-me"},
            },
        )


def test_changed_acceptance_content_makes_an_approved_publication_stale(tmp_path):
    publisher = FakePublisher()
    application, tracker = _governed_application(tmp_path, publisher=publisher)
    packet = _request_and_approve_publication(application)
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="revised-acceptance-turn",
    )
    changed = replace(
        _acceptance(),
        delivered_scope=("A materially changed delivered scope",),
    )
    application.report_pm(
        request_identity=PM_IDENTITY,
        report=PMReportDraft(
            record_id="acceptance-publication-2",
            report_type="acceptance",
            summary="The acceptance content changed after approval.",
            timestamp="2026-08-24T08:01:00Z",
            evidence=("A new local review was submitted.",),
            acceptance=changed,
        ),
    )

    with pytest.raises(
        ApprovalEnforcementError,
        match="publication_acceptance_mismatch",
    ):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id="stale-publication-action",
        )

    assert publisher.calls == []
    assert tracker.issue.state == "open"
    assert application.map_detail(map_id=MAP_ID)["stage"] == "acceptance"


def test_publisher_authority_change_makes_an_approved_publication_stale(tmp_path):
    publisher = FakePublisher()
    application, tracker = _governed_application(tmp_path, publisher=publisher)
    packet = _request_and_approve_publication(application)
    publisher.authority_ref = "gh:enterprise.example:publisher"

    with pytest.raises(
        ApprovalEnforcementError, match="publication_acceptance_mismatch"
    ):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id="changed-publisher-authority",
        )

    assert publisher.calls == []
    assert tracker.issue.state == "open"


def test_partial_remote_failure_records_repair_incident_without_retry_or_close(
    tmp_path,
):
    publisher = FakePublisher()
    publisher.failure = PublicationPartialFailure(
        "Remote ref changed, but provider evidence could not be confirmed."
    )
    application, tracker = _governed_application(tmp_path, publisher=publisher)
    packet = _request_and_approve_publication(application)

    with pytest.raises(PublicationRepairRequired) as captured:
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id="partial-publication-action",
        )

    assert captured.value.as_dict() == {
        "type": "publication_repair_required",
        "map_id": MAP_ID,
        "action_id": "partial-publication-action",
        "reason": "Remote ref changed, but provider evidence could not be confirmed.",
        "retryable": False,
    }
    assert len(publisher.calls) == 1
    assert tracker.issue.state == "open"
    assert tracker.effect_order == ["publication_evidence"]
    assert tracker.publications[0].record.status == "repair_required"
    assert application.map_detail(map_id=MAP_ID)["publication"]["state"] == (
        "repair_required"
    )

    with pytest.raises(PublicationRepairRequired):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id="partial-publication-action",
        )
    assert len(publisher.calls) == 1


def test_post_apply_without_evidence_is_terminal_and_never_reexecuted(tmp_path):
    publisher = FakePublisher()
    publisher.omit_evidence = True
    application, tracker = _governed_application(tmp_path, publisher=publisher)
    packet = _request_and_approve_publication(application)
    action_id = "orphaned-publisher-attempt"

    with pytest.raises(PublicationRepairRequired):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id=action_id,
        )

    assert application.outbox_status(effect_id=f"publisher:{action_id}")["state"] == (
        "terminal"
    )
    application.recover_outbox()
    assert len(publisher.calls) == 1

    result = application.reconcile_publication(map_id=MAP_ID, action_id=action_id)

    assert result["stage"] == "acceptance"
    assert tracker.issue.state == "open"
    assert [item.record.status for item in tracker.publications] == [
        "repair_required",
        "aborted",
    ]
    publication = application.map_detail(map_id=MAP_ID)["publication"]
    assert publication["state"] == "aborted"
    assert publication["unresolved_incidents"] == []
    assert len(publisher.calls) == 1
    assert tracker.issue.state == "open"

    with pytest.raises(ApprovalEnforcementError, match="aborted_grant_consumed"):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id=action_id,
        )
    assert len(publisher.calls) == 1

    rebuilt = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "rebuilt-after-abort",
        tracker=tracker,
        profile_name="ceo",
        clock=lambda: datetime(2026, 8, 24, 8, 10, tzinfo=timezone.utc),
        authority_policy=AuthorityEnvelopePolicy.from_settings(
            {"chairman_actor_ids": ["basic:chairman"]}
        ),
        publisher=publisher,
    )
    project = rebuilt.configure_project(project_url=PROJECT_URL)
    rebuilt.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    assert rebuilt.map_detail(map_id=MAP_ID)["approvals"]["items"][0]["status"] == (
        "consumed"
    )
    with pytest.raises(ApprovalEnforcementError, match="approval_consumed"):
        rebuilt.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id="must-not-replay-after-rebuild",
        )


def test_unknown_publisher_exception_is_terminal_and_generic_repair_cannot_replay(
    tmp_path,
):
    publisher = FakePublisher()
    publisher.failure = RuntimeError("provider connection ended after request upload")
    application, tracker = _governed_application(tmp_path, publisher=publisher)
    packet = _request_and_approve_publication(application)

    with pytest.raises(PublicationRepairRequired):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id="unknown-outcome-action",
        )

    with pytest.raises(ValueError, match="cannot be re-executed"):
        application.repair_outbox(
            effect_id="publisher:unknown-outcome-action",
            repair_id="unsafe-retry",
            note="Do not repeat an uncertain mutation.",
        )
    application.recover_outbox()
    assert len(publisher.calls) == 1
    assert tracker.issue.state == "open"


def test_evidence_only_reconciliation_resolves_same_action_and_then_closes(tmp_path):
    now = [datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)]
    publisher = FakePublisher()
    publisher.failure = PublicationPartialFailure("Remote ref outcome is uncertain")
    application, tracker = _governed_application(
        tmp_path,
        publisher=publisher,
        clock=lambda: now[0],
    )
    packet = _request_and_approve_publication(application)
    action_id = "reconciled-publication-action"

    with pytest.raises(PublicationRepairRequired):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id=action_id,
        )
    action = publisher.calls[0]
    publisher.failure = None
    now[0] += timedelta(days=2)
    publisher.evidence[action_id] = RemotePublicationEvidence(
        action_id=action_id,
        revision=action.revision,
        action=action.action,
        target=action.target,
        provider="fake-github",
        remote_id="remote-reconciled",
        remote_url=f"https://github.com/acme/atlas/commit/{action.revision}",
        published_at="2026-08-26T08:00:00Z",
    )

    result = application.reconcile_publication(map_id=MAP_ID, action_id=action_id)

    assert result["stage"] == "done"
    publication = application.map_detail(map_id=MAP_ID)["publication"]
    assert publication["state"] == "published"
    assert publication["unresolved_incidents"] == []
    assert [item.record.status for item in tracker.publications] == [
        "repair_required",
        "succeeded",
    ]
    assert application.outbox_status(effect_id=f"publisher:{action_id}")["state"] == (
        "succeeded"
    )
    assert len(publisher.calls) == 1
    succeeded = tracker.publications[-1].record
    assert succeeded.occurred_at == "2026-08-24T08:00:00.000000Z"
    assert succeeded.evidence is not None
    assert succeeded.evidence.published_at == "2026-08-26T08:00:00Z"

    rebuilt = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "rebuilt-after-delayed-readback",
        tracker=tracker,
        profile_name="ceo",
        clock=lambda: now[0],
        authority_policy=AuthorityEnvelopePolicy.from_settings(
            {"chairman_actor_ids": ["basic:chairman"]}
        ),
        publisher=publisher,
    )
    project = rebuilt.configure_project(project_url=PROJECT_URL)
    rebuilt.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    assert rebuilt.map_detail(map_id=MAP_ID)["stage"] == "done"


def test_reconciliation_refuses_drift_or_ambiguous_remote_state(tmp_path):
    publisher = FakePublisher()
    publisher.failure = PublicationPartialFailure("Remote outcome is uncertain")
    publisher.absence_confirmed = False
    application, tracker = _governed_application(tmp_path, publisher=publisher)
    packet = _request_and_approve_publication(application)
    action_id = "drifted-publication-action"
    with pytest.raises(PublicationRepairRequired):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id=action_id,
        )
    publisher.failure = None

    with pytest.raises(PublicationRepairRequired, match="cannot prove absence"):
        application.reconcile_publication(map_id=MAP_ID, action_id=action_id)

    assert tracker.issue.state == "open"
    assert [item.record.status for item in tracker.publications] == ["repair_required"]
    assert application.outbox_status(effect_id=f"publisher:{action_id}")["state"] == (
        "terminal"
    )


def test_post_call_readback_exhaustion_always_records_repair_incident(tmp_path):
    publisher = FakePublisher()
    publisher.readback_failure = RuntimeError("provider readback unavailable")
    application, tracker = _governed_application(
        tmp_path,
        publisher=publisher,
        outbox_settings=OutboxSettings(max_attempts=1),
    )
    packet = _request_and_approve_publication(application)
    action_id = "readback-exhausted-action"

    with pytest.raises(PublicationRepairRequired):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id=action_id,
        )

    assert len(publisher.calls) == 1
    assert application.outbox_status(effect_id=f"publisher:{action_id}")["state"] == (
        "terminal"
    )
    assert [item.record.status for item in tracker.publications] == ["repair_required"]


def test_post_call_retry_confirms_incident_before_success_and_close(tmp_path):
    publisher = FakePublisher()
    publisher.readback_failure = RuntimeError(
        "provider readback temporarily unavailable"
    )
    application, tracker = _governed_application(tmp_path, publisher=publisher)
    packet = _request_and_approve_publication(application)
    action_id = "readback-retry-ordered-action"

    with pytest.raises(TrackerError, match="temporarily unavailable"):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id=action_id,
        )
    assert (
        application.outbox_status(
            effect_id=f"tracker-publication:{action_id}:repair-required"
        )["state"]
        == "pending"
    )

    publisher.readback_failure = None
    assert (
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id=action_id,
        )["stage"]
        == "done"
    )
    assert [item.record.status for item in tracker.publications] == [
        "repair_required",
        "succeeded",
    ]
    assert tracker.effect_order == [
        "publication_evidence",
        "publication_evidence",
        "issue_close",
    ]


def test_final_marker_rechecks_expiry_after_slow_preflight(tmp_path):
    publisher = FakePublisher()
    application, tracker = _governed_application(
        tmp_path,
        publisher=publisher,
    )
    packet = _request_and_approve_publication(application)
    action_id = "expired-during-preflight-action"

    def expire_after_final_preflight():
        if publisher.validate_action_calls == 2:
            with application._storage.atomic() as connection:
                connection.execute(
                    "UPDATE approval_ledger SET expires_at = ? WHERE request_id = ?",
                    ("2026-08-24T07:59:00Z", packet.request_id),
                )

    publisher.after_validate_action = expire_after_final_preflight

    with pytest.raises(ApprovalEnforcementError, match="stale before remote-call"):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id=action_id,
        )

    assert publisher.calls == []
    assert tracker.issue.state == "open"
    assert not application._outbox.external_call_started(f"publisher:{action_id}")


def test_marker_timestamps_after_blocking_tracker_reads(tmp_path):
    now = [datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)]
    publisher = FakePublisher()
    application, tracker = _governed_application(
        tmp_path, publisher=publisher, clock=lambda: now[0]
    )
    packet = _request_and_approve_publication(application)
    with application._storage.atomic() as connection:
        connection.execute(
            "UPDATE approval_ledger SET expires_at = ? WHERE request_id = ?",
            ("2026-08-24T08:00:01Z", packet.request_id),
        )
    original = tracker.list_pm_reports
    reads = 0

    def delayed_reports(url):
        nonlocal reads
        reads += 1
        result = original(url)
        if reads == 2:
            now[0] += timedelta(seconds=2)
        return result

    tracker.list_pm_reports = delayed_reports
    action_id = "expiry-during-marker-readback"

    with pytest.raises(ApprovalEnforcementError, match="remote-call marker"):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id=action_id,
        )
    assert publisher.calls == []
    assert not application._outbox.external_call_started(f"publisher:{action_id}")


def test_reconcile_confirms_incident_before_recording_success_and_close(tmp_path):
    interrupted = False

    def interrupt_incident(point, intent):
        nonlocal interrupted
        record = intent.payload.get("record")
        if (
            not interrupted
            and point == "before_external_call"
            and isinstance(record, dict)
            and record.get("status") == "repair_required"
        ):
            interrupted = True
            raise RuntimeError("incident tracker interruption")

    publisher = FakePublisher()
    publisher.failure = PublicationPartialFailure("Remote outcome is uncertain")
    application, tracker = _governed_application(
        tmp_path,
        publisher=publisher,
        outbox_crash_injector=interrupt_incident,
    )
    packet = _request_and_approve_publication(application)
    action_id = "incident-before-resolution-action"
    with pytest.raises(TrackerError, match="incident tracker interruption"):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id=action_id,
        )
    action = publisher.calls[0]
    publisher.failure = None
    publisher.evidence[action_id] = RemotePublicationEvidence(
        action_id=action_id,
        revision=action.revision,
        action=action.action,
        target=action.target,
        provider="fake-github",
        remote_id="remote-after-incident",
        remote_url=f"https://github.com/acme/atlas/commit/{action.revision}",
        published_at="2026-08-24T08:06:00Z",
    )
    application._storage.set_project_reachability(
        project_id=PROJECT_ID,
        source="tracker",
        state="healthy",
        reason=None,
        changed_at="2026-08-24T08:06:00Z",
    )

    assert (
        application.reconcile_publication(map_id=MAP_ID, action_id=action_id)["stage"]
        == "done"
    )
    assert tracker.effect_order == [
        "publication_evidence",
        "publication_evidence",
        "issue_close",
    ]
    assert [item.record.status for item in tracker.publications] == [
        "repair_required",
        "succeeded",
    ]


def test_confirmed_absent_reconcile_retry_dispatches_durable_successor(
    tmp_path, monkeypatch
):
    publisher = FakePublisher()
    publisher.omit_evidence = True
    application, tracker = _governed_application(tmp_path, publisher=publisher)
    packet = _request_and_approve_publication(application)
    action_id = "aborted-successor-retry"
    with pytest.raises(PublicationRepairRequired):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id=action_id,
        )
    original_dispatch = application._dispatch_if_present

    def interrupt_aborted(effect_id):
        if effect_id.endswith(":aborted"):
            raise RuntimeError("crash after atomic resolution")
        original_dispatch(effect_id)

    monkeypatch.setattr(application, "_dispatch_if_present", interrupt_aborted)
    with pytest.raises(RuntimeError, match="atomic resolution"):
        application.reconcile_publication(map_id=MAP_ID, action_id=action_id)
    assert application.outbox_status(effect_id=f"publisher:{action_id}")["state"] == (
        "succeeded"
    )
    assert (
        application.outbox_status(effect_id=f"tracker-publication:{action_id}:aborted")[
            "state"
        ]
        == "pending"
    )
    monkeypatch.setattr(application, "_dispatch_if_present", original_dispatch)

    assert (
        application.reconcile_publication(map_id=MAP_ID, action_id=action_id)["stage"]
        == "acceptance"
    )
    assert [item.record.status for item in tracker.publications] == [
        "repair_required",
        "aborted",
    ]


def test_evidence_reconciliation_atomically_enqueues_record_before_resolution(
    tmp_path, monkeypatch
):
    publisher = FakePublisher()
    publisher.failure = PublicationPartialFailure("Remote ref outcome is uncertain")
    application, tracker = _governed_application(tmp_path, publisher=publisher)
    packet = _request_and_approve_publication(application)
    action_id = "atomic-reconciliation-action"
    with pytest.raises(PublicationRepairRequired):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id=action_id,
        )
    action = publisher.calls[0]
    publisher.failure = None
    publisher.evidence[action_id] = RemotePublicationEvidence(
        action_id=action_id,
        revision=action.revision,
        action=action.action,
        target=action.target,
        provider="fake-github",
        remote_id="remote-atomic",
        remote_url=f"https://github.com/acme/atlas/commit/{action.revision}",
        published_at="2026-08-24T08:06:00Z",
    )
    original = application._outbox._enqueue_prepared

    def fail_successor(*_args, **_kwargs):
        raise RuntimeError("injected successor failure")

    monkeypatch.setattr(application._outbox, "_enqueue_prepared", fail_successor)
    with pytest.raises(RuntimeError, match="successor failure"):
        application.reconcile_publication(map_id=MAP_ID, action_id=action_id)

    assert application.outbox_status(effect_id=f"publisher:{action_id}")["state"] == (
        "terminal"
    )
    assert [item.record.status for item in tracker.publications] == ["repair_required"]
    monkeypatch.setattr(application._outbox, "_enqueue_prepared", original)

    assert (
        application.reconcile_publication(map_id=MAP_ID, action_id=action_id)["stage"]
        == "done"
    )


def test_preexisting_remote_state_cannot_retroactively_consume_a_new_grant(tmp_path):
    publisher = FakePublisher()
    application, tracker = _governed_application(tmp_path, publisher=publisher)
    packet = _request_and_approve_publication(application)
    action_id = "preexisting-remote-action"
    requested = _acceptance().requested_publication_action
    assert requested is not None
    publisher.evidence[action_id] = RemotePublicationEvidence(
        action_id=action_id,
        revision=REVISION,
        action=requested.action,
        target=requested.target,
        provider="fake-github",
        remote_id="remote-preexisting",
        remote_url=f"https://github.com/acme/atlas/commit/{REVISION}",
        published_at="2026-08-24T07:00:00Z",
    )

    with pytest.raises(ApprovalEnforcementError, match="Pre-existing remote state"):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id=action_id,
        )

    assert publisher.calls == []
    assert not application._outbox.external_call_started(f"publisher:{action_id}")
    assert tracker.publications == []
    assert tracker.issue.state == "open"


def test_final_preflight_failure_never_marks_an_uncertain_remote_attempt(tmp_path):
    publisher = FakePublisher()
    publisher.validation_failure_on = 2
    application, tracker = _governed_application(tmp_path, publisher=publisher)
    packet = _request_and_approve_publication(application)
    action_id = "final-preflight-drift"

    with pytest.raises(ApprovalEnforcementError, match="final preflight"):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id=action_id,
        )

    effect_id = f"publisher:{action_id}"
    assert publisher.calls == []
    assert not application._outbox.external_call_started(effect_id)
    assert tracker.publications == []
    assert (
        application.repair_outbox(
            effect_id=effect_id,
            repair_id="repair-final-preflight",
            note="The target drift was corrected before any remote call.",
        )["state"]
        == "pending"
    )


def test_expiry_before_final_execution_requires_a_new_approval(tmp_path):
    application_ref = []

    def expire_before_execution(point, intent):
        if (
            point == "before_external_call"
            and intent.effect_type == "publisher.execute"
        ):
            with application_ref[0]._storage.atomic() as connection:
                connection.execute(
                    "UPDATE approval_ledger SET expires_at = ? WHERE request_id = ?",
                    ("2026-08-24T07:59:00Z", intent.payload["approval_request_id"]),
                )

    publisher = FakePublisher()
    application, tracker = _governed_application(
        tmp_path,
        publisher=publisher,
        outbox_crash_injector=expire_before_execution,
    )
    application_ref.append(application)
    packet = _request_and_approve_publication(application)
    action_id = "expired-before-execution"

    with pytest.raises(ApprovalEnforcementError, match="became stale"):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id=action_id,
        )

    assert publisher.calls == []
    assert tracker.publications == []
    assert not application._outbox.external_call_started(f"publisher:{action_id}")


def test_unresolved_incident_blocks_a_later_acceptance_approval(tmp_path):
    publisher = FakePublisher()
    publisher.failure = PublicationPartialFailure("Remote state is uncertain")
    application, _tracker = _governed_application(tmp_path, publisher=publisher)
    packet = _request_and_approve_publication(application)
    with pytest.raises(PublicationRepairRequired):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id="unresolved-first-action",
        )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="new-acceptance-after-incident",
    )
    application.report_pm(
        request_identity=PM_IDENTITY,
        report=PMReportDraft(
            record_id="acceptance-publication-2",
            report_type="acceptance",
            summary="A later local acceptance cannot erase an incident.",
            timestamp="2026-08-24T08:07:00Z",
            evidence=("Local validation passed.",),
            acceptance=_acceptance(),
        ),
    )

    with pytest.raises(ApprovalEnforcementError, match="publication_repair_required"):
        application.request_approval(
            map_id=MAP_ID,
            request_identity=CEO_IDENTITY,
            packet=_publication_packet(
                request_id="later-publication-request",
                acceptance=_acceptance(),
                acceptance_report_id="acceptance-publication-2",
            ),
        )


@pytest.mark.parametrize("change", ["stage", "acceptance"])
def test_closeout_rechecks_authoritative_stage_and_acceptance_lineage(tmp_path, change):
    publisher = FakePublisher()
    application, tracker = _governed_application(tmp_path, publisher=publisher)
    packet = _request_and_approve_publication(application)

    def mutate_authority(_action):
        if change == "stage":
            tracker.issue = replace(tracker.issue, labels=("map", "map-stage/delivery"))
            return
        changed_report = PMReportDraft(
            record_id="acceptance-concurrent-2",
            report_type="acceptance",
            summary="Acceptance changed while publication completed.",
            timestamp="2026-08-24T08:09:00Z",
            evidence=("A later validation exists.",),
            acceptance=replace(
                _acceptance(), delivered_scope=("A later delivered scope",)
            ),
        ).assign_to(MAP_ID)
        tracker.reports.append(
            TrackerPMReportRecord(
                report=changed_report,
                tracker_record_id="IC_pm_concurrent_2",
                tracker_record_url=f"{ISSUE_URL}#issuecomment-pm-concurrent-2",
            )
        )

    publisher.before_execute = mutate_authority
    with pytest.raises(TrackerError):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id=f"changed-{change}-closeout",
        )

    assert tracker.issue.state == "open"
    assert tracker.effect_order == ["publication_evidence"]


def test_partial_failure_never_projects_publisher_secret_material(tmp_path):
    publisher = FakePublisher()
    publisher.failure = PublicationPartialFailure(
        "access_token=github_pat_never-copy-me; remote ref may have changed"
    )
    application, tracker = _governed_application(tmp_path, publisher=publisher)
    packet = _request_and_approve_publication(application)

    with pytest.raises(PublicationRepairRequired) as captured:
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id="secret-redaction-action",
        )

    safe_reason = (
        "Privileged publisher reported a partial remote failure; inspect the "
        "isolated publisher audit boundary."
    )
    assert captured.value.reason == safe_reason
    assert tracker.publications[0].record.reason == safe_reason
    assert "github_pat" not in str(application.map_detail(map_id=MAP_ID)).lower()


class SimulatedPublicationCrash(BaseException):
    pass


def test_restart_derives_rejection_stage_and_pm_resume_after_decision_crash(tmp_path):
    coordinator = FakeCoordinator()

    def crash_before_decision_projection(point, intent):
        event = intent.payload.get("event")
        if (
            point == "after_confirmation"
            and isinstance(event, dict)
            and event.get("event_type") == "rejected"
        ):
            raise SimulatedPublicationCrash()

    application, tracker = _governed_application(
        tmp_path,
        coordinator=coordinator,
        outbox_crash_injector=crash_before_decision_projection,
    )
    packet = _publication_packet(
        request_id="publish-rejection-crash",
        acceptance=_acceptance(),
    )
    application.request_approval(
        map_id=MAP_ID,
        request_identity=CEO_IDENTITY,
        packet=packet,
    )
    calls_before_decision = len(coordinator.calls)

    with pytest.raises(SimulatedPublicationCrash):
        application.decide_approval(
            map_id=MAP_ID,
            request_id=packet.request_id,
            actor_identity=_chairman_identity(),
            decision="rejected",
            note="Add the rollback proof before publication.",
        )

    restarted = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data",
        tracker=tracker,
        profile_name="ceo",
        clock=lambda: datetime(2026, 8, 24, 8, 2, tzinfo=timezone.utc),
        authority_policy=AuthorityEnvelopePolicy.from_settings(
            {"chairman_actor_ids": ["basic:chairman"]}
        ),
        coordinator_resume=coordinator,
    )

    recovered = restarted.recover_outbox()

    assert recovered["acceptance_decisions"] == [
        {
            "map_id": MAP_ID,
            "request_id": packet.request_id,
            "decision": "rejected",
            "resumed": True,
        }
    ]
    assert restarted.map_detail(map_id=MAP_ID)["stage"] == "delivery"
    assert len(coordinator.calls) == calls_before_decision + 1
    assert coordinator.calls[-1]["turn_id"] == (
        f"acceptance:{packet.request_id}:rejected"
    )


def test_restart_reads_remote_evidence_and_finishes_closeout_without_republishing(
    tmp_path,
):
    publisher = FakePublisher()

    def crash_after_publication(point, intent):
        if point == "after_external_call" and intent.effect_type == "publisher.execute":
            raise SimulatedPublicationCrash()

    application, tracker = _governed_application(
        tmp_path,
        publisher=publisher,
        outbox_crash_injector=crash_after_publication,
    )
    packet = _request_and_approve_publication(application)
    with pytest.raises(SimulatedPublicationCrash):
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id="restart-publication-action",
        )

    assert len(publisher.calls) == 1
    assert tracker.issue.state == "open"
    restarted = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data",
        tracker=tracker,
        session_runner=PublicationSessionRunner(),
        profile_name="ceo",
        clock=lambda: datetime(2026, 8, 24, 8, 2, tzinfo=timezone.utc),
        authority_policy=AuthorityEnvelopePolicy.from_settings(
            {"chairman_actor_ids": ["basic:chairman"]}
        ),
        publisher=publisher,
    )

    recovered = restarted.recover_outbox()

    assert recovered["processed_count"] == 4
    assert recovered["outcomes"][0]["reconciled_by_readback"] is True
    assert len(publisher.calls) == 1
    assert tracker.effect_order == [
        "publication_evidence",
        "publication_evidence",
        "issue_close",
    ]
    assert [item.record.status for item in tracker.publications] == [
        "repair_required",
        "succeeded",
    ]
    assert restarted.map_detail(map_id=MAP_ID)["stage"] == "done"


def test_direct_terminal_transition_is_denied_and_approved_cancel_is_not_planned(
    tmp_path,
):
    application, tracker = _governed_application(tmp_path)

    for stage in ("done", "cancelled"):
        with pytest.raises(MapTransitionError, match="terminal stages are derived"):
            application.transition_map(
                map_id=MAP_ID,
                expected_stage="acceptance",
                requested_stage=stage,
            )
    assert tracker.issue.state == "open"

    packet = _cancellation_packet()
    application.request_approval(
        map_id=MAP_ID,
        request_identity=CEO_IDENTITY,
        packet=packet,
    )
    application.decide_approval(
        map_id=MAP_ID,
        request_id=packet.request_id,
        actor_identity=_chairman_identity(),
        decision="approved",
        note="Close this Map as not planned.",
    )

    result = application.cancel_map(
        map_id=MAP_ID,
        actor_identity=_chairman_identity(),
        approval_request_id=packet.request_id,
        mutation_id="cancel-atlas-not-planned-action",
    )

    assert result["stage"] == "cancelled"
    assert tracker.issue.state_reason == "not_planned"
    assert tracker.effect_order == ["issue_close"]
    assert tracker.publications == []

    repeated = application.cancel_map(
        map_id=MAP_ID,
        actor_identity=_chairman_identity(),
        approval_request_id=packet.request_id,
        mutation_id="cancel-atlas-not-planned-action",
    )
    assert repeated["stage"] == "cancelled"
    assert tracker.effect_order == ["issue_close"]

    rebuilt = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "rebuilt-cancellation",
        tracker=tracker,
    )
    project = rebuilt.configure_project(project_url=PROJECT_URL)
    rebound = rebuilt.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    assert rebound["stage"] == "cancelled"


def test_not_planned_close_revalidates_consumed_authority_immediately_before_close(
    tmp_path,
):
    crashed = False

    def interrupt_first_close(point, intent):
        nonlocal crashed
        if (
            not crashed
            and point == "before_external_call"
            and intent.effect_type == "tracker.issue-close"
        ):
            crashed = True
            raise RuntimeError("injected close interruption")

    application, tracker = _governed_application(
        tmp_path,
        outbox_crash_injector=interrupt_first_close,
    )
    packet = _cancellation_packet()
    application.request_approval(
        map_id=MAP_ID,
        request_identity=CEO_IDENTITY,
        packet=packet,
    )
    application.decide_approval(
        map_id=MAP_ID,
        request_id=packet.request_id,
        actor_identity=_chairman_identity(),
        decision="approved",
        note="Close this Map as not planned.",
    )
    mutation_id = "cancel-revalidation-action"
    with pytest.raises(TrackerError, match="injected close interruption"):
        application.cancel_map(
            map_id=MAP_ID,
            actor_identity=_chairman_identity(),
            approval_request_id=packet.request_id,
            mutation_id=mutation_id,
        )

    tracker.approvals = [
        record for record in tracker.approvals if record.event.event_type != "consumed"
    ]
    application._storage.set_project_reachability(
        project_id=PROJECT_ID,
        source="tracker",
        state="healthy",
        reason=None,
        changed_at="2026-08-24T08:01:00Z",
    )
    with pytest.raises(TrackerError, match="immutable consumption event"):
        application.cancel_map(
            map_id=MAP_ID,
            actor_identity=_chairman_identity(),
            approval_request_id=packet.request_id,
            mutation_id=mutation_id,
        )

    assert tracker.issue.state == "open"
    assert (
        application.outbox_status(effect_id=f"tracker-close:{mutation_id}:not-planned")[
            "state"
        ]
        == "terminal"
    )


@pytest.mark.parametrize("state_reason", ["completed", "not_planned"])
def test_authoritative_refresh_rejects_direct_external_closure_without_lineage(
    tmp_path,
    state_reason,
):
    application, tracker = _governed_application(tmp_path)
    tracker.issue = replace(
        tracker.issue,
        state="closed",
        state_reason=state_reason,
        labels=("map",),
    )

    refreshed = application.refresh()

    assert refreshed["maps"][0]["stage"] == "acceptance"
    assert refreshed["projects"][0]["authority"]["state"] == "stale"


def test_completed_closeout_rejects_incident_recorded_after_success(tmp_path):
    _application, tracker = _governed_application(tmp_path)
    evidence = RemotePublicationEvidence(
        action_id="late-incident-action",
        revision=REVISION,
        action="push",
        target={"repository": "acme/atlas", "ref": "refs/heads/main"},
        provider="fake-github",
        remote_id="remote-late-incident",
        remote_url=f"https://github.com/acme/atlas/commit/{REVISION}",
        published_at="2026-08-24T08:01:00Z",
    )
    succeeded = PublicationRecord(
        record_id="publication:late-incident-action:succeeded",
        action_id=evidence.action_id,
        map_id=MAP_ID,
        approval_request_id="publish-atlas-main-a",
        status="succeeded",
        revision=REVISION,
        action=evidence.action,
        target=evidence.target,
        occurred_at="2026-08-24T08:01:00Z",
        evidence=evidence,
    )
    incident = PublicationRecord(
        record_id="publication:late-incident-action:repair-required",
        action_id=evidence.action_id,
        map_id=MAP_ID,
        approval_request_id="publish-atlas-main-a",
        status="repair_required",
        revision=REVISION,
        action=evidence.action,
        target=evidence.target,
        occurred_at="2026-08-24T08:02:00Z",
        reason="A later provider observation made the outcome uncertain.",
    )
    tracker.append_publication_record(ISSUE_URL, issue_id=MAP_ID, record=succeeded)
    tracker.append_publication_record(ISSUE_URL, issue_id=MAP_ID, record=incident)
    intent = OutboxIntent(
        effect_id="tracker-close:late-incident-action:completed",
        effect_type=TRACKER_ISSUE_CLOSE,
        map_id=MAP_ID,
        payload_hash="sha256:test",
        payload={
            "issue_url": ISSUE_URL,
            "issue_id": MAP_ID,
            "state_reason": "completed",
            "publication_record_id": succeeded.record_id,
            "acceptance_report_id": "acceptance-publication-1",
            "expected_stage": "acceptance",
        },
        state="pending",
        owner_id=None,
        lease_expires_at=None,
        attempt_count=0,
        next_attempt_at=None,
        created_at="2026-08-24T08:02:00Z",
        updated_at="2026-08-24T08:02:00Z",
        acknowledged_at=None,
        acknowledgment=None,
        last_error_type=None,
        last_error_message=None,
        terminal_reason=None,
    )

    with pytest.raises(EffectTerminalError, match="unresolved publication incident"):
        TrackerEffectAdapter(tracker).readback(intent)

    assert tracker.issue.state == "open"


def test_approved_but_unconsumed_cancellation_cannot_authorize_direct_close(tmp_path):
    application, tracker = _governed_application(tmp_path)
    packet = _cancellation_packet()
    application.request_approval(
        map_id=MAP_ID,
        request_identity=CEO_IDENTITY,
        packet=packet,
    )
    application.decide_approval(
        map_id=MAP_ID,
        request_id=packet.request_id,
        actor_identity=_chairman_identity(),
        decision="approved",
        note="Cancellation is approved only through the governed action.",
    )
    tracker.issue = replace(
        tracker.issue,
        state="closed",
        state_reason="not_planned",
        labels=("map",),
    )

    refreshed = application.refresh()

    assert refreshed["maps"][0]["stage"] == "acceptance"
    assert refreshed["projects"][0]["authority"]["state"] == "stale"


@pytest.mark.parametrize(
    ("invalidated_by", "reason"),
    [("revocation", "approval_revoked"), ("expiry", "approval_expired")],
)
def test_revoked_or_expired_publication_approval_cannot_be_replayed(
    tmp_path,
    invalidated_by,
    reason,
):
    now = [datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)]
    publisher = FakePublisher()
    application, tracker = _governed_application(
        tmp_path,
        publisher=publisher,
        clock=lambda: now[0],
    )
    packet = _request_and_approve_publication(application)
    if invalidated_by == "revocation":
        application.revoke_approval(
            map_id=MAP_ID,
            request_id=packet.request_id,
            actor_identity=_chairman_identity(),
            note="Publication authority withdrawn.",
        )
    else:
        now[0] += timedelta(days=2)

    with pytest.raises(ApprovalEnforcementError) as captured:
        application.publish_map(
            map_id=MAP_ID,
            approval_request_id=packet.request_id,
            mutation_id=f"{invalidated_by}-publication-action",
        )

    assert captured.value.reason == reason
    assert publisher.calls == []
    assert tracker.issue.state == "open"


@pytest.mark.parametrize("decision", ["rejected", "revision"])
def test_rejected_acceptance_returns_to_delivery_with_requested_changes(
    tmp_path,
    decision,
):
    coordinator = FakeCoordinator()
    application, tracker = _governed_application(tmp_path, coordinator=coordinator)
    packet = _publication_packet(
        request_id=f"publish-atlas-{decision}",
        acceptance=_acceptance(),
    )
    application.request_approval(
        map_id=MAP_ID,
        request_identity=CEO_IDENTITY,
        packet=packet,
    )

    application.decide_approval(
        map_id=MAP_ID,
        request_id=packet.request_id,
        actor_identity=_chairman_identity(),
        decision=decision,
        note="Add rollback automation before asking again.",
    )

    detail = application.map_detail(map_id=MAP_ID)
    assert tracker.issue.state == "open"
    assert detail["stage"] == "delivery"
    assert detail["acceptance_outcome"] == {
        "state": "changes_requested",
        "request_id": packet.request_id,
        "decision": decision,
        "requested_changes": "Add rollback automation before asking again.",
    }
    assert coordinator.calls[-1]["turn_id"] == (
        f"acceptance:{packet.request_id}:{decision}"
    )
    assert "Add rollback automation" in coordinator.calls[-1]["content"]

    application.report_pm(
        request_identity=PM_IDENTITY,
        report=PMReportDraft(
            record_id="acceptance-publication-2",
            report_type="acceptance",
            summary="Requested rollback automation was added and revalidated.",
            timestamp="2026-08-24T08:10:00Z",
            evidence=("Rollback validation passed.",),
            acceptance=_acceptance(),
        ),
    )
    revised_detail = application.map_detail(map_id=MAP_ID)
    assert revised_detail["stage"] == "acceptance"
    assert "acceptance_outcome" not in revised_detail
    with pytest.raises(
        ApprovalEnforcementError, match="publication_acceptance_mismatch"
    ):
        application.request_approval(
            map_id=MAP_ID,
            request_identity=CEO_IDENTITY,
            packet=_publication_packet(
                request_id=f"stale-{decision}-packet",
                acceptance=_acceptance(),
            ),
        )
    accepted = application.request_approval(
        map_id=MAP_ID,
        request_identity=CEO_IDENTITY,
        packet=_publication_packet(
            request_id=f"replacement-{decision}-packet",
            acceptance=_acceptance(),
            acceptance_report_id="acceptance-publication-2",
        ),
    )
    assert accepted["approval"]["status"] == "pending"


def test_acceptance_requires_structured_evidence_before_tracker_mutation(tmp_path):
    application, tracker = _application(tmp_path)

    with pytest.raises(ValueError, match="structured acceptance evidence"):
        PMReportDraft(
            record_id="legacy-acceptance",
            report_type="acceptance",
            summary="Evidence strings alone are not actionable acceptance.",
            timestamp="2026-08-24T08:00:00Z",
            evidence=("Tests passed.",),
        )

    assert tracker.reports == []
    assert application.map_detail(map_id=MAP_ID)["stage"] == "delivery"


def test_legacy_acceptance_history_remains_readable_but_not_actionable():
    historical = PMReport.from_payload(
        {
            "assignment_map_id": MAP_ID,
            "record_id": "legacy-acceptance",
            "type": "acceptance",
            "summary": "Historical unstructured acceptance.",
            "timestamp": "2026-08-20T08:00:00Z",
            "evidence": ["Legacy validation string."],
        }
    )

    assert historical.content.acceptance is None
    with pytest.raises(ValueError, match="structured acceptance evidence"):
        PMReportDraft(
            record_id="new-unstructured-acceptance",
            report_type="acceptance",
            summary="New reports must use the structured contract.",
            timestamp="2026-08-24T08:00:00Z",
            evidence=("Validation string.",),
        )


def test_latest_confirmed_acceptance_wins_even_with_an_earlier_reported_timestamp(
    tmp_path,
):
    application, _tracker = _governed_application(tmp_path)
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="backdated-acceptance-turn",
    )
    changed = replace(_acceptance(), delivered_scope=("Later confirmed scope",))
    application.report_pm(
        request_identity=PM_IDENTITY,
        report=PMReportDraft(
            record_id="acceptance-publication-2",
            report_type="acceptance",
            summary="This was confirmed later despite its source timestamp.",
            timestamp="2026-08-23T08:00:00Z",
            evidence=("Later confirmation is authoritative.",),
            acceptance=changed,
        ),
    )

    detail = application.map_detail(map_id=MAP_ID)
    assert detail["acceptance"]["report"]["id"] == "acceptance-publication-2"
    requested = application.request_approval(
        map_id=MAP_ID,
        request_identity=CEO_IDENTITY,
        packet=_publication_packet(
            request_id="backdated-latest-publication",
            acceptance=changed,
            acceptance_report_id="acceptance-publication-2",
        ),
    )
    assert requested["approval"]["status"] == "pending"
