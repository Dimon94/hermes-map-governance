"""Plugin-owned persistence isolated from Hermes Kanban storage."""

from __future__ import annotations

import sqlite3
from pathlib import Path


PLUGIN_STORAGE_NAMESPACE = "map-governance"


class PluginStorage:
    """Own the Map Governance registry database and nothing outside it."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.database = self.root / "registry.db"

    def check_readiness(self) -> dict[str, str]:
        """Create and verify the minimal storage metadata schema."""
        self.root.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS plugin_metadata (
                    namespace TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO plugin_metadata(namespace, schema_version)
                VALUES (?, ?)
                """,
                (PLUGIN_STORAGE_NAMESPACE, 1),
            )
            row = connection.execute(
                "SELECT schema_version FROM plugin_metadata WHERE namespace = ?",
                (PLUGIN_STORAGE_NAMESPACE,),
            ).fetchone()
        if row != (1,):
            raise RuntimeError("Map Governance storage metadata is not readable")
        return {
            "status": "ready",
            "namespace": PLUGIN_STORAGE_NAMESPACE,
            "database": str(self.database),
        }
