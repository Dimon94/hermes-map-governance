from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from map_governance.native import register


def test_native_health_uses_context_scoped_plugin_storage(tmp_path, capsys):
    storage_root = tmp_path / "profile" / "plugin-data" / "native-namespace"
    registrations = []
    context = SimpleNamespace(
        state=SimpleNamespace(data_dir=storage_root),
        register_cli_command=lambda **command: registrations.append(command),
    )

    register(context)

    command = registrations[0]
    result = command["handler_fn"](SimpleNamespace(maps_command="health"))
    report = json.loads(capsys.readouterr().out)

    assert command["name"] == "maps"
    assert result == 0
    assert report["status"] == "ready"
    assert set(report["components"]) == {
        "native",
        "dashboard",
        "application",
        "storage",
    }
    assert report["components"]["native"]["command"] == "maps health"
    assert report["components"]["dashboard"]["path"] == "/maps"
    assert Path(report["components"]["storage"]["database"]).is_relative_to(
        storage_root
    )
    assert not (tmp_path / "profile" / "kanban.db").exists()
