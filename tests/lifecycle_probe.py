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
    ceo_profile = "default"

    installed = subprocess.run(
        [str(hermes), "plugins", "install", source_url, "--enable"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    install_root = hermes_home / "plugins" / "map-governance"
    assert install_root.is_dir()
    assert (install_root / "plugin.yaml").is_file()
    assert (install_root / "map_governance").is_dir()
    assert (install_root / "dashboard").is_dir()
    assert (install_root / "skills").is_dir()
    for repository_only_path in (
        ".github",
        "AGENTS.md",
        "CLAUDE.md",
        "CONTEXT.md",
        "README.md",
        "docs",
        "tests",
        "tools",
    ):
        assert not (install_root / repository_only_path).exists()
    install_metadata = json.loads(
        (hermes_home / "plugins" / ".install-metadata.json").read_text(encoding="utf-8")
    )
    assert install_metadata["map-governance"]["source"] == source_url

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

    external_skills = hermes_home / "external-skills"
    delivery_skill = external_skills / "delivery-pipeline" / "SKILL.md"
    implement_skill = external_skills / "implement" / "SKILL.md"
    for skill in (delivery_skill, implement_skill):
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("# isolated lifecycle owner Skill\n", encoding="utf-8")
    repository = hermes_home / "project-repository"
    repository.mkdir()
    desired_file = hermes_home / "setup-desired.json"
    desired_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profiles": {"ceo": "ceo", "pm": "pm", "publisher": "publisher"},
                "skills": {
                    "plugin": ["map-governance:ceo", "map-governance:pm"],
                    "external": [
                        {"id": "delivery-pipeline", "path": str(delivery_skill)},
                        {"id": "implement", "path": str(implement_skill)},
                    ],
                },
                "routing": {
                    "policy": "codex",
                    "default_worker": "codex",
                    "unavailable_worker": "blocked",
                },
                "github": {
                    "hostname": "github.com",
                    "projects": [
                        {
                            "id": "PVT_acme_7",
                            "url": "https://github.com/orgs/acme/projects/7",
                            "owner": "acme",
                            "owner_type": "organization",
                            "number": 7,
                            "required_capability": "write",
                        }
                    ],
                    "repositories": [
                        {
                            "coordinate": "acme/atlas",
                            "path": str(repository),
                            "worker_write_required": True,
                            "publication_required": False,
                        }
                    ],
                },
                "authorities": {
                    "worker": {
                        "kind": "local_git",
                        "credential_ref": "local-git",
                    },
                    "publisher": {
                        "kind": "none",
                        "credential_ref": "none",
                        "required": False,
                    },
                },
                "herdr": {"executable": "herdr"},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    setup_plan = run_maps("setup", "plan", "--file", str(desired_file))
    setup_plan_file = hermes_home / "setup-plan.json"
    setup_plan_file.write_text(json.dumps(setup_plan), encoding="utf-8")
    setup_applied = run_maps(
        "setup",
        "apply",
        "--file",
        str(setup_plan_file),
        "--action",
        "config.prerequisites",
    )
    assert setup_applied["readback"] == "confirmed"
    doctor_result = subprocess.run(
        [str(hermes), "maps", "doctor"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert doctor_result.returncode == 1, doctor_result.stdout + doctor_result.stderr
    doctor = json.loads(
        next(
            line
            for line in reversed(doctor_result.stdout.splitlines())
            if line.startswith("{")
        )
    )
    assert doctor["status"] == "fail"
    doctor_checks = {check["id"]: check for check in doctor["checks"]}
    assert doctor_checks["configuration"]["status"] == "pass"
    assert doctor_checks["skills.plugin.ceo"]["status"] == "pass"
    assert doctor_checks["skills.plugin.pm"]["status"] == "pass"

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
    cli_portfolio = run_maps("portfolio", "--health", "healthy", "--stale", "no")
    assert cli_portfolio["read_only"] is True
    assert cli_portfolio["capabilities"]["mutations"] == []
    assert [item["map_id"] for item in cli_portfolio["items"]] == ["I_atlas_41"]

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
    assert plugin_manager.list_plugin_skills("map-governance") == ["ceo", "pm"]
    ceo_skill = plugin_manager.find_plugin_skill("map-governance:ceo")
    pm_skill = plugin_manager.find_plugin_skill("map-governance:pm")
    assert ceo_skill is not None and ceo_skill.is_file()
    assert pm_skill is not None and pm_skill.is_file()
    assert registry.snapshot_registration("map_governance_ceo", scope=None) is None
    assert registry.snapshot_registration("map_governance_pm", scope=None) is None
    assert (
        registry.snapshot_registration("map_governance_pm_dispatch", scope=None) is None
    )
    ceo_tool = registry.snapshot_registration(
        "map_governance_ceo",
        scope=plugin_manager.scope_key,
    )
    assert ceo_tool is not None
    assert ceo_tool.toolset == "map-governance-ceo"
    assert set(ceo_tool.schema["parameters"]["properties"]["action"]["enum"]) == {
        "inspect",
        "record_decision",
        "answer_question",
        "request_approval",
        "commission",
        "resume",
        "runtime_status",
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
    pm_tool = registry.snapshot_registration(
        "map_governance_pm",
        scope=plugin_manager.scope_key,
    )
    assert pm_tool is not None
    assert pm_tool.toolset == "map-governance-pm"
    assert pm_tool.toolset != ceo_tool.toolset
    assert set(pm_tool.schema["parameters"]["properties"]["action"]["enum"]) == {
        "inspect",
        "report",
        "acknowledge_decision",
    }
    pm_definitions = get_tool_definitions(
        enabled_toolsets=["map-governance-pm"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    pm_definition_names = {
        definition["function"]["name"] for definition in pm_definitions
    }
    assert pm_definition_names == {
        "map_governance_pm",
        "map_governance_pm_dispatch",
    }, pm_definition_names

    client = TestClient(web_server.app)
    auth = {"X-Hermes-Session-Token": os.environ["HERMES_DASHBOARD_SESSION_TOKEN"]}
    discovered = client.get("/api/dashboard/plugins", headers=auth)
    assert discovered.status_code == 200, discovered.text
    maps_plugin = next(
        item for item in discovered.json() if item["name"] == "map-governance"
    )
    assert maps_plugin["label"] == "Maps"
    assert maps_plugin["tab"]["path"] == "/maps"

    opened = client.post(
        f"/api/plugins/map-governance/maps/{first_card['id']}/session"
        f"?profile={ceo_profile}",
        headers=auth,
    )
    assert opened.status_code == 200, opened.text
    canonical_session = opened.json()["ceo_session"]
    assert canonical_session["state"] == "ready"
    assert canonical_session["root_session_id"]
    assert canonical_session["live_session_id"]
    reopened = client.post(
        f"/api/plugins/map-governance/maps/{first_card['id']}/session"
        f"?profile={ceo_profile}",
        headers=auth,
    )
    assert reopened.status_code == 200, reopened.text
    assert (
        reopened.json()["ceo_session"]["root_session_id"]
        == (canonical_session["root_session_id"])
    )
    assert (
        reopened.json()["ceo_session"]["live_session_id"]
        == (canonical_session["live_session_id"])
    )
    detail = client.get(
        f"/api/plugins/map-governance/maps/{first_card['id']}?profile={ceo_profile}",
        headers=auth,
    )
    assert detail.status_code == 200, detail.text
    projected_session = detail.json()["ceo_session"]
    assert projected_session["state"] == "ready"

    page = client.get("/maps")
    assert page.status_code == 200, page.text
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
        f"/api/plugins/map-governance/health?profile={ceo_profile}",
        headers=auth,
    )
    assert health.status_code == 200, health.text
    assert health.json()["status"] == "ready"
    board = client.get(
        f"/api/plugins/map-governance/board?profile={ceo_profile}",
        headers=auth,
    )
    assert board.status_code == 200, board.text
    assert len(board.json()["projects"]) == 1
    assert len(board.json()["maps"]) == 1
    assert board.json()["maps"][0]["tracker"]["identity"] == "acme/atlas#41"
    portfolio = client.get(
        "/api/plugins/map-governance/portfolio"
        f"?profile={ceo_profile}&project_id=PVT_acme_7&stage=authorized",
        headers=auth,
    )
    assert portfolio.status_code == 200, portfolio.text
    assert portfolio.json()["read_only"] is True
    assert [item["map_id"] for item in portfolio.json()["items"]] == ["I_atlas_41"]
    assert not (hermes_home / "kanban.db").exists()

    registry_database = Path(health.json()["components"]["storage"]["database"])
    assert registry_database.is_file()
    disabled = subprocess.run(
        [str(hermes), "plugins", "disable", "map-governance"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert disabled.returncode == 0, disabled.stdout + disabled.stderr
    assert install_root.is_dir()
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
                "disabled": True,
                "doctor_available": True,
                "canonical_session_opened": True,
                "payload_isolated": True,
                "removed": True,
                "setup_verified": True,
                "source_recorded": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
