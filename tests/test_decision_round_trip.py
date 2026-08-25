from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from map_governance import (
    AuthorityEnvelopePolicy,
    DecisionResumePendingError,
    GovernanceAuthorizationError,
    GovernanceRequestIdentity,
    MapGovernanceApplication,
    PMDecisionResponse,
    PMReportDraft,
)
from map_governance.approvals import ApprovalHistoryEvent, GovernanceActorIdentity
from map_governance.reports import PMReport, TrackerPMReportRecord
from map_governance.sessions import CanonicalSession
from map_governance.tracker import (
    StructuredDecision,
    TrackerApprovalRecord,
    TrackerDecisionRecord,
    TrackerIssue,
    TrackerProject,
)


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugin"
MAP_ID = "I_atlas_round_trip"
ISSUE_URL = "https://github.com/acme/atlas/issues/51"
PROJECT_URL = "https://github.com/orgs/acme/projects/7"
CEO_IDENTITY = GovernanceRequestIdentity("ceo", "ceo-session-atlas")
PM_IDENTITY = GovernanceRequestIdentity("pm", "pm-session-atlas")


class RoundTripTracker:
    def __init__(self) -> None:
        self.issue = TrackerIssue(
            id=MAP_ID,
            repository="acme/atlas",
            number=51,
            title="Atlas decision round trip",
            url=ISSUE_URL,
            state="open",
            state_reason=None,
            labels=("map", "map-stage/delivery"),
        )
        self.pm_reports: list[TrackerPMReportRecord] = []
        self.decisions: list[TrackerDecisionRecord] = []
        self.approvals: list[TrackerApprovalRecord] = []
        self.effect_order: list[str] = []

    def get_project(self, url: str) -> TrackerProject:
        return TrackerProject(
            id="PVT_acme_7",
            owner="acme",
            owner_type="organization",
            number=7,
            title="Acme portfolio",
            url=url,
        )

    def get_issue(self, url: str) -> TrackerIssue:
        assert url == ISSUE_URL
        return self.issue

    def transition_issue_stage(self, url, *, expected_stage, requested_stage):
        assert url == ISSUE_URL
        current = next(
            label.removeprefix("map-stage/")
            for label in self.issue.labels
            if label.startswith("map-stage/")
        )
        assert current == expected_stage
        self.issue = replace(
            self.issue,
            labels=("map", f"map-stage/{requested_stage}"),
        )
        self.effect_order.append(f"stage:{requested_stage}")
        return self.issue

    def list_pm_reports(self, url: str):
        return list(self.pm_reports)

    def append_pm_report(self, url: str, *, issue_id: str, report: PMReport):
        record = TrackerPMReportRecord(
            report=report,
            tracker_record_id=f"IC_pm_{len(self.pm_reports) + 1}",
            tracker_record_url=f"{ISSUE_URL}#issuecomment-pm-{len(self.pm_reports) + 1}",
        )
        self.pm_reports.append(record)
        self.effect_order.append("tracker:question")
        return record

    def list_decisions(self, url: str):
        return list(self.decisions)

    def append_decision(self, url: str, *, issue_id: str, decision: StructuredDecision):
        record = TrackerDecisionRecord(
            decision=decision,
            tracker_record_id=f"IC_decision_{len(self.decisions) + 1}",
            tracker_record_url=(
                f"{ISSUE_URL}#issuecomment-decision-{len(self.decisions) + 1}"
            ),
        )
        self.decisions.append(record)
        self.effect_order.append("tracker:decision")
        return record

    def list_approval_events(self, url: str):
        return list(self.approvals)

    def append_approval_event(
        self, url: str, *, issue_id: str, event: ApprovalHistoryEvent
    ):
        record = TrackerApprovalRecord(
            event=event,
            tracker_record_id=f"IC_approval_{len(self.approvals) + 1}",
            tracker_record_url=(
                f"{ISSUE_URL}#issuecomment-approval-{len(self.approvals) + 1}"
            ),
        )
        self.approvals.append(record)
        self.effect_order.append(f"tracker:approval:{event.event_type}")
        return record


class RoundTripSessionRunner:
    def __init__(self) -> None:
        self.session = CanonicalSession(
            root_session_id="ceo-round-trip-root",
            live_session_id="ceo-round-trip-root",
            title="uninitialized",
            last_activity_at="2026-08-23T09:00:00Z",
        )
        self.markers: set[str] = set()
        self.turns: list[str] = []

    def find_exact(self, *, title):
        return [self.session] if self.session.title == title else []

    def initialize(self, session, **_kwargs):
        return session

    def mint(self, **kwargs):
        self.session = replace(self.session, title=kwargs["title"])
        return self.session

    def resolve(self, *, root_session_id):
        return self.session if root_session_id == self.session.root_session_id else None

    def load_skill(self, session, **_kwargs):
        return session

    def has_resume_marker(self, *, root_session_id, idempotency_key):
        return idempotency_key in self.markers

    def resume_once(self, *, root_session_id, content, idempotency_key):
        if idempotency_key not in self.markers:
            self.markers.add(idempotency_key)
            self.turns.append(content)
        return self.session


class RoundTripCoordinator:
    def __init__(self, tracker: RoundTripTracker) -> None:
        self.tracker = tracker
        self.markers = {}
        self.calls = []
        self.fail_next_content_resume = False

    def readback(self, *, map_id, turn_id):
        return self.markers.get((map_id, turn_id))

    def resume(
        self,
        *,
        map_id,
        profile_name,
        session_id,
        coordinator_id,
        turn_id,
        content,
    ):
        if turn_id.startswith("decision:"):
            assert self.tracker.decisions or self.tracker.approvals
            if self.fail_next_content_resume:
                self.fail_next_content_resume = False
                raise RuntimeError("PM process is restarting")
            self.calls.append({"turn_id": turn_id, "content": content})
        self.markers[(map_id, turn_id)] = {
            "map_id": map_id,
            "turn_id": turn_id,
            "correlation_id": "compatibility-choice-001",
        }


def _application(
    tmp_path,
    *,
    decision_class="product",
    blocking=False,
    outbox_crash_injector=None,
):
    tracker = RoundTripTracker()
    sessions = RoundTripSessionRunner()
    coordinator = RoundTripCoordinator(tracker)
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
        tracker=tracker,
        session_runner=sessions,
        coordinator_resume=coordinator,
        profile_name="ceo",
        authority_policy=AuthorityEnvelopePolicy.from_settings(
            {"chairman_actor_ids": ["chairman-1"]}
        ),
        clock=lambda: datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc),
        outbox_crash_injector=outbox_crash_injector,
    )
    project = application.configure_project(project_url=PROJECT_URL)
    application.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    opened = application.open_map(map_id=MAP_ID)
    application.assign_pm(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="turn-question",
    )
    application.report_pm(
        request_identity=PM_IDENTITY,
        report=PMReportDraft(
            record_id="pm-question-record-001",
            report_type="question",
            summary="Should legacy aliases remain supported?",
            timestamp="2026-08-23T10:00:00Z",
            blocking=blocking,
            continuation_requirement="Choose one compatibility policy.",
            correlation_id="compatibility-choice-001",
            decision_class=decision_class,
            scope={"map_id": MAP_ID, "area": "compatibility"},
            evidence=("Two supported clients still send aliases.",),
            options=(
                (
                    "Fund the compatibility window",
                    "Do not fund the compatibility window",
                )
                if decision_class == "budget_increase"
                else ("Keep aliases", "Remove aliases")
            ),
        ),
    )
    return application, tracker, sessions, coordinator, opened


def test_ceo_authority_answer_commits_once_before_pm_resume(tmp_path):
    application, tracker, sessions, coordinator, opened = _application(tmp_path)
    response = PMDecisionResponse(
        correlation_id="compatibility-choice-001",
        recommendation="Keep aliases",
        rationale="Compatibility outweighs the small maintenance cost.",
        cost_risk="One release of deprecation maintenance.",
        decision_payload={"selected_option": "Keep aliases"},
        outcome="continue",
        timestamp="2026-08-23T10:01:00Z",
    )

    first = application.answer_pm_question(
        map_id=MAP_ID,
        request_identity=GovernanceRequestIdentity(
            "ceo", opened["ceo_session"]["live_session_id"]
        ),
        response=response,
    )
    repeated = application.answer_pm_question(
        map_id=MAP_ID,
        request_identity=GovernanceRequestIdentity(
            "ceo", opened["ceo_session"]["live_session_id"]
        ),
        response=response,
    )

    assert first["route"] == "ceo_decision"
    assert repeated["idempotent"] is True
    assert len(sessions.turns) == 2  # stable CEO Skill load plus one PM question turn
    assert len(tracker.decisions) == 1
    assert tracker.approvals == []
    assert len(coordinator.calls) == 1
    assert tracker.effect_order.index("tracker:decision") < len(tracker.effect_order)
    assert "IC_decision_1" in coordinator.calls[0]["content"]
    assert "compatibility-choice-001" in coordinator.calls[0]["content"]


def test_chairman_approve_reject_and_revision_commit_ledger_before_pm_resume(
    tmp_path,
):
    for decision in ("approved", "rejected", "revision"):
        case_root = tmp_path / decision
        application, tracker, _sessions, coordinator, opened = _application(
            case_root,
            decision_class="budget_increase",
            blocking=True,
        )
        response = PMDecisionResponse(
            correlation_id="compatibility-choice-001",
            recommendation="Fund the compatibility window",
            rationale="The migration needs one additional compatibility release.",
            cost_risk="The requested increase is 5000 units.",
            decision_payload={"amount": 5_000, "currency": "USD"},
            outcome="continue",
            timestamp="2026-08-23T10:01:00Z",
        )

        routed = application.answer_pm_question(
            map_id=MAP_ID,
            request_identity=GovernanceRequestIdentity(
                "ceo", opened["ceo_session"]["live_session_id"]
            ),
            response=response,
        )

        assert routed["route"] == "chairman_approval"
        assert routed["approval"]["status"] == "pending"
        assert tracker.decisions == []
        assert coordinator.calls == []
        decided = application.decide_approval(
            map_id=MAP_ID,
            request_id="compatibility-choice-001",
            actor_identity=GovernanceActorIdentity(
                "chairman", "ceo", "chairman-1", "dashboard-session"
            ),
            decision=decision,
            note=f"Chairman chose {decision}.",
        )
        repeated = application.decide_approval(
            map_id=MAP_ID,
            request_id="compatibility-choice-001",
            actor_identity=GovernanceActorIdentity(
                "chairman", "ceo", "chairman-1", "dashboard-session"
            ),
            decision=decision,
            note=f"Chairman chose {decision}.",
        )

        assert decided["approval"]["status"] == decision
        assert repeated["idempotent"] is True
        assert len(coordinator.calls) == 1
        decision_record = decided["approval"]["tracker"]["decision"]
        assert decision_record["id"] in coordinator.calls[0]["content"]
        assert "compatibility-choice-001" in coordinator.calls[0]["content"]
        assert tracker.effect_order.index(f"tracker:approval:{decision}") < len(
            tracker.effect_order
        )
        assert application.map_detail(map_id=MAP_ID)["stage"] == "decision"


def test_pm_rereads_authoritative_answer_and_acknowledges_before_continuing(
    tmp_path,
):
    application, tracker, _sessions, _coordinator, opened = _application(
        tmp_path,
        blocking=True,
    )
    response = PMDecisionResponse(
        correlation_id="compatibility-choice-001",
        recommendation="Keep aliases",
        rationale="Compatibility outweighs the maintenance cost.",
        cost_risk="One release of maintenance.",
        decision_payload={"selected_option": "Keep aliases"},
        outcome="continue",
        timestamp="2026-08-23T10:01:00Z",
    )
    application.answer_pm_question(
        map_id=MAP_ID,
        request_identity=GovernanceRequestIdentity(
            "ceo", opened["ceo_session"]["live_session_id"]
        ),
        response=response,
    )

    acknowledged = application.acknowledge_pm_decision(
        request_identity=PM_IDENTITY,
        correlation_id="compatibility-choice-001",
    )
    repeated = application.acknowledge_pm_decision(
        request_identity=PM_IDENTITY,
        correlation_id="compatibility-choice-001",
    )

    assert acknowledged["continuation"] == "continue"
    assert acknowledged["tracker"]["id"] == "IC_decision_1"
    assert repeated["idempotent"] is True
    assert application.map_detail(map_id=MAP_ID)["stage"] == "delivery"
    state = application.pm_state(request_identity=PM_IDENTITY)
    assert state["assignment"]["coordinator"]["state"] == "active"
    assert state["decision_acknowledgments"] == [
        {
            "correlation_id": "compatibility-choice-001",
            "outcome": "continue",
            "turn_id": "decision:compatibility-choice-001:continue",
            "tracker": {
                "id": "IC_decision_1",
                "url": f"{ISSUE_URL}#issuecomment-decision-1",
            },
            "acknowledged_at": "2026-08-23T10:00:00Z",
        }
    ]
    assert tracker.effect_order[-1] == "stage:delivery"


def test_rejected_or_revision_answer_is_acknowledged_but_stays_blocked(tmp_path):
    for decision in ("rejected", "revision"):
        application, _tracker, _sessions, _coordinator, opened = _application(
            tmp_path / decision,
            decision_class="budget_increase",
            blocking=True,
        )
        application.answer_pm_question(
            map_id=MAP_ID,
            request_identity=GovernanceRequestIdentity(
                "ceo", opened["ceo_session"]["live_session_id"]
            ),
            response=PMDecisionResponse(
                correlation_id="compatibility-choice-001",
                recommendation="Fund the compatibility window",
                rationale="The migration asks for a budget increase.",
                cost_risk="The requested increase is 5000 units.",
                decision_payload={"amount": 5_000},
                outcome="continue",
                timestamp="2026-08-23T10:01:00Z",
            ),
        )
        application.decide_approval(
            map_id=MAP_ID,
            request_id="compatibility-choice-001",
            actor_identity=GovernanceActorIdentity(
                "chairman", "ceo", "chairman-1", "dashboard-session"
            ),
            decision=decision,
            note=f"Chairman chose {decision}.",
        )

        acknowledged = application.acknowledge_pm_decision(
            request_identity=PM_IDENTITY,
            correlation_id="compatibility-choice-001",
        )

        assert acknowledged["continuation"] == "blocked"
        assert application.map_detail(map_id=MAP_ID)["stage"] == "decision"
        assert (
            application.pm_state(request_identity=PM_IDENTITY)["assignment"][
                "coordinator"
            ]["state"]
            == "idle"
        )


def test_pm_restart_before_resume_recovers_without_duplicate_decision_or_prompt(
    tmp_path,
):
    application, tracker, sessions, coordinator, opened = _application(tmp_path)
    coordinator.fail_next_content_resume = True
    response = PMDecisionResponse(
        correlation_id="compatibility-choice-001",
        recommendation="Keep aliases",
        rationale="Compatibility remains material.",
        cost_risk="One release of maintenance.",
        decision_payload={"selected_option": "Keep aliases"},
        outcome="continue",
        timestamp="2026-08-23T10:01:00Z",
    )

    with pytest.raises(DecisionResumePendingError, match="PM process is restarting"):
        application.answer_pm_question(
            map_id=MAP_ID,
            request_identity=GovernanceRequestIdentity(
                "ceo", opened["ceo_session"]["live_session_id"]
            ),
            response=response,
        )

    assert len(tracker.decisions) == 1
    assert coordinator.calls == []
    restarted = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
        tracker=tracker,
        session_runner=sessions,
        coordinator_resume=coordinator,
        profile_name="ceo",
        clock=lambda: datetime(2026, 8, 23, 10, 2, tzinfo=timezone.utc),
    )

    recovered = restarted.recover_outbox()
    repeated = restarted.answer_pm_question(
        map_id=MAP_ID,
        request_identity=GovernanceRequestIdentity(
            "ceo", opened["ceo_session"]["live_session_id"]
        ),
        response=response,
    )

    assert recovered["processed_count"] == 1
    assert len(tracker.decisions) == 1
    assert len(coordinator.calls) == 1
    assert repeated["idempotent"] is True
    assert (
        restarted.pm_state(request_identity=PM_IDENTITY)["assignment"]["coordinator"][
            "state"
        ]
        == "active"
    )


class SimulatedDecisionCompletionCrash(BaseException):
    pass


def test_decision_completion_persists_resume_intent_before_parent_ack(tmp_path):
    def crash_after_completion(point, intent):
        if point == "after_completion" and intent.effect_type == "tracker.decision":
            raise SimulatedDecisionCompletionCrash()

    application, tracker, sessions, coordinator, opened = _application(
        tmp_path,
        outbox_crash_injector=crash_after_completion,
    )
    response = PMDecisionResponse(
        correlation_id="compatibility-choice-001",
        recommendation="Keep aliases",
        rationale="Compatibility remains material.",
        cost_risk="One release of maintenance.",
        decision_payload={"selected_option": "Keep aliases"},
        outcome="continue",
        timestamp="2026-08-23T10:01:00Z",
    )

    with pytest.raises(SimulatedDecisionCompletionCrash):
        application.answer_pm_question(
            map_id=MAP_ID,
            request_identity=GovernanceRequestIdentity(
                "ceo", opened["ceo_session"]["live_session_id"]
            ),
            response=response,
        )

    resume_effect = application.outbox_status(
        effect_id=(
            f"coordinator-resume:{MAP_ID}:decision:compatibility-choice-001:continue"
        )
    )
    assert resume_effect["state"] == "pending"
    restarted = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
        tracker=tracker,
        session_runner=sessions,
        coordinator_resume=coordinator,
        profile_name="ceo",
        clock=lambda: datetime(2026, 8, 23, 10, 2, tzinfo=timezone.utc),
    )

    repeated = restarted.answer_pm_question(
        map_id=MAP_ID,
        request_identity=GovernanceRequestIdentity(
            "ceo", opened["ceo_session"]["live_session_id"]
        ),
        response=response,
    )

    assert repeated["decision"]["decision_id"] == "compatibility-choice-001"
    assert len(tracker.decisions) == 1
    assert len(coordinator.calls) == 1


def test_chairman_completion_persists_resume_intent_before_parent_ack(tmp_path):
    def crash_after_completion(point, intent):
        completion = intent.payload.get("completion")
        if (
            point == "after_completion"
            and intent.effect_type == "tracker.approval-event"
            and isinstance(completion, dict)
            and completion.get("operation") == "decision"
        ):
            raise SimulatedDecisionCompletionCrash()

    application, tracker, sessions, coordinator, opened = _application(
        tmp_path,
        decision_class="budget_increase",
        blocking=True,
        outbox_crash_injector=crash_after_completion,
    )
    application.answer_pm_question(
        map_id=MAP_ID,
        request_identity=GovernanceRequestIdentity(
            "ceo", opened["ceo_session"]["live_session_id"]
        ),
        response=PMDecisionResponse(
            correlation_id="compatibility-choice-001",
            recommendation="Fund the compatibility window",
            rationale="The migration needs one compatibility release.",
            cost_risk="The requested increase is 5000 units.",
            decision_payload={"amount": 5_000},
            outcome="continue",
            timestamp="2026-08-23T10:01:00Z",
        ),
    )

    with pytest.raises(SimulatedDecisionCompletionCrash):
        application.decide_approval(
            map_id=MAP_ID,
            request_id="compatibility-choice-001",
            actor_identity=GovernanceActorIdentity(
                "chairman", "ceo", "chairman-1", "dashboard-session"
            ),
            decision="approved",
            note="Chairman approved the exact request.",
        )

    resume_effect = application.outbox_status(
        effect_id=(
            f"coordinator-resume:{MAP_ID}:decision:compatibility-choice-001:continue"
        )
    )
    assert resume_effect["state"] == "pending"
    restarted = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
        tracker=tracker,
        session_runner=sessions,
        coordinator_resume=coordinator,
        profile_name="ceo",
        authority_policy=AuthorityEnvelopePolicy.from_settings(
            {"chairman_actor_ids": ["chairman-1"]}
        ),
        clock=lambda: datetime(2026, 8, 23, 10, 2, tzinfo=timezone.utc),
    )

    repeated = restarted.decide_approval(
        map_id=MAP_ID,
        request_id="compatibility-choice-001",
        actor_identity=GovernanceActorIdentity(
            "chairman", "ceo", "chairman-1", "dashboard-session"
        ),
        decision="approved",
        note="Chairman approved the exact request.",
    )

    assert repeated["idempotent"] is True
    assert len(coordinator.calls) == 1


def test_ack_retry_completes_stage_after_ack_was_already_committed(
    tmp_path, monkeypatch
):
    application, _tracker, _sessions, _coordinator, opened = _application(
        tmp_path,
        blocking=True,
    )
    application.answer_pm_question(
        map_id=MAP_ID,
        request_identity=GovernanceRequestIdentity(
            "ceo", opened["ceo_session"]["live_session_id"]
        ),
        response=PMDecisionResponse(
            correlation_id="compatibility-choice-001",
            recommendation="Keep aliases",
            rationale="Compatibility remains material.",
            cost_risk="One release of maintenance.",
            decision_payload={"selected_option": "Keep aliases"},
            outcome="continue",
            timestamp="2026-08-23T10:01:00Z",
        ),
    )
    transition = application.transition_map
    failures = 0

    def fail_once(**arguments):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise RuntimeError("process stopped after acknowledgment commit")
        return transition(**arguments)

    monkeypatch.setattr(application, "transition_map", fail_once)

    with pytest.raises(RuntimeError, match="after acknowledgment"):
        application.acknowledge_pm_decision(
            request_identity=PM_IDENTITY,
            correlation_id="compatibility-choice-001",
        )
    repeated = application.acknowledge_pm_decision(
        request_identity=PM_IDENTITY,
        correlation_id="compatibility-choice-001",
    )

    assert repeated["idempotent"] is True
    assert application.map_detail(map_id=MAP_ID)["stage"] == "delivery"


def test_low_level_decision_cannot_override_chairman_question_correlation(tmp_path):
    application, _tracker, _sessions, _coordinator, opened = _application(
        tmp_path,
        decision_class="budget_increase",
        blocking=True,
    )

    with pytest.raises(GovernanceAuthorizationError) as denied:
        application.record_decision(
            map_id=MAP_ID,
            request_identity=GovernanceRequestIdentity(
                "ceo", opened["ceo_session"]["live_session_id"]
            ),
            decision=StructuredDecision(
                decision_id="compatibility-choice-001",
                type="product",
                rationale="Attempt to bypass the chairman packet.",
                authority="ceo",
                affected_stage="delivery",
                timestamp="2026-08-23T10:01:00Z",
            ),
        )

    assert denied.value.reason == "pm_question_requires_answer_action"


def test_chairman_answer_wins_if_tracker_contains_a_conflicting_ceo_decision(
    tmp_path,
):
    application, tracker, _sessions, _coordinator, opened = _application(
        tmp_path,
        decision_class="budget_increase",
        blocking=True,
    )
    application.answer_pm_question(
        map_id=MAP_ID,
        request_identity=GovernanceRequestIdentity(
            "ceo", opened["ceo_session"]["live_session_id"]
        ),
        response=PMDecisionResponse(
            correlation_id="compatibility-choice-001",
            recommendation="Fund the compatibility window",
            rationale="The migration asks for a budget increase.",
            cost_risk="The requested increase is 5000 units.",
            decision_payload={"amount": 5_000},
            outcome="continue",
            timestamp="2026-08-23T10:01:00Z",
        ),
    )
    tracker.decisions.append(
        TrackerDecisionRecord(
            decision=StructuredDecision(
                decision_id="compatibility-choice-001",
                type="product",
                rationale="Conflicting lower-authority record.",
                authority="ceo",
                affected_stage="delivery",
                timestamp="2026-08-23T10:01:30Z",
                authority_context={"decision_payload": {"outcome": "continue"}},
            ),
            tracker_record_id="IC_conflicting_decision",
            tracker_record_url=f"{ISSUE_URL}#issuecomment-conflicting-decision",
        )
    )
    application.decide_approval(
        map_id=MAP_ID,
        request_id="compatibility-choice-001",
        actor_identity=GovernanceActorIdentity(
            "chairman", "ceo", "chairman-1", "dashboard-session"
        ),
        decision="rejected",
        note="Chairman rejected the request.",
    )

    before = application.board_events(cursor=0, limit=500)["latest_cursor"]
    acknowledged = application.acknowledge_pm_decision(
        request_identity=PM_IDENTITY,
        correlation_id="compatibility-choice-001",
    )
    events = application.board_events(cursor=before, limit=20)["events"]

    assert acknowledged["continuation"] == "blocked"
    assert application.map_detail(map_id=MAP_ID)["stage"] == "decision"
    assert any(event["type"] == "decision-ack.updated" for event in events)
