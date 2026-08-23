from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
import sqlite3

import pytest

from dataclasses import replace

from map_governance import MapGovernanceApplication, StaleProjectionError
from map_governance.events import (
    BoardEventConflict,
    BoardEventJournal,
    BoardEventSettings,
)
from map_governance.tracker import TrackerError, TrackerIssue, TrackerProject


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PROJECT_URL = "https://github.com/orgs/acme/projects/7"
ISSUE_URL = "https://github.com/acme/atlas/issues/41"


class TrackerBoundary:
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
        assert url == ISSUE_URL
        return TrackerIssue(
            id="I_atlas_41",
            repository="acme/atlas",
            number=41,
            title="Map the Atlas launch",
            url=url,
            state="open",
            state_reason=None,
            labels=("map", "map-stage/authorized"),
        )

    def list_decisions(self, _url: str):
        return []

    def list_pm_reports(self, _url: str):
        return []


class TwoProjectTrackerBoundary(TrackerBoundary):
    def __init__(self) -> None:
        self.unavailable_projects: set[str] = set()
        self.projects = {
            PROJECT_URL: super().get_project(PROJECT_URL),
            "https://github.com/users/octocat/projects/3": TrackerProject(
                id="PVT_octocat_3",
                owner="octocat",
                owner_type="user",
                number=3,
                title="Octocat portfolio",
                url="https://github.com/users/octocat/projects/3",
            ),
        }
        self.issues = {
            ISSUE_URL: super().get_issue(ISSUE_URL),
            "https://github.com/octocat/hello-world/issues/9": TrackerIssue(
                id="I_hello_9",
                repository="octocat/hello-world",
                number=9,
                title="Map a friendly launch",
                url="https://github.com/octocat/hello-world/issues/9",
                state="open",
                state_reason=None,
                labels=("map", "map-stage/authorized"),
            ),
        }

    def get_project(self, url: str) -> TrackerProject:
        project = self.projects[url]
        if project.id in self.unavailable_projects:
            raise TrackerError("GitHub Project authority is unreachable")
        return project

    def get_issue(self, url: str) -> TrackerIssue:
        issue = self.issues[url]
        project_id = "PVT_acme_7" if url == ISSUE_URL else "PVT_octocat_3"
        if project_id in self.unavailable_projects:
            raise TrackerError("GitHub Issue authority is unreachable")
        return issue

    def transition_issue_stage(
        self, url: str, *, expected_stage: str, requested_stage: str
    ) -> TrackerIssue:
        issue = self.get_issue(url)
        labels = tuple(
            label for label in issue.labels if not label.startswith("map-stage/")
        ) + (f"map-stage/{requested_stage}",)
        committed = replace(issue, labels=labels)
        self.issues[url] = committed
        return committed


def application(storage_root: Path) -> MapGovernanceApplication:
    return MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=TrackerBoundary(),
        clock=lambda: datetime(2026, 8, 23, 7, 30, tzinfo=timezone.utc),
    )


def test_committed_projection_events_resume_in_order_after_restart(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    first_process = application(storage_root)

    project = first_process.configure_project(project_url=PROJECT_URL)
    first_process.bind_map(project_id=project["id"], issue_url=ISSUE_URL)

    first_batch = first_process.board_events(cursor=0)

    assert first_batch["status"] == "open"
    assert [event["type"] for event in first_batch["events"]] == [
        "project.upserted",
        "map.upserted",
    ]
    assert [event["cursor"] for event in first_batch["events"]] == [1, 2]
    assert first_batch["cursor"] == 2
    assert all(
        event["payload_hash"].startswith("sha256:") for event in first_batch["events"]
    )

    restarted = application(storage_root)
    resumed = restarted.board_events(cursor=1)

    assert [event["cursor"] for event in resumed["events"]] == [2]
    assert resumed["events"][0]["map_id"] == "I_atlas_41"
    assert resumed["events"][0]["project_id"] == "PVT_acme_7"
    assert resumed["events"][0]["payload"]["card"]["stage"] == "authorized"


def test_stable_event_id_is_idempotent_only_for_the_same_payload(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    application(storage_root).health()
    journal = BoardEventJournal(storage_root / "registry.db")

    created = journal.commit(
        event_id="map:I_atlas_41:version-1",
        project_id="PVT_acme_7",
        map_id="I_atlas_41",
        event_type="map.upserted",
        payload={"card": {"id": "I_atlas_41", "stage": "authorized"}},
        committed_at="2026-08-23T07:30:00Z",
    )
    replayed = journal.commit(
        event_id="map:I_atlas_41:version-1",
        project_id="PVT_acme_7",
        map_id="I_atlas_41",
        event_type="map.upserted",
        payload={"card": {"id": "I_atlas_41", "stage": "authorized"}},
        committed_at="2026-08-23T07:31:00Z",
    )

    assert created is True
    assert replayed is False
    assert len(journal.read(cursor=0)["events"]) == 1
    with pytest.raises(BoardEventConflict, match="different content"):
        journal.commit(
            event_id="map:I_atlas_41:version-1",
            project_id="PVT_acme_7",
            map_id="I_atlas_41",
            event_type="map.upserted",
            payload={"card": {"id": "I_atlas_41", "stage": "delivery"}},
            committed_at="2026-08-23T07:32:00Z",
        )


def test_out_of_order_stable_event_replay_cannot_regress_a_resource(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    application(storage_root).health()
    journal = BoardEventJournal(storage_root / "registry.db")
    shared = {
        "project_id": "PVT_acme_7",
        "map_id": "I_atlas_41",
        "event_type": "map.upserted",
    }
    journal.commit(
        event_id="map:I_atlas_41:A",
        payload={"version": "A"},
        committed_at="2026-08-23T07:30:00Z",
        **shared,
    )
    journal.commit(
        event_id="map:I_atlas_41:B",
        payload={"version": "B"},
        committed_at="2026-08-23T07:31:00Z",
        **shared,
    )

    replayed = journal.commit(
        event_id="map:I_atlas_41:A",
        payload={"version": "A"},
        committed_at="2026-08-23T07:32:00Z",
        **shared,
    )

    assert replayed is False
    assert [
        event["payload"]["version"] for event in journal.read(cursor=0)["events"]
    ] == ["A", "B"]


def test_rolled_back_transaction_never_exposes_its_board_event(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    application(storage_root).health()
    journal = BoardEventJournal(storage_root / "registry.db")

    with pytest.raises(RuntimeError, match="abort governance state"):
        with journal.atomic() as connection:
            journal.append_in_transaction(
                connection,
                event_id="map:I_atlas_41:rolled-back",
                project_id="PVT_acme_7",
                map_id="I_atlas_41",
                event_type="map.upserted",
                payload={"card": {"id": "I_atlas_41", "stage": "delivery"}},
                committed_at="2026-08-23T07:30:00Z",
            )
            raise RuntimeError("abort governance state")

    assert journal.read(cursor=0)["events"] == []


def test_concurrent_commits_receive_one_total_durable_order(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    application(storage_root).health()
    journal = BoardEventJournal(storage_root / "registry.db")

    def commit(index: int) -> None:
        journal.commit(
            event_id=f"map:I_atlas_41:concurrent:{index}",
            project_id="PVT_acme_7",
            map_id="I_atlas_41",
            event_type="map.upserted",
            payload={"version": index},
            committed_at=f"2026-08-23T07:30:{index:02d}Z",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(commit, range(40)))

    events = journal.read(cursor=0, limit=100)["events"]
    assert [event["cursor"] for event in events] == list(range(1, 41))
    assert {event["payload"]["version"] for event in events} == set(range(40))


def test_concurrent_reads_report_one_consistent_journal_snapshot(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    application(storage_root).health()
    journal = BoardEventJournal(
        storage_root / "registry.db",
        settings=BoardEventSettings(max_events=1_000),
    )
    finished = Event()

    def write_versions() -> None:
        for index in range(100):
            journal.commit(
                event_id=f"map:I_atlas_41:snapshot:{index}",
                project_id="PVT_acme_7",
                map_id="I_atlas_41",
                event_type="map.upserted",
                payload={"version": index},
                committed_at=f"2026-08-23T07:31:{index % 60:02d}Z",
            )
        finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(write_versions)
        while not finished.is_set():
            snapshot = journal.read(cursor=0, limit=500)
            assert snapshot["status"] == "open"
            assert snapshot["cursor"] <= snapshot["latest_cursor"]
            assert all(
                event["cursor"] <= snapshot["latest_cursor"]
                for event in snapshot["events"]
            )
        future.result()


def test_expired_cursor_requires_one_full_projection_refresh(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    application(storage_root).health()
    journal = BoardEventJournal(
        storage_root / "registry.db",
        settings=BoardEventSettings(max_events=2, retention_seconds=86_400),
    )
    for index in range(1, 4):
        journal.commit(
            event_id=f"map:I_atlas_41:retention:{index}",
            project_id="PVT_acme_7",
            map_id="I_atlas_41",
            event_type="map.upserted",
            payload={"version": index},
            committed_at=f"2026-08-23T07:30:0{index}Z",
        )

    expired = journal.read(cursor=0)

    assert expired == {
        "status": "refresh_required",
        "reason": "cursor_expired",
        "cursor": 0,
        "retained_after_cursor": 1,
        "latest_cursor": 3,
    }
    resumed = journal.read(cursor=1)
    assert [event["cursor"] for event in resumed["events"]] == [2, 3]
    with pytest.raises(BoardEventConflict, match="different content"):
        journal.commit(
            event_id="map:I_atlas_41:retention:1",
            project_id="PVT_acme_7",
            map_id="I_atlas_41",
            event_type="map.upserted",
            payload={"version": "conflicting-after-retention"},
            committed_at="2026-08-23T07:31:00Z",
        )


def test_time_retention_expires_an_idle_journal_without_a_new_write(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    application(storage_root).health()
    clock = ["2026-08-23T07:30:00Z"]
    journal = BoardEventJournal(
        storage_root / "registry.db",
        settings=BoardEventSettings(max_events=100, retention_seconds=60),
        clock=lambda: clock[0],
    )
    journal.commit(
        event_id="project:PVT_acme_7:idle",
        project_id="PVT_acme_7",
        map_id=None,
        event_type="project.upserted",
        payload={"version": 1},
        committed_at=clock[0],
    )
    clock[0] = "2026-08-23T07:32:00Z"

    expired = journal.read(cursor=0)

    assert expired["status"] == "refresh_required"
    assert expired["retained_after_cursor"] == 1
    assert expired["latest_cursor"] == 1


def test_production_state_writers_physically_prune_configured_history(tmp_path):
    tracker = TwoProjectTrackerBoundary()
    storage_root = tmp_path / "plugin-data" / "map-governance"
    app = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        clock=lambda: datetime(2026, 8, 23, 7, 30, tzinfo=timezone.utc),
        event_settings=BoardEventSettings(max_events=2),
    )
    project = app.configure_project(project_url=PROJECT_URL)
    card = app.bind_map(project_id=project["id"], issue_url=ISSUE_URL)

    app.transition_map(
        map_id=card["id"],
        expected_stage="authorized",
        requested_stage="parked",
    )

    with sqlite3.connect(storage_root / "registry.db") as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM board_events").fetchone()[0] <= 2
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM board_event_identities"
            ).fetchone()[0]
            > 2
        )
    assert app.board_events(cursor=0)["status"] == "refresh_required"


def test_lagging_consumer_never_holds_or_blocks_governance_writers(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    application(storage_root).health()
    settings = BoardEventSettings(max_events=5, retention_seconds=1_000_000_000)
    journal = BoardEventJournal(storage_root / "registry.db", settings=settings)
    journal.commit(
        event_id="consumer-baseline",
        project_id="PVT_acme_7",
        map_id=None,
        event_type="project.upserted",
        payload={"version": 0},
        committed_at="2026-08-23T07:30:00Z",
    )
    lagging_cursor = 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda index: journal.commit(
                    event_id=f"writer:{index}",
                    project_id="PVT_acme_7",
                    map_id="I_atlas_41",
                    event_type="map.upserted",
                    payload={"version": index},
                    committed_at=f"2026-08-23T07:{31 + index // 60:02d}:{index % 60:02d}Z",
                ),
                range(100),
            )
        )

    assert journal.latest_cursor() == 101
    expired = journal.read(cursor=lagging_cursor)
    assert expired["status"] == "refresh_required"
    assert expired["latest_cursor"] == 101
    restarted = BoardEventJournal(storage_root / "registry.db", settings=settings)
    assert restarted.read(cursor=96)["cursor"] == 101


def test_one_project_stales_independently_and_only_reconcile_clears_it(tmp_path):
    tracker = TwoProjectTrackerBoundary()
    app = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
        tracker=tracker,
        clock=lambda: datetime(2026, 8, 23, 7, 30, tzinfo=timezone.utc),
    )
    acme = app.configure_project(project_url=PROJECT_URL)
    octocat = app.configure_project(
        project_url="https://github.com/users/octocat/projects/3"
    )
    acme_map = app.bind_map(project_id=acme["id"], issue_url=ISSUE_URL)
    octocat_map = app.bind_map(
        project_id=octocat["id"],
        issue_url="https://github.com/octocat/hello-world/issues/9",
    )
    tracker.unavailable_projects.add(acme["id"])

    degraded = app.reconcile_project(project_id=acme["id"])

    projects = {project["id"]: project for project in degraded["projects"]}
    assert projects[acme["id"]]["authority"] == {
        "state": "stale",
        "last_success_at": "2026-08-23T07:30:00Z",
        "reason": "Tracker authority is unreachable",
        "sources": [
            {
                "source": "tracker",
                "state": "stale",
                "last_success_at": "2026-08-23T07:30:00Z",
                "reason": "Tracker authority is unreachable",
            }
        ],
        "recovery": "Reconnect tracker authority and complete authoritative reconcile.",
    }
    assert projects[octocat["id"]]["authority"]["state"] == "healthy"
    assert (
        next(card for card in degraded["maps"] if card["id"] == acme_map["id"])["stage"]
        == "authorized"
    )

    with pytest.raises(StaleProjectionError, match="authoritative reconcile"):
        app.transition_map(
            map_id=acme_map["id"],
            expected_stage="authorized",
            requested_stage="parked",
        )
    assert (
        app.transition_map(
            map_id=octocat_map["id"],
            expected_stage="authorized",
            requested_stage="parked",
        )["stage"]
        == "parked"
    )

    tracker.unavailable_projects.remove(acme["id"])
    recovered = app.reconcile_project(project_id=acme["id"])
    recovered_projects = {project["id"]: project for project in recovered["projects"]}
    assert recovered_projects[acme["id"]]["authority"]["state"] == "healthy"
    events = app.board_events(cursor=0, limit=100)["events"]
    assert any(event["type"] == "reconcile.completed" for event in events)
    assert [
        event["payload"]["state"]
        for event in events
        if event["type"] == "reachability.updated" and event["project_id"] == acme["id"]
    ] == ["reconciling", "stale", "reconciling"]


def test_stage_and_outbox_events_follow_their_committed_state_changes(tmp_path):
    tracker = TwoProjectTrackerBoundary()
    app = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
        tracker=tracker,
        clock=lambda: datetime(2026, 8, 23, 7, 30, tzinfo=timezone.utc),
    )
    project = app.configure_project(project_url=PROJECT_URL)
    card = app.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    cursor = app.board_events(cursor=0)["cursor"]

    app.transition_map(
        map_id=card["id"],
        expected_stage="authorized",
        requested_stage="parked",
    )
    committed = app.board_events(cursor=cursor, limit=100)["events"]

    assert [event["type"] for event in committed] == [
        "outbox.updated",
        "outbox.updated",
        "map.upserted",
        "outbox.updated",
    ]
    assert [
        event["payload"]["effect"]["state"]
        for event in committed
        if event["type"] == "outbox.updated"
    ] == ["pending", "leased", "succeeded"]
    assert (
        next(event for event in committed if event["type"] == "map.upserted")[
            "payload"
        ]["card"]["stage"]
        == "parked"
    )


def test_populated_schema_v7_migrates_without_losing_governance_state(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    tracker = TwoProjectTrackerBoundary()
    app = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        clock=lambda: datetime(2026, 8, 23, 7, 30, tzinfo=timezone.utc),
    )
    project = app.configure_project(project_url=PROJECT_URL)
    card = app.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    app.transition_map(
        map_id=card["id"],
        expected_stage="authorized",
        requested_stage="parked",
    )
    database = storage_root / "registry.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO decision_projections(
                map_id, decision_id, decision_type, rationale, authority,
                affected_stage, decided_at, tracker_record_id,
                tracker_record_url, confirmed_at
            ) VALUES (?, 'decision-legacy', 'product', 'Keep the launch narrow.',
                      'ceo', 'delivery', ?, 'IC_decision_legacy', ?, ?)
            """,
            (
                card["id"],
                "2026-08-23T07:30:00Z",
                f"{ISSUE_URL}#issuecomment-decision-legacy",
                "2026-08-23T07:30:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO approval_ledger(
                request_id, map_id, decision_class, proposed_action,
                alternatives_json, rationale, cost_risk, evidence_json,
                requested_scope_json, decision_payload_json, payload_hash,
                packet_hash, status, requested_by_profile,
                requested_by_session, requested_at, tracker_request_id,
                tracker_request_url, decided_by, decided_by_profile,
                decision_note, decided_at, expires_at, tracker_decision_id,
                tracker_decision_url, updated_at
            ) VALUES (
                'approval-legacy', ?, 'delivery_authorization', 'transition_map',
                '["authorize","revise"]', 'Validated scope.', 'One week.',
                '["issue-evidence"]', ?, ?, 'sha256:legacy-payload',
                'sha256:legacy-packet', 'approved', 'ceo', 'session-legacy', ?,
                'IC_approval_request', ?, 'chairman-1', 'ceo', 'Approved.', ?, ?,
                'IC_approval_decision', ?, ?
            )
            """,
            (
                card["id"],
                f'{{"map_id":"{card["id"]}"}}',
                '{"expected_stage":"authorized","requested_stage":"delivery"}',
                "2026-08-23T07:30:00Z",
                f"{ISSUE_URL}#issuecomment-approval-request",
                "2026-08-23T07:30:00Z",
                "2026-08-24T07:30:00Z",
                f"{ISSUE_URL}#issuecomment-approval-decision",
                "2026-08-23T07:30:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO approval_ledger_events(
                event_id, request_id, event_type, occurred_at, payload_hash,
                tracker_record_id, tracker_record_url
            ) VALUES ('approval:legacy:request', 'approval-legacy', 'requested', ?,
                      'sha256:legacy-payload', 'IC_approval_request', ?)
            """,
            (
                "2026-08-23T07:30:00Z",
                f"{ISSUE_URL}#issuecomment-approval-request",
            ),
        )
        connection.execute(
            """
            INSERT INTO pm_report_projections(
                map_id, record_id, report_type, summary, reported_at,
                evidence_json, blocking, continuation_requirement, failure_code,
                tracker_record_id, tracker_record_url, confirmed_at
            ) VALUES (?, 'pm-legacy', 'checkpoint', 'Delivery remains on track.', ?,
                      '[]', NULL, NULL, NULL, 'IC_pm_legacy', ?, ?)
            """,
            (
                card["id"],
                "2026-08-23T07:30:00Z",
                f"{ISSUE_URL}#issuecomment-pm-legacy",
                "2026-08-23T07:30:00Z",
            ),
        )
        effect = connection.execute(
            "SELECT effect_id, state FROM outbox_intents"
        ).fetchone()
        assert effect is not None and effect[1] == "succeeded"
        approval_count = connection.execute(
            "SELECT COUNT(*) FROM approval_ledger"
        ).fetchone()[0]
        report_count = connection.execute(
            "SELECT COUNT(*) FROM pm_report_projections"
        ).fetchone()[0]
        decision_count = connection.execute(
            "SELECT COUNT(*) FROM decision_projections"
        ).fetchone()[0]
        connection.execute("DROP TABLE board_events")
        connection.execute("DROP TABLE board_event_identities")
        connection.execute("DROP TABLE board_event_resource_heads")
        connection.execute("DROP TABLE board_event_meta")
        connection.execute("DROP TABLE project_reachability")
        connection.execute(
            "UPDATE plugin_metadata SET schema_version = 7 WHERE namespace = ?",
            ("map-governance",),
        )

    restarted = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        clock=lambda: datetime(2026, 8, 23, 7, 31, tzinfo=timezone.utc),
    )

    migrated_card = restarted.map_detail(map_id=card["id"])
    assert migrated_card["stage"] == "parked"
    assert migrated_card["external_effects"]["succeeded_count"] == 1
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM approval_ledger").fetchone()[0]
            == approval_count
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM pm_report_projections").fetchone()[
                0
            ]
            == report_count
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM decision_projections").fetchone()[
                0
            ]
            == decision_count
        )
    assert restarted.board()["projects"][0]["authority"]["state"] == "healthy"
    assert restarted.board_events(cursor=0)["events"] == []
