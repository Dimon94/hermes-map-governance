"""Subprocess probe for the public Hermes plugin lifecycle."""

from __future__ import annotations

import json
import os
import shutil
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

    from hermes_cli import web_server
    from starlette.testclient import TestClient

    client = TestClient(web_server.app)
    auth = {
        "X-Hermes-Session-Token": os.environ["HERMES_DASHBOARD_SESSION_TOKEN"]
    }
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
    assert board.json()["maps"] == []
    assert board.json()["empty_state"]["title"] == "No Maps are bound"
    assert not (hermes_home / "kanban.db").exists()

    registry_database = Path(
        health.json()["components"]["storage"]["database"]
    )
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
    assert not any(
        plugin["name"] == "map-governance" for plugin in after_remove.json()
    )
    assert (
        client.get("/dashboard-plugins/map-governance/dist/index.js").status_code
        == 404
    )
    assert not (hermes_home / "kanban.db").exists()

    print(
        json.dumps(
            {
                "installed": True,
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
