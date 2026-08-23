"""The single application interface shared by every plugin adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .storage import PluginStorage


class MapGovernanceApplication:
    """Coordinate Map Governance operations behind one public seam."""

    def __init__(self, *, plugin_root: Path, storage_root: Path) -> None:
        self._plugin_root = plugin_root.resolve()
        self._storage = PluginStorage(storage_root)

    def health(self) -> dict[str, Any]:
        """Return readiness for the application and its owned storage."""
        storage = self._storage.check_readiness()
        native = self._native_readiness()
        dashboard = self._dashboard_readiness()
        status = (
            "ready"
            if native["status"] == dashboard["status"] == "ready"
            else "not_ready"
        )
        return {
            "status": status,
            "components": {
                "native": native,
                "dashboard": dashboard,
                "application": {
                    "status": "ready",
                    "interface": type(self).__name__,
                },
                "storage": storage,
            },
        }

    def _native_readiness(self) -> dict[str, str]:
        required_files = (
            self._plugin_root / "plugin.yaml",
            self._plugin_root / "__init__.py",
        )
        return {
            "status": "ready" if all(path.is_file() for path in required_files) else "not_ready",
            "command": "maps health",
        }

    def _dashboard_readiness(self) -> dict[str, str]:
        manifest_path = self._plugin_root / "dashboard" / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entry = self._plugin_root / "dashboard" / manifest["entry"]
            ready = (
                manifest.get("name") == "map-governance"
                and manifest.get("tab", {}).get("path") == "/maps"
                and entry.is_file()
            )
        except (KeyError, OSError, TypeError, ValueError):
            ready = False
        return {
            "status": "ready" if ready else "not_ready",
            "path": "/maps",
        }

    def board(self) -> dict[str, Any]:
        """Return the current governance board projection."""
        return {
            "maps": [],
            "empty_state": {
                "title": "No Maps are bound",
                "description": (
                    "Bind an existing GitHub Map Issue to start a governance board."
                ),
            },
        }
