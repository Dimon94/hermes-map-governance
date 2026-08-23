from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock

import pytest

from map_governance import (
    GovernanceAuthorizationError,
    GovernanceRequestIdentity,
    MapGovernanceApplication,
    StructuredDecisionConflict,
    TrackerDecisionConfirmationError,
)
from map_governance.sessions import CanonicalSession
from map_governance.tracker import (
    StructuredDecision,
    TrackerDecisionRecord,
    TrackerIssue,
    TrackerProject,
    TrackerError,
)


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MAP_ID = "I_atlas_41"
OTHER_MAP_ID = "I_atlas_42"
PROFILE = "ceo"
ROOT_SESSION_ID = "mapgov-root"
LIVE_SESSION_ID = "mapgov-live"
ISSUE_URL = "https://github.com/acme/atlas/issues/41"
OTHER_ISSUE_URL = "https://github.com/acme/atlas/issues/42"
PROJECT_URL = "https://github.com/orgs/acme/projects/7"


class ExecutiveTracker:
    def __init__(self) -> None:
        self.governance_writes = 0
        self.decisions: list[TrackerDecisionRecord] = []
        self.confirm_decision_writes = True
        self.issues = {
            ISSUE_URL: TrackerIssue(
                id=MAP_ID,
                repository="acme/atlas",
                number=41,
                title="Map the Atlas launch",
                url=ISSUE_URL,
                state="open",
                state_reason=None,
                labels=("map", "map-stage/authorized"),
            ),
            OTHER_ISSUE_URL: TrackerIssue(
                id=OTHER_MAP_ID,
                repository="acme/atlas",
                number=42,
                title="Map the Atlas billing model",
                url=OTHER_ISSUE_URL,
                state="open",
                state_reason=None,
                labels=("map", "map-stage/discovery"),
            ),
        }

    def get_project(self, url: str) -> TrackerProject:
        assert url == PROJECT_URL
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

    def transition_issue_stage(self, *args, **kwargs):
        raise AssertionError("CEO governance tests do not transition Map stages")

    def append_decision(self, *args, **kwargs):
        self.governance_writes += 1
        decision = kwargs["decision"]
        record = TrackerDecisionRecord(
            decision=decision,
            tracker_record_id=f"IC_{len(self.decisions) + 1}",
            tracker_record_url=f"{ISSUE_URL}#issuecomment-{len(self.decisions) + 1}",
        )
        if self.confirm_decision_writes:
            self.decisions.append(record)
        return record

    def list_decisions(self, url: str) -> list[TrackerDecisionRecord]:
        return list(self.decisions) if url == ISSUE_URL else []


class CanonicalSessionRunner:
    def __init__(self) -> None:
        self.sessions_by_title: dict[str, CanonicalSession] = {}
        self.sessions_by_root: dict[str, CanonicalSession] = {}

    def find_exact(self, *, title: str) -> list[CanonicalSession]:
        session = self.sessions_by_title.get(title)
        return [session] if session is not None else []

    def mint(self, *, title: str, **kwargs) -> CanonicalSession:
        suffix = len(self.sessions_by_title) + 1
        session = CanonicalSession(
            root_session_id=ROOT_SESSION_ID if suffix == 1 else f"mapgov-root-{suffix}",
            live_session_id=LIVE_SESSION_ID if suffix == 1 else f"mapgov-live-{suffix}",
            title=title,
            last_activity_at="2026-08-23T09:00:00Z",
            bootstrap_sent=True,
        )
        self.sessions_by_title[title] = session
        self.sessions_by_root[session.root_session_id] = session
        return session

    def initialize(self, session: CanonicalSession, **kwargs) -> CanonicalSession:
        return session

    def resolve(self, *, root_session_id: str) -> CanonicalSession | None:
        return self.sessions_by_root.get(root_session_id)

    def load_skill(self, session: CanonicalSession, **kwargs) -> CanonicalSession:
        return session


def _canonical_application(
    tmp_path,
    tracker: ExecutiveTracker | None = None,
) -> MapGovernanceApplication:
    tracker = tracker or ExecutiveTracker()
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
        tracker=tracker,
        session_runner=CanonicalSessionRunner(),
        profile_name=PROFILE,
        clock=lambda: datetime(2026, 8, 23, 9, 30, tzinfo=timezone.utc),
    )
    project = application.configure_project(project_url=PROJECT_URL)
    application.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    opened = application.open_map(map_id=MAP_ID)
    assert opened["ceo_session"]["live_session_id"] == LIVE_SESSION_ID
    return application


def test_canonical_ceo_session_reads_the_executive_state(tmp_path):
    tracker = ExecutiveTracker()
    application = _canonical_application(tmp_path, tracker)

    state = application.executive_state(
        map_id=MAP_ID,
        request_identity=GovernanceRequestIdentity(
            profile_name=PROFILE,
            session_id=LIVE_SESSION_ID,
        ),
    )

    assert state["map"]["tracker"]["identity"] == "acme/atlas#41"
    assert state["map"]["stage"] == "authorized"
    assert state["recent_decisions"] == []
    assert state["approvals"] == {"count": 0, "items": []}
    assert state["delivery_summary"] == {"state": "not_reported"}


@pytest.mark.parametrize(
    ("profile_name", "session_id", "reason"),
    (
        ("chairman", LIVE_SESSION_ID, "profile_mismatch"),
        (PROFILE, "another-map-session", "session_mismatch"),
    ),
)
def test_cross_profile_session_or_map_requests_fail_closed_and_are_audited(
    tmp_path,
    monkeypatch,
    profile_name,
    session_id,
    reason,
):
    tracker = ExecutiveTracker()
    application = _canonical_application(tmp_path, tracker)
    monkeypatch.setenv("HERMES_SESSION_PROFILE", PROFILE)
    monkeypatch.setenv("HERMES_SESSION_ID", LIVE_SESSION_ID)

    with pytest.raises(GovernanceAuthorizationError) as raised:
        application.executive_state(
            map_id=MAP_ID,
            request_identity=GovernanceRequestIdentity(
                profile_name=profile_name,
                session_id=session_id,
            ),
        )

    assert raised.value.reason == reason
    assert tracker.governance_writes == 0
    assert application.authorization_denials(map_id=MAP_ID) == [
        {
            "action": "inspect",
            "map_id": MAP_ID,
            "profile_name": profile_name,
            "session_id": session_id,
            "reason": reason,
            "denied_at": "2026-08-23T09:30:00Z",
        }
    ]


def test_session_canonical_for_another_map_has_no_cross_map_authority(tmp_path):
    tracker = ExecutiveTracker()
    application = _canonical_application(tmp_path, tracker)
    application.bind_map(project_id="PVT_acme_7", issue_url=OTHER_ISSUE_URL)
    other = application.open_map(map_id=OTHER_MAP_ID)

    with pytest.raises(GovernanceAuthorizationError):
        application.executive_state(
            map_id=MAP_ID,
            request_identity=GovernanceRequestIdentity(
                PROFILE,
                other["ceo_session"]["live_session_id"],
            ),
        )

    assert tracker.governance_writes == 0
    assert application.authorization_denials(map_id=MAP_ID)[-1]["reason"] == (
        "session_mismatch"
    )


def test_unauthorized_decision_is_audited_before_any_tracker_governance_write(
    tmp_path,
):
    tracker = ExecutiveTracker()
    application = _canonical_application(tmp_path, tracker)
    decision = StructuredDecision(
        decision_id="decision-denied-001",
        type="product",
        rationale="This request has no canonical authority.",
        authority="ceo",
        affected_stage="authorized",
        timestamp="2026-08-23T09:25:00Z",
    )

    with pytest.raises(GovernanceAuthorizationError):
        application.record_decision(
            map_id=MAP_ID,
            request_identity=GovernanceRequestIdentity(PROFILE, "foreign-session"),
            decision=decision,
        )

    assert tracker.governance_writes == 0
    assert tracker.decisions == []
    assert application.authorization_denials(map_id=MAP_ID)[-1]["action"] == (
        "record_decision"
    )


def test_canonical_ceo_cannot_claim_another_decision_authority(tmp_path):
    tracker = ExecutiveTracker()
    application = _canonical_application(tmp_path, tracker)
    decision = StructuredDecision(
        decision_id="decision-denied-authority-001",
        type="product",
        rationale="The authenticated CEO cannot claim chairman authority.",
        authority="chairman",
        affected_stage="authorized",
        timestamp="2026-08-23T09:25:00Z",
    )

    with pytest.raises(GovernanceAuthorizationError) as raised:
        application.record_decision(
            map_id=MAP_ID,
            request_identity=GovernanceRequestIdentity(PROFILE, LIVE_SESSION_ID),
            decision=decision,
        )

    assert raised.value.reason == "decision_authority_mismatch"
    assert tracker.governance_writes == 0
    assert tracker.decisions == []
    assert application.authorization_denials(map_id=MAP_ID)[-1] == {
        "action": "record_decision",
        "map_id": MAP_ID,
        "profile_name": PROFILE,
        "session_id": LIVE_SESSION_ID,
        "reason": "decision_authority_mismatch",
        "denied_at": "2026-08-23T09:30:00Z",
    }


def test_canonical_ceo_tool_policy_blocks_non_governance_capabilities(tmp_path):
    tracker = ExecutiveTracker()
    application = _canonical_application(tmp_path, tracker)
    identity = GovernanceRequestIdentity(PROFILE, LIVE_SESSION_ID)

    assert (
        application.enforce_canonical_ceo_toolset(
            request_identity=identity,
            tool_name="map_governance_ceo",
            allowed_tool_names=frozenset({"map_governance_ceo"}),
        )
        is True
    )
    with pytest.raises(GovernanceAuthorizationError) as raised:
        application.enforce_canonical_ceo_toolset(
            request_identity=identity,
            tool_name="terminal",
            allowed_tool_names=frozenset({"map_governance_ceo"}),
        )

    assert raised.value.reason == "tool_outside_ceo_toolset"
    assert tracker.governance_writes == 0
    assert application.authorization_denials(map_id=MAP_ID)[-1]["action"] == (
        "invoke_tool:terminal"
    )
    assert (
        application.enforce_canonical_ceo_toolset(
            request_identity=GovernanceRequestIdentity(PROFILE, "ordinary-session"),
            tool_name="terminal",
            allowed_tool_names=frozenset({"map_governance_ceo"}),
        )
        is False
    )


def test_confirmed_decision_appears_in_detail_card_and_executive_state(tmp_path):
    tracker = ExecutiveTracker()
    application = _canonical_application(tmp_path, tracker)
    request_identity = GovernanceRequestIdentity(
        profile_name=PROFILE,
        session_id=LIVE_SESSION_ID,
    )
    decision = StructuredDecision(
        decision_id="decision-atlas-market-001",
        type="product",
        rationale="Launch to the research cohort before widening access.",
        authority="ceo",
        affected_stage="authorized",
        timestamp="2026-08-23T09:25:00Z",
    )

    recorded = application.record_decision(
        map_id=MAP_ID,
        request_identity=request_identity,
        decision=decision,
    )

    expected = {
        "decision_id": "decision-atlas-market-001",
        "type": "product",
        "rationale": "Launch to the research cohort before widening access.",
        "authority": "ceo",
        "affected_stage": "authorized",
        "timestamp": "2026-08-23T09:25:00Z",
        "tracker": {
            "id": "IC_1",
            "url": f"{ISSUE_URL}#issuecomment-1",
        },
        "confirmed_at": "2026-08-23T09:30:00Z",
    }
    assert recorded == {"map_id": MAP_ID, "decision": expected, "idempotent": False}
    assert tracker.governance_writes == 1
    assert application.map_detail(map_id=MAP_ID)["recent_decisions"] == [expected]
    card = application.board()["maps"][0]
    assert card["decision_summary"] == {"count": 1, "latest": expected}
    assert application.executive_state(
        map_id=MAP_ID,
        request_identity=request_identity,
    )["recent_decisions"] == [expected]


def test_recent_decisions_and_card_latest_sort_by_absolute_timestamp(tmp_path):
    tracker = ExecutiveTracker()
    tracker.decisions.extend(
        [
            TrackerDecisionRecord(
                decision=StructuredDecision(
                    decision_id="decision-offset-earlier",
                    type="operational",
                    rationale="This local time is earlier after offset conversion.",
                    authority="ceo",
                    affected_stage="authorized",
                    timestamp="2026-08-23T10:00:00+02:00",
                ),
                tracker_record_id="IC_earlier",
                tracker_record_url=f"{ISSUE_URL}#issuecomment-earlier",
            ),
            TrackerDecisionRecord(
                decision=StructuredDecision(
                    decision_id="decision-utc-latest",
                    type="product",
                    rationale="This is the latest decision in absolute time.",
                    authority="ceo",
                    affected_stage="authorized",
                    timestamp="2026-08-23T09:30:00Z",
                ),
                tracker_record_id="IC_latest",
                tracker_record_url=f"{ISSUE_URL}#issuecomment-latest",
            ),
        ]
    )
    application = _canonical_application(tmp_path, tracker)

    recent = application.map_detail(map_id=MAP_ID)["recent_decisions"]

    assert [decision["decision_id"] for decision in recent] == [
        "decision-utc-latest",
        "decision-offset-earlier",
    ]
    assert application.board()["maps"][0]["decision_summary"]["latest"] == recent[0]


def test_stable_decision_id_replays_once_and_rejects_a_different_payload(tmp_path):
    tracker = ExecutiveTracker()
    application = _canonical_application(tmp_path, tracker)
    identity = GovernanceRequestIdentity(PROFILE, LIVE_SESSION_ID)
    decision = StructuredDecision(
        decision_id="decision-atlas-market-001",
        type="product",
        rationale="Start with the research cohort.",
        authority="ceo",
        affected_stage="authorized",
        timestamp="2026-08-23T09:25:00Z",
    )

    first = application.record_decision(
        map_id=MAP_ID,
        request_identity=identity,
        decision=decision,
    )
    replay = application.record_decision(
        map_id=MAP_ID,
        request_identity=identity,
        decision=decision,
    )

    assert first["idempotent"] is False
    assert replay == {**first, "idempotent": True}
    assert tracker.governance_writes == 1
    assert application.board()["maps"][0]["decision_summary"]["count"] == 1

    with pytest.raises(StructuredDecisionConflict):
        application.record_decision(
            map_id=MAP_ID,
            request_identity=identity,
            decision=replace(decision, rationale="Widen access immediately."),
        )

    assert tracker.governance_writes == 1
    assert (
        application.map_detail(map_id=MAP_ID)["recent_decisions"][0]
        == first["decision"]
    )


def test_tracker_write_without_readback_confirmation_is_not_projected(tmp_path):
    tracker = ExecutiveTracker()
    tracker.confirm_decision_writes = False
    application = _canonical_application(tmp_path, tracker)
    decision = StructuredDecision(
        decision_id="decision-atlas-market-unconfirmed",
        type="product",
        rationale="Wait for tracker confirmation.",
        authority="ceo",
        affected_stage="authorized",
        timestamp="2026-08-23T09:25:00Z",
    )

    with pytest.raises(TrackerDecisionConfirmationError):
        application.record_decision(
            map_id=MAP_ID,
            request_identity=GovernanceRequestIdentity(PROFILE, LIVE_SESSION_ID),
            decision=decision,
        )

    assert tracker.governance_writes == 1
    assert application.map_detail(map_id=MAP_ID)["recent_decisions"] == []
    assert application.board()["maps"][0]["decision_summary"] == {
        "count": 0,
        "latest": None,
    }


def test_tracker_failure_leaves_the_decision_projection_unchanged(tmp_path):
    class FailingTracker(ExecutiveTracker):
        def append_decision(self, *args, **kwargs):
            self.governance_writes += 1
            raise TrackerError("GitHub decision write failed")

    tracker = FailingTracker()
    application = _canonical_application(tmp_path, tracker)
    decision = StructuredDecision(
        decision_id="decision-atlas-failed-001",
        type="product",
        rationale="Do not project a failed tracker write.",
        authority="ceo",
        affected_stage="authorized",
        timestamp="2026-08-23T09:25:00Z",
    )

    with pytest.raises(TrackerError, match="decision write failed"):
        application.record_decision(
            map_id=MAP_ID,
            request_identity=GovernanceRequestIdentity(PROFILE, LIVE_SESSION_ID),
            decision=decision,
        )

    assert tracker.governance_writes == 1
    assert application.map_detail(map_id=MAP_ID)["recent_decisions"] == []


def test_fresh_projection_rebuilds_confirmed_decisions_from_tracker_truth(tmp_path):
    tracker = ExecutiveTracker()
    application = _canonical_application(tmp_path / "first", tracker)
    decision = StructuredDecision(
        decision_id="decision-atlas-rebuild-001",
        type="product",
        rationale="Tracker truth must rebuild the executive projection.",
        authority="ceo",
        affected_stage="authorized",
        timestamp="2026-08-23T09:25:00Z",
    )
    recorded = application.record_decision(
        map_id=MAP_ID,
        request_identity=GovernanceRequestIdentity(PROFILE, LIVE_SESSION_ID),
        decision=decision,
    )

    rebuilt = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "rebuilt" / "plugin-data" / "map-governance",
        tracker=tracker,
        clock=lambda: datetime(2026, 8, 23, 9, 30, tzinfo=timezone.utc),
    )
    project = rebuilt.configure_project(project_url=PROJECT_URL)
    rebuilt.bind_map(project_id=project["id"], issue_url=ISSUE_URL)

    assert rebuilt.map_detail(map_id=MAP_ID)["recent_decisions"] == [
        recorded["decision"]
    ]
    assert rebuilt.board()["maps"][0]["decision_summary"]["count"] == 1


def test_concurrent_retries_converge_on_one_tracker_decision(tmp_path):
    class RacingTracker(ExecutiveTracker):
        def __init__(self) -> None:
            super().__init__()
            self._lock = Lock()
            self.first_read = Event()
            self.second_read = Event()
            self.release_reads = Event()
            self.release_reads.set()
            self.empty_reads = 0

        def list_decisions(self, url: str) -> list[TrackerDecisionRecord]:
            with self._lock:
                records = list(self.decisions)
                if not records:
                    self.empty_reads += 1
                    (
                        self.first_read if self.empty_reads == 1 else self.second_read
                    ).set()
            if not records:
                self.release_reads.wait(timeout=2)
            return records

        def append_decision(self, *args, **kwargs):
            with self._lock:
                self.governance_writes += 1
                decision = kwargs["decision"]
                record = TrackerDecisionRecord(
                    decision=decision,
                    tracker_record_id=f"IC_{self.governance_writes}",
                    tracker_record_url=(
                        f"{ISSUE_URL}#issuecomment-{self.governance_writes}"
                    ),
                )
                self.decisions.append(record)
                return record

    tracker = RacingTracker()
    application = _canonical_application(tmp_path, tracker)
    tracker.first_read.clear()
    tracker.second_read.clear()
    tracker.release_reads.clear()
    tracker.empty_reads = 0
    decision = StructuredDecision(
        decision_id="decision-atlas-concurrent-001",
        type="product",
        rationale="Concurrent retries must converge.",
        authority="ceo",
        affected_stage="authorized",
        timestamp="2026-08-23T09:25:00Z",
    )

    def record():
        return application.record_decision(
            map_id=MAP_ID,
            request_identity=GovernanceRequestIdentity(PROFILE, LIVE_SESSION_ID),
            decision=decision,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(record)
        assert tracker.first_read.wait(timeout=1)
        second = executor.submit(record)
        tracker.second_read.wait(timeout=0.2)
        tracker.release_reads.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert tracker.governance_writes == 1
    assert sorted(result["idempotent"] for result in results) == [False, True]
    assert application.board()["maps"][0]["decision_summary"]["count"] == 1
