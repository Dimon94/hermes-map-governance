from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

from map_governance import (
    GovernanceAuthorizationError,
    GovernanceRequestIdentity,
    MapGovernanceApplication,
    PMReportConflict,
    TrackerPMReportConfirmationError,
)
from map_governance.reports import PMReport, PMReportDraft, TrackerPMReportRecord
from map_governance.sessions import CanonicalSession
from map_governance.tracker import TrackerError, TrackerIssue, TrackerProject


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MAP_ID = "I_atlas_41"
ISSUE_URL = "https://github.com/acme/atlas/issues/41"
OTHER_MAP_ID = "I_atlas_42"
OTHER_ISSUE_URL = "https://github.com/acme/atlas/issues/42"
PROJECT_URL = "https://github.com/orgs/acme/projects/7"
PM_IDENTITY = GovernanceRequestIdentity("pm", "pm-session-atlas")


class PMTracker:
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
        self.issues = {ISSUE_URL: self.issue}
        self.report_writes = 0
        self.confirm_report_writes = True
        self.fail_transitions = False
        self.before_report_append = None

    def get_project(self, url: str) -> TrackerProject:
        return TrackerProject(
            id="PVT_acme_7",
            owner="acme",
            owner_type="organization",
            number=7,
            title="Acme CEO portfolio",
            url=url,
        )

    def get_issue(self, url: str) -> TrackerIssue:
        return self.issues[url]

    def transition_issue_stage(self, *args, **kwargs) -> TrackerIssue:
        if self.fail_transitions:
            raise TrackerError("GitHub stage write failed")
        expected = kwargs["expected_stage"]
        requested = kwargs["requested_stage"]
        issue_url = args[0]
        issue = self.issues[issue_url]
        current = next(
            label.removeprefix("map-stage/")
            for label in issue.labels
            if label.startswith("map-stage/")
        )
        assert current == expected
        committed = replace(
            issue,
            labels=("map", f"map-stage/{requested}"),
        )
        self.issues[issue_url] = committed
        if issue_url == ISSUE_URL:
            self.issue = committed
        return committed

    def list_decisions(self, url: str) -> list:
        return []

    def list_pm_reports(self, url: str) -> list[TrackerPMReportRecord]:
        return list(self.reports) if url == ISSUE_URL else []

    def append_pm_report(
        self,
        url: str,
        *,
        issue_id: str,
        report: PMReport,
    ) -> TrackerPMReportRecord:
        assert url == ISSUE_URL
        assert issue_id == MAP_ID
        if self.before_report_append is not None:
            self.before_report_append(report)
        self.report_writes += 1
        record = TrackerPMReportRecord(
            report=report,
            tracker_record_id="IC_checkpoint_1",
            tracker_record_url=f"{ISSUE_URL}#issuecomment-checkpoint-1",
        )
        if self.confirm_report_writes:
            self.reports.append(record)
        return record


class ControllableCoordinatorResume:
    def __init__(self) -> None:
        self.markers = {}
        self.calls = []
        self.contents = []
        self.before_resume = None

    def readback(self, *, map_id: str, turn_id: str):
        return self.markers.get((map_id, turn_id))

    def resume(
        self,
        *,
        map_id: str,
        profile_name: str,
        session_id: str,
        coordinator_id: str,
        turn_id: str,
        content: str = "",
    ) -> None:
        if self.before_resume is not None:
            self.before_resume(map_id, turn_id)
        self.calls.append((map_id, turn_id))
        self.contents.append(content)
        self.markers[(map_id, turn_id)] = {
            "map_id": map_id,
            "turn_id": turn_id,
            "profile_name": profile_name,
            "session_id": session_id,
            "coordinator_id": coordinator_id,
        }


class SimulatedCoordinatorProcessCrash(BaseException):
    pass


class SimulatedPMReportProcessCrash(BaseException):
    pass


class QuestionSessionRunner:
    def __init__(self) -> None:
        self.session = CanonicalSession(
            root_session_id="ceo-root-atlas",
            live_session_id="ceo-root-atlas",
            title="Atlas CEO",
            last_activity_at="2026-08-23T09:00:00Z",
        )
        self.markers: set[str] = set()
        self.resume_calls: list[dict] = []
        self.before_resume = None

    def find_exact(self, *, title):
        return [self.session] if self.session.title == title else []

    def initialize(self, session, *, bootstrap, idempotency_key):
        return session

    def mint(self, **kwargs):
        self.session = replace(self.session, title=kwargs["title"])
        return self.session

    def resolve(self, *, root_session_id):
        return self.session if root_session_id == self.session.root_session_id else None

    def load_skill(self, session, *, content, idempotency_key):
        return session

    def has_resume_marker(self, *, root_session_id, idempotency_key):
        return idempotency_key in self.markers

    def resume_once(self, *, root_session_id, content, idempotency_key):
        if self.before_resume is not None:
            self.before_resume(content)
        if idempotency_key not in self.markers:
            self.resume_calls.append(
                {
                    "root_session_id": root_session_id,
                    "content": content,
                    "idempotency_key": idempotency_key,
                }
            )
            self.markers.add(idempotency_key)
        return self.session


def _application(tmp_path, tracker: PMTracker) -> MapGovernanceApplication:
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
        tracker=tracker,
        profile_name="pm",
        clock=lambda: datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc),
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
        turn_id="turn-checkpoint-1",
    )
    return application


def test_tracker_confirmed_checkpoint_projects_an_executive_summary_and_ends_idle(
    tmp_path,
):
    tracker = PMTracker()
    application = _application(tmp_path, tracker)

    result = application.report_pm(
        request_identity=PM_IDENTITY,
        report=PMReportDraft(
            record_id="pm-checkpoint-001",
            report_type="checkpoint",
            summary="The first delivery outcome is ready for executive review.",
            timestamp="2026-08-23T09:59:00Z",
        ),
    )

    assert result["idempotent"] is False
    assert result["report"]["assignment_map_id"] == MAP_ID
    assert result["coordinator"]["state"] == "idle"
    assert result["coordinator"]["last_outcome"] == "report"
    card = application.board()["maps"][0]
    assert card["stage"] == "delivery"
    assert card["delivery_summary"] == {
        "state": "reported",
        "count": 1,
        "latest": result["report"],
        "badges": [],
    }
    assert "evidence" not in card["delivery_summary"]["latest"]
    assert tracker.reports[0].report.assignment_map_id == MAP_ID
    events = application.board_events(cursor=0, limit=100)["events"]
    pm_events = [event for event in events if event["type"] == "pm.updated"]
    assert pm_events
    assert all(
        set(event["payload"]["assignment"]) == {"state", "updated_at"}
        for event in pm_events
    )
    assert any(
        event["type"] == "pm-report.upserted"
        and event["payload"]["report"] == result["report"]
        for event in events
    )


def test_pm_report_intent_is_durable_before_tracker_append(tmp_path):
    tracker = PMTracker()
    application = _application(tmp_path, tracker)
    effect_id = f"tracker-pm-report:{MAP_ID}:pm-durable-001"
    observed = []

    tracker.before_report_append = lambda _report: observed.append(
        application.outbox_status(effect_id=effect_id)["state"]
    )
    application.report_pm(
        request_identity=PM_IDENTITY,
        report=PMReportDraft(
            record_id="pm-durable-001",
            report_type="checkpoint",
            summary="The durable intent exists before GitHub is called.",
            timestamp="2026-08-23T09:59:30Z",
        ),
    )

    assert observed == ["leased"]
    status = application.outbox_status(effect_id=effect_id)
    assert status["effect_type"] == "tracker.pm-report"
    assert status["state"] == "succeeded"


def test_question_commits_complete_contract_before_one_canonical_ceo_turn(tmp_path):
    tracker = PMTracker()
    sessions = QuestionSessionRunner()
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
        tracker=tracker,
        session_runner=sessions,
        profile_name="ceo",
        clock=lambda: datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc),
    )
    project = application.configure_project(project_url=PROJECT_URL)
    application.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    application.open_map(map_id=MAP_ID)
    sessions.resume_calls.clear()
    application.assign_pm(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="turn-question-001",
    )
    observed = []
    sessions.before_resume = lambda content: observed.append(
        {
            "tracker_reports": len(tracker.reports),
            "stage": next(
                label.removeprefix("map-stage/")
                for label in tracker.issue.labels
                if label.startswith("map-stage/")
            ),
            "pm_state": application.pm_state(request_identity=PM_IDENTITY)[
                "assignment"
            ]["coordinator"]["state"],
            "content": content,
        }
    )
    question = PMReportDraft(
        record_id="question-report-001",
        report_type="question",
        summary="Choose the supported compatibility behavior.",
        timestamp="2026-08-23T10:00:00Z",
        blocking=True,
        continuation_requirement="Select one compatibility option.",
        correlation_id="decision-correlation-001",
        decision_class="product",
        scope={"map_id": MAP_ID, "area": "compatibility"},
        evidence=("Legacy clients still send the alias.",),
        options=("Keep the alias", "Remove the alias"),
    )

    first = application.report_pm(request_identity=PM_IDENTITY, report=question)
    repeated = application.report_pm(request_identity=PM_IDENTITY, report=question)

    assert first["report"]["correlation_id"] == "decision-correlation-001"
    assert repeated["idempotent"] is True
    assert tracker.report_writes == 1
    assert len(sessions.resume_calls) == 1
    assert observed == [
        {
            "tracker_reports": 1,
            "stage": "decision",
            "pm_state": "idle",
            "content": sessions.resume_calls[0]["content"],
        }
    ]
    assert "decision-correlation-001" in sessions.resume_calls[0]["content"]
    assert "Keep the alias" in sessions.resume_calls[0]["content"]
    detail = application.map_detail(map_id=MAP_ID)
    assert detail["pm_reports"][0]["scope"] == {
        "map_id": MAP_ID,
        "area": "compatibility",
    }
    assert detail["pm_reports"][0]["options"] == [
        "Keep the alias",
        "Remove the alias",
    ]


@pytest.mark.parametrize(
    "report",
    (
        PMReportDraft(
            record_id="question-1",
            report_type="question",
            summary="Which compatibility behavior should delivery preserve?",
            timestamp="2026-08-23T10:00:00Z",
            blocking=False,
            continuation_requirement="A yes/no answer about legacy aliases.",
            correlation_id="question-correlation-1",
            decision_class="product",
            scope={"map_id": MAP_ID, "area": "compatibility"},
            evidence=("Legacy clients still send aliases.",),
            options=("Keep aliases", "Remove aliases"),
        ),
        PMReportDraft(
            record_id="blocker-1",
            report_type="blocker",
            summary="The Map cannot proceed without the signed data agreement.",
            timestamp="2026-08-23T10:00:00Z",
            blocking=True,
            continuation_requirement="Evidence of a signed data agreement.",
        ),
        PMReportDraft(
            record_id="acceptance-1",
            report_type="acceptance",
            summary="The accepted outcomes are ready for chairman review.",
            timestamp="2026-08-23T10:00:00Z",
            evidence=("Outcome contract suite passed.",),
        ),
        PMReportDraft(
            record_id="failure-1",
            report_type="failure",
            summary="Delivery terminated because the source artifact is invalid.",
            timestamp="2026-08-23T10:00:00Z",
            failure_code="invalid-source-artifact",
        ),
    ),
)
def test_each_pm_report_type_has_the_fields_its_executive_meaning_requires(report):
    assert report.record_id


@pytest.mark.parametrize(
    "arguments",
    (
        {
            "record_id": "question-missing-impact",
            "report_type": "question",
            "summary": "Choose a behavior.",
            "timestamp": "2026-08-23T10:00:00Z",
            "continuation_requirement": "An answer.",
        },
        {
            "record_id": "blocker-missing-need",
            "report_type": "blocker",
            "summary": "Delivery is blocked.",
            "timestamp": "2026-08-23T10:00:00Z",
            "blocking": True,
        },
        {
            "record_id": "acceptance-no-evidence",
            "report_type": "acceptance",
            "summary": "Please accept.",
            "timestamp": "2026-08-23T10:00:00Z",
        },
        {
            "record_id": "failure-no-code",
            "report_type": "failure",
            "summary": "Delivery failed.",
            "timestamp": "2026-08-23T10:00:00Z",
        },
        {
            "record_id": "checkpoint-with-execution-evidence",
            "report_type": "checkpoint",
            "summary": "An outcome checkpoint.",
            "timestamp": "2026-08-23T10:00:00Z",
            "evidence": ("A worker command passed.",),
        },
    ),
)
def test_incomplete_pm_report_semantics_fail_closed(arguments):
    with pytest.raises(ValueError):
        PMReportDraft(**arguments)


def test_controllable_coordinator_runs_every_report_type_without_lane_leakage(
    tmp_path,
):
    tracker = PMTracker()
    application = _application(tmp_path, tracker)
    reports = (
        PMReportDraft(
            record_id="checkpoint-scenario",
            report_type="checkpoint",
            summary="The public delivery seam is ready.",
            timestamp="2026-08-23T10:01:00Z",
        ),
        PMReportDraft(
            record_id="question-scenario",
            report_type="question",
            summary="May legacy aliases remain available?",
            timestamp="2026-08-23T10:02:00Z",
            blocking=False,
            continuation_requirement="A yes/no answer about legacy aliases.",
            correlation_id="question-scenario-correlation",
            decision_class="product",
            scope={"map_id": MAP_ID, "area": "compatibility"},
            evidence=("Legacy clients still send aliases.",),
            options=("Keep aliases", "Remove aliases"),
        ),
        PMReportDraft(
            record_id="blocker-scenario",
            report_type="blocker",
            summary="All delivery is waiting for the data agreement.",
            timestamp="2026-08-23T10:03:00Z",
            blocking=True,
            continuation_requirement="Evidence that the data agreement is signed.",
        ),
        PMReportDraft(
            record_id="acceptance-scenario",
            report_type="acceptance",
            summary="The agreed outcomes are ready for executive acceptance.",
            timestamp="2026-08-23T10:04:00Z",
            evidence=("The outcome-level contract suite passed.",),
        ),
        PMReportDraft(
            record_id="failure-scenario",
            report_type="failure",
            summary="Delivery terminated because the source contract is invalid.",
            timestamp="2026-08-23T10:05:00Z",
            failure_code="invalid-source-contract",
        ),
    )

    results = []
    for index, report in enumerate(reports):
        if index:
            stage = application.board()["maps"][0]["stage"]
            if stage in {"decision", "acceptance"}:
                application.transition_map(
                    map_id=MAP_ID,
                    expected_stage=stage,
                    requested_stage="delivery",
                )
            application.begin_pm_turn(
                map_id=MAP_ID,
                request_identity=PM_IDENTITY,
                coordinator_id="coordinator-atlas",
                turn_id=f"turn-scenario-{index}",
            )
        results.append(
            application.report_pm(
                request_identity=PM_IDENTITY,
                report=report,
            )
        )

    assert [result["coordinator"]["state"] for result in results] == ["idle"] * len(
        reports
    )
    assert len(tracker.reports) == len(reports)
    board = application.board()
    assert len(board["maps"]) == 1
    card = board["maps"][0]
    assert card["stage"] == "delivery"
    assert card["delivery_summary"]["count"] == len(reports)
    assert card["delivery_summary"]["latest"]["type"] == "failure"
    assert card["delivery_summary"]["badges"] == [
        {"type": "terminal_failure", "count": 1}
    ]
    assert not {
        "implementation_tickets",
        "worker_lanes",
        "worktrees",
        "panes",
        "worker_logs",
    } & set(card)
    assert tracker.issue.state == "open"


def test_coordinator_resume_restarts_from_marker_without_duplicate_call(tmp_path):
    tracker = PMTracker()
    coordinator = ControllableCoordinatorResume()
    storage_root = tmp_path / "plugin-data" / "map-governance"

    def crash_after_resume(point, _intent):
        if point == "after_external_call":
            raise SimulatedCoordinatorProcessCrash()

    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        profile_name="pm",
        clock=lambda: datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc),
        coordinator_resume=coordinator,
        outbox_crash_injector=crash_after_resume,
    )
    project = application.configure_project(project_url=PROJECT_URL)
    application.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    application.assign_pm(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
    )
    effect_id = f"coordinator-resume:{MAP_ID}:turn-durable-resume"
    observed = []
    coordinator.before_resume = lambda _map_id, _turn_id: observed.append(
        application.outbox_status(effect_id=effect_id)["state"]
    )

    with pytest.raises(SimulatedCoordinatorProcessCrash):
        application.begin_pm_turn(
            map_id=MAP_ID,
            request_identity=PM_IDENTITY,
            coordinator_id="coordinator-atlas",
            turn_id="turn-durable-resume",
        )

    assert (
        application.pm_state(request_identity=PM_IDENTITY)["assignment"]["coordinator"][
            "state"
        ]
        == "idle"
    )
    coordinator.before_resume = None
    restarted = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        profile_name="pm",
        clock=lambda: datetime(2026, 8, 23, 10, 1, tzinfo=timezone.utc),
        coordinator_resume=coordinator,
    )
    recovered = restarted.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="turn-durable-resume",
    )

    assert observed == ["leased"]
    assert coordinator.calls == [(MAP_ID, "turn-durable-resume")]
    assert coordinator.contents == [
        "Resume the assigned PM turn. Re-read authoritative Map state before "
        "continuing."
    ]
    assert recovered["coordinator"]["state"] == "active"
    status = restarted.outbox_status(effect_id=effect_id)
    assert status["state"] == "succeeded"
    assert [attempt["outcome"] for attempt in status["attempts"]] == [
        "lease_expired",
        "succeeded",
    ]


def test_unresolved_coordinator_resume_reserves_turn_across_process_crash(tmp_path):
    tracker = PMTracker()
    coordinator = ControllableCoordinatorResume()
    storage_root = tmp_path / "plugin-data" / "map-governance"

    def crash_after_resume(point, _intent):
        if point == "after_external_call":
            raise SimulatedCoordinatorProcessCrash()

    crashing_process = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        profile_name="pm",
        clock=lambda: datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc),
        coordinator_resume=coordinator,
        outbox_crash_injector=crash_after_resume,
    )
    project = crashing_process.configure_project(project_url=PROJECT_URL)
    crashing_process.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    crashing_process.assign_pm(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
    )

    with pytest.raises(SimulatedCoordinatorProcessCrash):
        crashing_process.begin_pm_turn(
            map_id=MAP_ID,
            request_identity=PM_IDENTITY,
            coordinator_id="coordinator-atlas",
            turn_id="turn-1",
        )

    restarted = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        profile_name="pm",
        clock=lambda: datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc),
        coordinator_resume=coordinator,
    )
    with pytest.raises(ValueError, match="unresolved coordinator turn"):
        restarted.begin_pm_turn(
            map_id=MAP_ID,
            request_identity=PM_IDENTITY,
            coordinator_id="coordinator-atlas",
            turn_id="turn-2",
        )

    assert coordinator.calls == [(MAP_ID, "turn-1")]
    with pytest.raises(ValueError, match="does not exist"):
        restarted.outbox_status(effect_id=f"coordinator-resume:{MAP_ID}:turn-2")


@pytest.mark.parametrize("competing_outcome", ("report", "dispatch"))
def test_unresolved_pm_report_reserves_the_turn_outcome_across_process_crash(
    tmp_path,
    competing_outcome,
):
    tracker = PMTracker()
    _application(tmp_path, tracker)
    storage_root = tmp_path / "plugin-data" / "map-governance"

    def crash_after_report(point, intent):
        if point == "after_external_call" and intent.effect_type == "tracker.pm-report":
            raise SimulatedPMReportProcessCrash()

    crashing_process = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        profile_name="pm",
        clock=lambda: datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc),
        outbox_crash_injector=crash_after_report,
    )
    with pytest.raises(SimulatedPMReportProcessCrash):
        crashing_process.report_pm(
            request_identity=PM_IDENTITY,
            report=PMReportDraft(
                record_id="confirmed-before-report-crash",
                report_type="checkpoint",
                summary="Tracker confirmed this turn outcome before the crash.",
                timestamp="2026-08-23T10:00:00Z",
            ),
        )

    restarted = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        profile_name="pm",
        clock=lambda: datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc),
    )
    with pytest.raises(ValueError, match="already has a reserved report outcome"):
        if competing_outcome == "report":
            restarted.report_pm(
                request_identity=PM_IDENTITY,
                report=PMReportDraft(
                    record_id="competing-report-after-crash",
                    report_type="checkpoint",
                    summary="This second outcome must never reach the tracker.",
                    timestamp="2026-08-23T10:00:01Z",
                ),
            )
        else:
            restarted.complete_pm_dispatch(
                map_id=MAP_ID,
                request_identity=PM_IDENTITY,
                coordinator_id="coordinator-atlas",
                turn_id="turn-checkpoint-1",
                dispatch_id="competing-dispatch-after-crash",
            )

    assert tracker.report_writes == 1
    assert [record.report.content.record_id for record in tracker.reports] == [
        "confirmed-before-report-crash"
    ]
    assert (
        restarted.pm_state(request_identity=PM_IDENTITY)["assignment"]["coordinator"][
            "state"
        ]
        == "active"
    )


def test_active_pm_turn_rejects_another_coordinator_resume_before_external_call(
    tmp_path,
):
    tracker = PMTracker()
    coordinator = ControllableCoordinatorResume()
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
        tracker=tracker,
        profile_name="pm",
        clock=lambda: datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc),
        coordinator_resume=coordinator,
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
        turn_id="turn-1",
    )

    with pytest.raises(ValueError, match="already has an active turn"):
        application.begin_pm_turn(
            map_id=MAP_ID,
            request_identity=PM_IDENTITY,
            coordinator_id="coordinator-atlas",
            turn_id="turn-2",
        )

    assert coordinator.calls == [(MAP_ID, "turn-1")]
    with pytest.raises(ValueError, match="does not exist"):
        application.outbox_status(effect_id=f"coordinator-resume:{MAP_ID}:turn-2")


@pytest.mark.parametrize(
    ("report_type", "blocking", "expected_stage", "expected_badge"),
    (
        ("question", False, "delivery", "non_blocking_question"),
        ("question", True, "decision", "blocking_question"),
        ("blocker", False, "delivery", "localized_blocker"),
        ("blocker", True, "decision", "whole_map_blocker"),
    ),
)
def test_question_and_blocker_impact_controls_stage_without_overstating_the_map(
    tmp_path,
    report_type,
    blocking,
    expected_stage,
    expected_badge,
):
    tracker = PMTracker()
    application = _application(tmp_path, tracker)

    application.report_pm(
        request_identity=PM_IDENTITY,
        report=PMReportDraft(
            record_id=f"{report_type}-{blocking}",
            report_type=report_type,
            summary="A concrete delivery question or blocker.",
            timestamp="2026-08-23T10:06:00Z",
            blocking=blocking,
            continuation_requirement="A specific answer or evidence item.",
            **(
                {
                    "correlation_id": f"impact-correlation-{blocking}",
                    "decision_class": "product",
                    "scope": {"map_id": MAP_ID, "area": "compatibility"},
                    "evidence": ("The compatibility contract is ambiguous.",),
                    "options": ("Keep compatibility", "Remove compatibility"),
                }
                if report_type == "question"
                else {}
            ),
        ),
    )

    card = application.board()["maps"][0]
    assert card["stage"] == expected_stage
    assert card["delivery_summary"]["badges"] == [{"type": expected_badge, "count": 1}]


def test_retry_reconciles_a_confirmed_report_after_stage_write_failure(tmp_path):
    tracker = PMTracker()
    application = _application(tmp_path, tracker)
    draft = PMReportDraft(
        record_id="blocking-question-stage-retry",
        report_type="question",
        summary="All delivery needs one product answer.",
        timestamp="2026-08-23T10:07:00Z",
        blocking=True,
        continuation_requirement="Choose the supported compatibility behavior.",
        correlation_id="blocking-question-stage-retry-correlation",
        decision_class="product",
        scope={"map_id": MAP_ID, "area": "compatibility"},
        evidence=("The compatibility contract is ambiguous.",),
        options=("Keep compatibility", "Remove compatibility"),
    )
    tracker.fail_transitions = True

    with pytest.raises(TrackerError, match="stage write failed"):
        application.report_pm(request_identity=PM_IDENTITY, report=draft)

    assert tracker.report_writes == 1
    assert (
        application.pm_state(request_identity=PM_IDENTITY)["assignment"]["coordinator"][
            "state"
        ]
        == "active"
    )
    assert application.board()["maps"][0]["delivery_summary"] == {
        "state": "not_reported"
    }
    tracker.fail_transitions = False
    application.reconcile_project(project_id="PVT_acme_7")

    recovered = application.report_pm(request_identity=PM_IDENTITY, report=draft)

    assert recovered["idempotent"] is True
    assert tracker.report_writes == 1
    assert application.board()["maps"][0]["stage"] == "decision"


def test_acceptance_report_submits_evidence_without_approving_or_closing_the_map(
    tmp_path,
):
    tracker = PMTracker()
    application = _application(tmp_path, tracker)

    application.report_pm(
        request_identity=PM_IDENTITY,
        report=PMReportDraft(
            record_id="acceptance-evidence-only",
            report_type="acceptance",
            summary="The outcome is ready for executive review.",
            timestamp="2026-08-23T10:08:00Z",
            evidence=("The public outcome contract passed.",),
        ),
    )

    card = application.map_detail(map_id=MAP_ID)
    assert card["stage"] == "acceptance"
    assert card["approvals"] == {"count": 0, "items": []}
    assert tracker.issue.state == "open"


def test_unconfirmed_tracker_report_never_reaches_the_executive_projection(tmp_path):
    tracker = PMTracker()
    tracker.confirm_report_writes = False
    application = _application(tmp_path, tracker)

    with pytest.raises(TrackerPMReportConfirmationError):
        application.report_pm(
            request_identity=PM_IDENTITY,
            report=PMReportDraft(
                record_id="unconfirmed-checkpoint",
                report_type="checkpoint",
                summary="This write is not visible in Issue history.",
                timestamp="2026-08-23T10:10:00Z",
            ),
        )

    assert application.board()["maps"][0]["delivery_summary"] == {
        "state": "not_reported"
    }
    coordinator = application.pm_state(request_identity=PM_IDENTITY)["assignment"][
        "coordinator"
    ]
    assert coordinator["state"] == "active"
    assert coordinator["active_turn_id"] == "turn-checkpoint-1"


def test_same_report_payload_is_idempotent_but_changed_payload_conflicts(tmp_path):
    tracker = PMTracker()
    application = _application(tmp_path, tracker)
    draft = PMReportDraft(
        record_id="stable-checkpoint",
        report_type="checkpoint",
        summary="One immutable executive payload.",
        timestamp="2026-08-23T10:11:00Z",
    )

    first = application.report_pm(request_identity=PM_IDENTITY, report=draft)
    repeated = application.report_pm(request_identity=PM_IDENTITY, report=draft)

    assert first["idempotent"] is False
    assert repeated["idempotent"] is True
    assert tracker.report_writes == 1
    with pytest.raises(PMReportConflict):
        application.report_pm(
            request_identity=PM_IDENTITY,
            report=PMReportDraft(
                record_id="stable-checkpoint",
                report_type="checkpoint",
                summary="A conflicting payload under the same identity.",
                timestamp="2026-08-23T10:11:00Z",
            ),
        )
    assert tracker.report_writes == 1


def test_concurrent_identical_reports_converge_on_one_tracker_record(tmp_path):
    tracker = PMTracker()
    application = _application(tmp_path, tracker)
    draft = PMReportDraft(
        record_id="concurrent-checkpoint",
        report_type="checkpoint",
        summary="Concurrent delivery callbacks observed the same outcome.",
        timestamp="2026-08-23T10:12:00Z",
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _index: application.report_pm(
                    request_identity=PM_IDENTITY,
                    report=draft,
                ),
                range(2),
            )
        )

    assert tracker.report_writes == 1
    assert sorted(result["idempotent"] for result in results) == [False, True]
    assert all(result["coordinator"]["state"] == "idle" for result in results)


@pytest.mark.parametrize(
    "identity",
    (
        GovernanceRequestIdentity("ceo", "pm-session-atlas"),
        GovernanceRequestIdentity("pm", "foreign-session"),
    ),
)
def test_cross_profile_or_session_pm_request_fails_closed_with_zero_writes(
    tmp_path,
    monkeypatch,
    identity,
):
    tracker = PMTracker()
    application = _application(tmp_path, tracker)
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "pm")
    monkeypatch.setenv("HERMES_SESSION_ID", "pm-session-atlas")

    with pytest.raises(GovernanceAuthorizationError) as raised:
        application.report_pm(
            request_identity=identity,
            report=PMReportDraft(
                record_id="spoofed-report",
                report_type="checkpoint",
                summary="Ambient process identity must not authorize this request.",
                timestamp="2026-08-23T10:13:00Z",
            ),
        )

    assert raised.value.reason == "pm_assignment_missing"
    assert tracker.report_writes == 0
    assert application.authorization_denials()[-1] == {
        "action": "pm:report",
        "map_id": "unassigned",
        "profile_name": identity.profile_name,
        "session_id": identity.session_id,
        "reason": "pm_assignment_missing",
        "denied_at": "2026-08-23T10:00:00Z",
    }


def test_pm_assignment_is_immutable_and_pm_toolset_crossover_is_audited(tmp_path):
    tracker = PMTracker()
    application = _application(tmp_path, tracker)

    with pytest.raises(GovernanceAuthorizationError) as assignment_denial:
        application.assign_pm(
            map_id=MAP_ID,
            request_identity=GovernanceRequestIdentity("pm", "replacement-session"),
            coordinator_id="replacement-coordinator",
        )
    assert assignment_denial.value.reason == "pm_assignment_conflict"
    with pytest.raises(GovernanceAuthorizationError) as raised:
        application.enforce_assigned_pm_toolset(
            request_identity=PM_IDENTITY,
            tool_name="map_governance_ceo",
            allowed_tool_names=frozenset({"map_governance_pm"}),
        )

    assert raised.value.reason == "tool_outside_pm_toolset"
    assert tracker.report_writes == 0
    assert application.authorization_denials(map_id=MAP_ID)[-1]["action"] == (
        "invoke_tool:map_governance_ceo"
    )


def test_one_pm_request_identity_cannot_be_reassigned_across_maps(tmp_path):
    tracker = PMTracker()
    tracker.issues[OTHER_ISSUE_URL] = TrackerIssue(
        id=OTHER_MAP_ID,
        repository="acme/atlas",
        number=42,
        title="Map the Atlas billing model",
        url=OTHER_ISSUE_URL,
        state="open",
        state_reason=None,
        labels=("map", "map-stage/delivery"),
    )
    application = _application(tmp_path, tracker)
    application.bind_map(project_id="PVT_acme_7", issue_url=OTHER_ISSUE_URL)

    with pytest.raises(GovernanceAuthorizationError) as denial:
        application.assign_pm(
            map_id=OTHER_MAP_ID,
            request_identity=PM_IDENTITY,
            coordinator_id="coordinator-atlas",
        )
    assert denial.value.reason == "pm_identity_assigned_to_another_map"
    assert application.authorization_denials(map_id=OTHER_MAP_ID)[-1]["action"] == (
        "pm:assign"
    )

    state = application.pm_state(request_identity=PM_IDENTITY)
    assert state["assignment"]["map"]["id"] == MAP_ID
    assert tracker.report_writes == 0


def test_confirmed_dispatch_ends_idle_without_creating_an_executive_record(tmp_path):
    tracker = PMTracker()
    application = _application(tmp_path, tracker)

    completed = application.complete_pm_dispatch(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="turn-checkpoint-1",
        dispatch_id="dispatch-contract-001",
    )
    repeated = application.complete_pm_dispatch(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="turn-checkpoint-1",
        dispatch_id="dispatch-contract-001",
    )

    assert completed["coordinator"]["state"] == "idle"
    assert completed["coordinator"]["last_outcome"] == "dispatch"
    assert repeated["idempotent"] is True
    assert application.board()["maps"][0]["delivery_summary"] == {
        "state": "not_reported"
    }
    resumed = application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="turn-after-dispatch",
    )
    assert resumed["coordinator"]["state"] == "active"


@pytest.mark.parametrize(
    ("operation", "identity", "coordinator_id", "reason"),
    (
        (
            "begin",
            GovernanceRequestIdentity("pm", "foreign-session"),
            "coordinator-atlas",
            "pm_assignment_request_mismatch",
        ),
        (
            "begin",
            PM_IDENTITY,
            "foreign-coordinator",
            "pm_coordinator_mismatch",
        ),
        (
            "dispatch",
            GovernanceRequestIdentity("ceo", "pm-session-atlas"),
            "coordinator-atlas",
            "pm_assignment_request_mismatch",
        ),
    ),
)
def test_coordinator_boundary_is_request_scoped_and_audits_denials(
    tmp_path,
    operation,
    identity,
    coordinator_id,
    reason,
):
    tracker = PMTracker()
    application = _application(tmp_path, tracker)

    with pytest.raises(GovernanceAuthorizationError) as denial:
        if operation == "begin":
            application.begin_pm_turn(
                map_id=MAP_ID,
                request_identity=identity,
                coordinator_id=coordinator_id,
                turn_id="unauthorized-turn",
            )
        else:
            application.complete_pm_dispatch(
                map_id=MAP_ID,
                request_identity=identity,
                coordinator_id=coordinator_id,
                turn_id="turn-checkpoint-1",
                dispatch_id="unauthorized-dispatch",
            )

    assert denial.value.reason == reason
    assert application.authorization_denials(map_id=MAP_ID)[-1] == {
        "action": f"pm:{'begin_turn' if operation == 'begin' else 'dispatch'}",
        "map_id": MAP_ID,
        "profile_name": identity.profile_name,
        "session_id": identity.session_id,
        "reason": reason,
        "denied_at": "2026-08-23T10:00:00Z",
    }
    assert tracker.report_writes == 0


def test_restart_and_refresh_rebuild_pm_projection_from_tracker_truth(tmp_path):
    tracker = PMTracker()
    application = _application(tmp_path, tracker)
    application.report_pm(
        request_identity=PM_IDENTITY,
        report=PMReportDraft(
            record_id="restart-checkpoint",
            report_type="checkpoint",
            summary="This confirmed report survives a projection rebuild.",
            timestamp="2026-08-23T10:14:00Z",
        ),
    )
    database = tmp_path / "plugin-data" / "map-governance" / "registry.db"
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM pm_report_projections")

    restarted = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
        tracker=tracker,
        profile_name="pm",
        clock=lambda: datetime(2026, 8, 23, 10, 30, tzinfo=timezone.utc),
    )
    assert restarted.pm_state(request_identity=PM_IDENTITY)["delivery_summary"] == {
        "state": "not_reported"
    }

    rebuilt = restarted.refresh(project_id="PVT_acme_7")

    assert rebuilt["maps"][0]["delivery_summary"]["latest"]["record_id"] == (
        "restart-checkpoint"
    )
    assert (
        restarted.pm_state(request_identity=PM_IDENTITY)["assignment"]["map"]["id"]
        == MAP_ID
    )


def test_previous_schema_is_migrated_through_the_public_application_seam(tmp_path):
    tracker = PMTracker()
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
        tracker=tracker,
        profile_name="pm",
    )
    project = application.configure_project(project_url=PROJECT_URL)
    application.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    database = tmp_path / "plugin-data" / "map-governance" / "registry.db"
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE pm_report_projections")
        connection.execute("DROP TABLE pm_assignments")
        connection.execute(
            "UPDATE plugin_metadata SET schema_version = 5 WHERE namespace = ?",
            ("map-governance",),
        )

    migrated = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
        tracker=tracker,
        profile_name="pm",
    )

    assigned = migrated.assign_pm(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
    )
    assert assigned["assignment"]["state"] == "idle"
    assert migrated.health()["components"]["storage"]["status"] == "ready"
