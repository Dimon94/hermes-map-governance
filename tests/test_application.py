import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from map_governance import (
    MapGovernanceApplication,
    MapTransitionConflict,
    MapTransitionError,
)
from map_governance.tracker import (
    TrackerConflictError,
    TrackerError,
    TrackerIssue,
    TrackerProject,
)


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class ControllableTracker:
    def __init__(self):
        self.transition_calls = []
        self.before_transition = None
        self.transition_error = None
        self.after_transition_stage = None
        self.projects = {
            "https://github.com/orgs/acme/projects/7": TrackerProject(
                id="PVT_acme_7",
                owner="acme",
                owner_type="organization",
                number=7,
                title="Acme CEO portfolio",
                url="https://github.com/orgs/acme/projects/7",
            ),
            "https://github.com/users/octocat/projects/3": TrackerProject(
                id="PVT_octocat_3",
                owner="octocat",
                owner_type="user",
                number=3,
                title="Octocat CEO portfolio",
                url="https://github.com/users/octocat/projects/3",
            ),
        }
        self.issues = {
            "https://github.com/acme/atlas/issues/41": TrackerIssue(
                id="I_atlas_41",
                repository="acme/atlas",
                number=41,
                title="Map the Atlas launch",
                url="https://github.com/acme/atlas/issues/41",
                state="open",
                state_reason=None,
                labels=("map", "map-stage/authorized"),
            ),
            "https://github.com/acme/atlas/issues/42": TrackerIssue(
                id="I_atlas_42",
                repository="acme/atlas",
                number=42,
                title="Map the Atlas billing model",
                url="https://github.com/acme/atlas/issues/42",
                state="open",
                state_reason=None,
                labels=("map", "map-stage/discovery"),
            ),
            "https://github.com/octocat/hello-world/issues/9": TrackerIssue(
                id="I_hello_9",
                repository="octocat/hello-world",
                number=9,
                title="Map a friendly launch",
                url="https://github.com/octocat/hello-world/issues/9",
                state="closed",
                state_reason="not_planned",
                labels=("map",),
            ),
        }

    def get_project(self, url):
        return self.projects[url]

    def get_issue(self, url):
        return self.issues[url]

    def list_decisions(self, url):
        return []

    def transition_issue_stage(self, url, *, expected_stage, requested_stage):
        observed_projection = (
            self.before_transition() if self.before_transition is not None else None
        )
        self.transition_calls.append(
            {
                "url": url,
                "expected_stage": expected_stage,
                "requested_stage": requested_stage,
                "observed_projection": observed_projection,
            }
        )
        if self.transition_error is not None:
            raise self.transition_error
        issue = self.issues[url]
        current_stages = tuple(
            label.removeprefix("map-stage/")
            for label in issue.labels
            if label.startswith("map-stage/")
        )
        if current_stages != (expected_stage,):
            current_stage = current_stages[0] if len(current_stages) == 1 else "invalid"
            raise TrackerConflictError(
                current_stage=current_stage,
                requested_stage=requested_stage,
            )
        labels = tuple(
            label for label in issue.labels if not label.startswith("map-stage/")
        ) + (f"map-stage/{requested_stage}",)
        committed = replace(issue, labels=labels)
        if self.after_transition_stage is not None:
            committed = replace(
                committed,
                labels=tuple(
                    label
                    for label in committed.labels
                    if not label.startswith("map-stage/")
                )
                + (f"map-stage/{self.after_transition_stage}",),
            )
        self.issues[url] = committed
        return committed


def _application(tmp_path, tracker):
    return MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
        tracker=tracker,
        clock=lambda: datetime(2026, 8, 23, 7, 30, tzinfo=timezone.utc),
    )


def test_health_connects_the_application_to_plugin_owned_storage(tmp_path):
    storage_root = tmp_path / "plugin-data" / "map-governance"
    kanban_database = tmp_path / "kanban.db"
    kanban_database.write_bytes(b"existing-kanban-data")

    report = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
    ).health()

    assert report["status"] == "ready"
    assert report["components"]["application"] == {
        "status": "ready",
        "interface": "MapGovernanceApplication",
    }
    assert report["components"]["storage"] == {
        "status": "ready",
        "namespace": "map-governance",
        "database": str(storage_root / "registry.db"),
    }
    assert (storage_root / "registry.db").is_file()
    assert kanban_database.read_bytes() == b"existing-kanban-data"


def test_board_exposes_a_useful_empty_projection_before_any_maps_are_bound(tmp_path):
    board = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
    ).board()

    assert board == {
        "projects": [],
        "maps": [],
        "empty_state": {
            "title": "No Maps are bound",
            "description": (
                "Bind an existing GitHub Map Issue to start a governance board."
            ),
        },
    }


def test_operator_binds_an_existing_issue_once_as_a_complete_map_card(tmp_path):
    application = _application(tmp_path, ControllableTracker())

    project = application.configure_project(
        project_url="https://github.com/orgs/acme/projects/7"
    )
    first = application.bind_map(
        project_id=project["id"],
        issue_url="https://github.com/acme/atlas/issues/41",
    )
    second = application.bind_map(
        project_id=project["id"],
        issue_url="https://github.com/acme/atlas/issues/41",
    )

    assert first == second
    board = application.board()
    assert [group["id"] for group in board["projects"]] == ["PVT_acme_7"]
    assert board["projects"][0]["title"] == "Acme CEO portfolio"
    assert board["projects"][0]["maps"] == [first]
    assert board["maps"] == [first]
    assert first == {
        "id": "I_atlas_41",
        "project": {
            "id": "PVT_acme_7",
            "url": "https://github.com/orgs/acme/projects/7",
        },
        "tracker": {
            "provider": "github",
            "id": "I_atlas_41",
            "identity": "acme/atlas#41",
            "url": "https://github.com/acme/atlas/issues/41",
        },
        "title": "Map the Atlas launch",
        "stage": "authorized",
        "available_transitions": ["delivery", "parked"],
        "decision_summary": {"count": 0, "latest": None},
        "ceo_session": {"state": "unbound"},
        "last_synchronized_at": "2026-08-23T07:30:00Z",
    }


def test_board_groups_multiple_maps_without_mixing_project_identity(tmp_path):
    application = _application(tmp_path, ControllableTracker())
    acme = application.configure_project(
        project_url="https://github.com/orgs/acme/projects/7"
    )
    octocat = application.configure_project(
        project_url="https://github.com/users/octocat/projects/3"
    )

    application.bind_map(
        project_id=acme["id"],
        issue_url="https://github.com/acme/atlas/issues/41",
    )
    application.bind_map(
        project_id=acme["id"],
        issue_url="https://github.com/acme/atlas/issues/42",
    )
    application.bind_map(
        project_id=octocat["id"],
        issue_url="https://github.com/octocat/hello-world/issues/9",
    )

    groups = {group["id"]: group for group in application.board()["projects"]}
    assert [card["tracker"]["identity"] for card in groups[acme["id"]]["maps"]] == [
        "acme/atlas#41",
        "acme/atlas#42",
    ]
    assert [card["tracker"]["identity"] for card in groups[octocat["id"]]["maps"]] == [
        "octocat/hello-world#9"
    ]
    assert groups[octocat["id"]]["maps"][0]["stage"] == "cancelled"
    assert groups[octocat["id"]]["maps"][0]["available_transitions"] == []
    assert all(
        card["project"]["id"] == group["id"]
        for group in groups.values()
        for card in group["maps"]
    )


def test_refresh_rebuilds_deleted_projections_without_touching_kanban(tmp_path):
    tracker = ControllableTracker()
    application = _application(tmp_path, tracker)
    kanban_database = tmp_path / "kanban.db"
    kanban_database.write_bytes(b"existing-worker-tasks")
    project = application.configure_project(
        project_url="https://github.com/orgs/acme/projects/7"
    )
    application.bind_map(
        project_id=project["id"],
        issue_url="https://github.com/acme/atlas/issues/41",
    )
    tracker.issues["https://github.com/acme/atlas/issues/41"] = replace(
        tracker.issues["https://github.com/acme/atlas/issues/41"],
        title="Atlas launch approved for delivery",
        labels=("map", "map-stage/delivery"),
    )

    registry = tmp_path / "plugin-data" / "map-governance" / "registry.db"
    with sqlite3.connect(registry) as connection:
        connection.execute("DELETE FROM map_projections")
        connection.execute("DELETE FROM project_projections")

    assert application.board()["maps"] == []
    rebuilt = application.refresh(project_id=project["id"])

    assert rebuilt["maps"][0]["title"] == "Atlas launch approved for delivery"
    assert rebuilt["maps"][0]["stage"] == "delivery"
    assert kanban_database.read_bytes() == b"existing-worker-tasks"


def test_open_map_requires_exactly_one_supported_executive_stage(tmp_path):
    tracker = ControllableTracker()
    tracker.issues["https://github.com/acme/atlas/issues/41"] = replace(
        tracker.issues["https://github.com/acme/atlas/issues/41"],
        labels=("map", "map-stage/discovery", "map-stage/delivery"),
    )
    application = _application(tmp_path, tracker)
    project = application.configure_project(
        project_url="https://github.com/orgs/acme/projects/7"
    )

    with pytest.raises(ValueError, match="exactly one supported"):
        application.bind_map(
            project_id=project["id"],
            issue_url="https://github.com/acme/atlas/issues/41",
        )

    assert application.board()["maps"] == []


def test_valid_transition_commits_tracker_before_visible_projection(tmp_path):
    tracker = ControllableTracker()
    application = _application(tmp_path, tracker)
    project = application.configure_project(
        project_url="https://github.com/orgs/acme/projects/7"
    )
    bound = application.bind_map(
        project_id=project["id"],
        issue_url="https://github.com/acme/atlas/issues/41",
    )
    tracker.before_transition = lambda: application.board()["maps"][0]["stage"]

    transitioned = application.transition_map(
        map_id=bound["id"],
        expected_stage="authorized",
        requested_stage="delivery",
    )

    assert tracker.transition_calls == [
        {
            "url": "https://github.com/acme/atlas/issues/41",
            "expected_stage": "authorized",
            "requested_stage": "delivery",
            "observed_projection": "authorized",
        }
    ]
    assert transitioned["stage"] == "delivery"
    assert application.board()["maps"][0]["stage"] == "delivery"


def test_invalid_transition_reports_policy_context_without_tracker_write(tmp_path):
    tracker = ControllableTracker()
    application = _application(tmp_path, tracker)
    project = application.configure_project(
        project_url="https://github.com/orgs/acme/projects/7"
    )
    bound = application.bind_map(
        project_id=project["id"],
        issue_url="https://github.com/acme/atlas/issues/41",
    )

    with pytest.raises(MapTransitionError) as raised:
        application.transition_map(
            map_id=bound["id"],
            expected_stage="authorized",
            requested_stage="acceptance",
        )

    assert raised.value.current_stage == "authorized"
    assert raised.value.requested_stage == "acceptance"
    assert raised.value.reason == "acceptance can only be entered from delivery"
    assert tracker.transition_calls == []
    assert application.board()["maps"][0]["stage"] == "authorized"


def test_tracker_write_failure_keeps_prior_projection_and_surfaces_cause(tmp_path):
    tracker = ControllableTracker()
    application = _application(tmp_path, tracker)
    project = application.configure_project(
        project_url="https://github.com/orgs/acme/projects/7"
    )
    bound = application.bind_map(
        project_id=project["id"],
        issue_url="https://github.com/acme/atlas/issues/41",
    )
    tracker.transition_error = TrackerError(
        "GitHub rejected the label update; check token issue-write permission"
    )

    with pytest.raises(TrackerError, match="issue-write permission"):
        application.transition_map(
            map_id=bound["id"],
            expected_stage="authorized",
            requested_stage="delivery",
        )

    assert len(tracker.transition_calls) == 1
    assert application.board()["maps"][0]["stage"] == "authorized"


def test_concurrent_transition_surfaces_refreshable_conflict_and_one_stage(tmp_path):
    tracker = ControllableTracker()
    application = _application(tmp_path, tracker)
    project = application.configure_project(
        project_url="https://github.com/orgs/acme/projects/7"
    )
    bound = application.bind_map(
        project_id=project["id"],
        issue_url="https://github.com/acme/atlas/issues/41",
    )

    def commit_competing_transition():
        tracker.before_transition = None
        return application.transition_map(
            map_id=bound["id"],
            expected_stage="authorized",
            requested_stage="parked",
        )["stage"]

    tracker.before_transition = commit_competing_transition

    with pytest.raises(MapTransitionConflict) as raised:
        application.transition_map(
            map_id=bound["id"],
            expected_stage="authorized",
            requested_stage="delivery",
        )

    assert raised.value.current_stage == "parked"
    assert raised.value.requested_stage == "delivery"
    assert raised.value.reason == "tracker stage changed; refresh and retry"
    assert raised.value.as_dict()["retryable"] is True
    assert application.board()["maps"][0]["stage"] == "parked"
    assert [
        label
        for label in tracker.issues[bound["tracker"]["url"]].labels
        if label.startswith("map-stage/")
    ] == ["map-stage/parked"]


def test_competing_tracker_write_after_mutation_never_commits_false_projection(
    tmp_path,
):
    tracker = ControllableTracker()
    application = _application(tmp_path, tracker)
    project = application.configure_project(
        project_url="https://github.com/orgs/acme/projects/7"
    )
    bound = application.bind_map(
        project_id=project["id"],
        issue_url="https://github.com/acme/atlas/issues/41",
    )
    tracker.after_transition_stage = "parked"

    with pytest.raises(MapTransitionConflict) as raised:
        application.transition_map(
            map_id=bound["id"],
            expected_stage="authorized",
            requested_stage="delivery",
        )

    assert raised.value.current_stage == "parked"
    assert raised.value.requested_stage == "delivery"
    assert application.board()["maps"][0]["stage"] == "authorized"
    assert tracker.issues[bound["tracker"]["url"]].labels[-1] == "map-stage/parked"


def test_stale_requested_stage_conflicts_before_tracker_mutation(tmp_path):
    tracker = ControllableTracker()
    application = _application(tmp_path, tracker)
    project = application.configure_project(
        project_url="https://github.com/orgs/acme/projects/7"
    )
    bound = application.bind_map(
        project_id=project["id"],
        issue_url="https://github.com/acme/atlas/issues/41",
    )
    issue_url = bound["tracker"]["url"]
    tracker.issues[issue_url] = replace(
        tracker.issues[issue_url],
        labels=("map", "map-stage/delivery"),
    )

    with pytest.raises(MapTransitionConflict) as raised:
        application.transition_map(
            map_id=bound["id"],
            expected_stage="authorized",
            requested_stage="parked",
        )

    assert raised.value.current_stage == "delivery"
    assert raised.value.requested_stage == "parked"
    assert tracker.transition_calls == []
    assert application.board()["maps"][0]["stage"] == "authorized"


@pytest.mark.parametrize(
    ("current_stage", "requested_stage"),
    [
        ("discovery", "awaiting-approval"),
        ("discovery", "parked"),
        ("awaiting-approval", "discovery"),
        ("awaiting-approval", "authorized"),
        ("awaiting-approval", "parked"),
        ("authorized", "delivery"),
        ("authorized", "parked"),
        ("delivery", "decision"),
        ("delivery", "acceptance"),
        ("delivery", "parked"),
        ("decision", "delivery"),
        ("decision", "parked"),
        ("acceptance", "delivery"),
        ("acceptance", "parked"),
        ("parked", "discovery"),
    ],
)
def test_governance_lifecycle_accepts_each_defined_transition(
    tmp_path,
    current_stage,
    requested_stage,
):
    tracker = ControllableTracker()
    issue_url = "https://github.com/acme/atlas/issues/41"
    tracker.issues[issue_url] = replace(
        tracker.issues[issue_url],
        labels=("map", f"map-stage/{current_stage}"),
    )
    application = _application(tmp_path, tracker)
    project = application.configure_project(
        project_url="https://github.com/orgs/acme/projects/7"
    )
    bound = application.bind_map(project_id=project["id"], issue_url=issue_url)

    result = application.transition_map(
        map_id=bound["id"],
        expected_stage=current_stage,
        requested_stage=requested_stage,
    )

    assert result["stage"] == requested_stage
    assert [
        label
        for label in tracker.issues[issue_url].labels
        if label.startswith("map-stage/")
    ] == [f"map-stage/{requested_stage}"]


def test_completed_issue_projects_done_without_an_active_stage_label(tmp_path):
    tracker = ControllableTracker()
    issue_url = "https://github.com/acme/atlas/issues/41"
    tracker.issues[issue_url] = replace(
        tracker.issues[issue_url],
        state="closed",
        state_reason="completed",
        labels=("map",),
    )
    application = _application(tmp_path, tracker)
    project = application.configure_project(
        project_url="https://github.com/orgs/acme/projects/7"
    )

    card = application.bind_map(project_id=project["id"], issue_url=issue_url)

    assert card["stage"] == "done"
    assert card["available_transitions"] == []

    with pytest.raises(MapTransitionError) as raised:
        application.transition_map(
            map_id=card["id"],
            expected_stage="done",
            requested_stage="delivery",
        )

    assert raised.value.current_stage == "done"
    assert raised.value.requested_stage == "delivery"
    assert raised.value.reason == (
        "terminal Map stages can only change by reopening the tracker Issue"
    )
    assert tracker.transition_calls == []
