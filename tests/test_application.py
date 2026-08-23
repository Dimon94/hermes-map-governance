import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from map_governance import MapGovernanceApplication
from map_governance.tracker import TrackerIssue, TrackerProject


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class ControllableTracker:
    def __init__(self):
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
    assert [
        card["tracker"]["identity"] for card in groups[octocat["id"]]["maps"]
    ] == ["octocat/hello-world#9"]
    assert groups[octocat["id"]]["maps"][0]["stage"] == "cancelled"
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
