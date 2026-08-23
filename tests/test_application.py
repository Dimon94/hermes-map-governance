from pathlib import Path

from map_governance import MapGovernanceApplication


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


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
        "maps": [],
        "empty_state": {
            "title": "No Maps are bound",
            "description": (
                "Bind an existing GitHub Map Issue to start a governance board."
            ),
        },
    }
