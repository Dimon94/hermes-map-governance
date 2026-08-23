"""Subprocess probe for the public Hermes plugin lifecycle."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


def main() -> int:
    source_url = sys.argv[1]
    hermes_home = Path(sys.argv[2])
    dashboard_probe = Path(sys.argv[3])
    hermes = Path(sys.executable).with_name("hermes")

    installed = subprocess.run(
        [str(hermes), "plugins", "install", source_url, "--enable"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    install_root = hermes_home / "plugins" / "map-governance"
    assert install_root.is_dir()

    diagnostic = subprocess.run(
        [str(hermes), "maps", "health"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert diagnostic.returncode == 0, diagnostic.stdout + diagnostic.stderr
    diagnostic_report = json.loads(
        next(
            line
            for line in reversed(diagnostic.stdout.splitlines())
            if line.startswith("{")
        )
    )
    assert diagnostic_report["status"] == "ready"

    def run_maps(*arguments: str) -> dict:
        result = subprocess.run(
            [str(hermes), "maps", *arguments],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(
            next(
                line
                for line in reversed(result.stdout.splitlines())
                if line.startswith("{")
            )
        )

    project = run_maps(
        "project",
        "configure",
        "--url",
        "https://github.com/orgs/acme/projects/7",
    )
    first_card = run_maps(
        "bind",
        "--project",
        project["id"],
        "--issue",
        "https://github.com/acme/atlas/issues/41",
    )
    second_card = run_maps(
        "bind",
        "--project",
        project["id"],
        "--issue",
        "https://github.com/acme/atlas/issues/41",
    )
    assert first_card["id"] == second_card["id"] == "I_atlas_41"
    cli_board = run_maps("board")
    assert len(cli_board["projects"]) == 1
    assert len(cli_board["maps"]) == 1
    assert cli_board["maps"][0]["tracker"]["identity"] == "acme/atlas#41"
    assert cli_board["maps"][0]["stage"] == "authorized"
    assert cli_board["maps"][0]["ceo_session"] == {"state": "unbound"}

    registry_database = Path(diagnostic_report["components"]["storage"]["database"])
    with sqlite3.connect(registry_database) as connection:
        connection.execute("DELETE FROM map_projections")
        connection.execute("DELETE FROM project_projections")
    assert run_maps("board")["maps"] == []
    rebuilt = run_maps("refresh", "--project", project["id"])
    assert len(rebuilt["maps"]) == 1
    assert rebuilt["maps"][0]["tracker"]["id"] == first_card["tracker"]["id"]
    assert not (hermes_home / "kanban.db").exists()

    from hermes_cli import web_server
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    from model_tools import get_tool_definitions
    from starlette.testclient import TestClient
    from tools.registry import registry

    discover_plugins()
    plugin_manager = get_plugin_manager()
    assert plugin_manager.list_plugin_skills("map-governance") == ["ceo"]
    ceo_skill = plugin_manager.find_plugin_skill("map-governance:ceo")
    assert ceo_skill is not None and ceo_skill.is_file()
    assert registry.snapshot_registration("map_governance_ceo", scope=None) is None
    ceo_tool = registry.snapshot_registration(
        "map_governance_ceo",
        scope=plugin_manager.scope_key,
    )
    assert ceo_tool is not None
    assert ceo_tool.toolset == "map-governance-ceo"
    assert set(ceo_tool.schema["parameters"]["properties"]["action"]["enum"]) == {
        "inspect",
        "record_decision",
    }
    ceo_definitions = get_tool_definitions(
        enabled_toolsets=["map-governance-ceo"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    ceo_definition_names = {
        definition["function"]["name"] for definition in ceo_definitions
    }
    assert ceo_definition_names == {"map_governance_ceo"}, ceo_definition_names

    client = TestClient(web_server.app)
    auth = {"X-Hermes-Session-Token": os.environ["HERMES_DASHBOARD_SESSION_TOKEN"]}
    discovered = client.get("/api/dashboard/plugins", headers=auth)
    assert discovered.status_code == 200, discovered.text
    maps_plugin = next(
        item for item in discovered.json() if item["name"] == "map-governance"
    )
    assert maps_plugin["label"] == "Maps"
    assert maps_plugin["tab"]["path"] == "/maps"

    page = client.get("/maps")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    bundle = client.get("/dashboard-plugins/map-governance/dist/index.js")
    assert bundle.status_code == 200
    assert "map-governance" in bundle.text
    installed_bundle = hermes_home / "installed-dashboard-bundle.js"
    installed_bundle.write_bytes(bundle.content)
    node = shutil.which("node")
    assert node is not None, "Node.js is required to execute the installed bundle"
    bundle_probe = subprocess.run(
        [node, str(dashboard_probe), str(installed_bundle)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert bundle_probe.returncode == 0, bundle_probe.stdout + bundle_probe.stderr

    health = client.get(
        "/api/plugins/map-governance/health?profile=default",
        headers=auth,
    )
    assert health.status_code == 200, health.text
    assert health.json()["status"] == "ready"
    board = client.get(
        "/api/plugins/map-governance/board?profile=default",
        headers=auth,
    )
    assert board.status_code == 200, board.text
    assert len(board.json()["projects"]) == 1
    assert len(board.json()["maps"]) == 1
    assert board.json()["maps"][0]["tracker"]["identity"] == "acme/atlas#41"
    assert not (hermes_home / "kanban.db").exists()

    registry_database = Path(health.json()["components"]["storage"]["database"])
    assert registry_database.is_file()
    removed = subprocess.run(
        [str(hermes), "plugins", "remove", "map-governance"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert not install_root.exists()
    assert registry_database.is_file()
    rescanned = client.get("/api/dashboard/plugins/rescan", headers=auth)
    assert rescanned.status_code == 200, rescanned.text
    after_remove = client.get("/api/dashboard/plugins", headers=auth)
    assert after_remove.status_code == 200, after_remove.text
    assert not any(plugin["name"] == "map-governance" for plugin in after_remove.json())
    assert (
        client.get("/dashboard-plugins/map-governance/dist/index.js").status_code == 404
    )
    assert not (hermes_home / "kanban.db").exists()

    print(
        json.dumps(
            {
                "installed": True,
                "map_bound": True,
                "native_discovered": True,
                "dashboard_discovered": True,
                "opened": True,
                "removed": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
