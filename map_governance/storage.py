"""Plugin-owned persistence isolated from Hermes Kanban storage."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


PLUGIN_STORAGE_NAMESPACE = "map-governance"
SCHEMA_VERSION = 2


class PluginStorage:
    """Own the Map Governance registry database and nothing outside it."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.database = self.root / "registry.db"

    def check_readiness(self) -> dict[str, str]:
        """Create and verify the minimal storage metadata schema."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT schema_version FROM plugin_metadata WHERE namespace = ?",
                (PLUGIN_STORAGE_NAMESPACE,),
            ).fetchone()
        if row is None or row["schema_version"] != SCHEMA_VERSION:
            raise RuntimeError("Map Governance storage metadata is not readable")
        return {
            "status": "ready",
            "namespace": PLUGIN_STORAGE_NAMESPACE,
            "database": str(self.database),
        }

    def save_project_binding(self, project: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ceo_projects(
                    project_id, project_url, owner_login, owner_type, project_number
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    project_url = excluded.project_url,
                    owner_login = excluded.owner_login,
                    owner_type = excluded.owner_type,
                    project_number = excluded.project_number
                """,
                (
                    project["id"],
                    project["url"],
                    project["owner"],
                    project["owner_type"],
                    project["number"],
                ),
            )

    def project_binding(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT project_id, project_url, owner_login, owner_type, project_number
                FROM ceo_projects
                WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def project_bindings(self, project_id: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT project_id, project_url, owner_login, owner_type, project_number
            FROM ceo_projects
        """
        parameters: tuple[str, ...] = ()
        if project_id is not None:
            query += " WHERE project_id = ?"
            parameters = (project_id,)
        query += " ORDER BY owner_login, project_number, project_id"
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters)]

    def save_map_binding(self, *, map_id: str, project_id: str, issue_url: str) -> None:
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT project_id, issue_url FROM map_bindings WHERE map_id = ?",
                (map_id,),
            ).fetchone()
            if existing is not None and (
                existing["project_id"] != project_id
                or existing["issue_url"] != issue_url
            ):
                raise ValueError("Map Issue is already bound to a different project")
            connection.execute(
                """
                INSERT OR IGNORE INTO map_bindings(map_id, project_id, issue_url)
                VALUES (?, ?, ?)
                """,
                (map_id, project_id, issue_url),
            )

    def map_bindings(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT map_id, project_id, issue_url
                    FROM map_bindings
                    WHERE project_id = ?
                    ORDER BY issue_url, map_id
                    """,
                    (project_id,),
                )
            ]

    def map_binding(self, map_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT map_id, project_id, issue_url
                FROM map_bindings
                WHERE map_id = ?
                """,
                (map_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def save_project_projection(
        self,
        *,
        project: dict[str, Any],
        synchronized_at: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO project_projections(project_id, title, synchronized_at)
                VALUES (?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    title = excluded.title,
                    synchronized_at = excluded.synchronized_at
                """,
                (project["id"], project["title"], synchronized_at),
            )

    def save_map_projection(self, card: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO map_projections(
                    map_id, project_id, repository, issue_number, issue_url,
                    title, stage, ceo_session_state, synchronized_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(map_id) DO UPDATE SET
                    project_id = excluded.project_id,
                    repository = excluded.repository,
                    issue_number = excluded.issue_number,
                    issue_url = excluded.issue_url,
                    title = excluded.title,
                    stage = excluded.stage,
                    ceo_session_state = excluded.ceo_session_state,
                    synchronized_at = excluded.synchronized_at
                """,
                (
                    card["id"],
                    card["project_id"],
                    card["repository"],
                    card["issue_number"],
                    card["issue_url"],
                    card["title"],
                    card["stage"],
                    card["ceo_session_state"],
                    card["synchronized_at"],
                ),
            )

    def compare_and_save_map_projection(
        self,
        card: dict[str, Any],
        *,
        expected_stage: str,
    ) -> str | None:
        """Save a committed tracker projection unless another writer changed it."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE map_projections
                SET
                    project_id = ?,
                    repository = ?,
                    issue_number = ?,
                    issue_url = ?,
                    title = ?,
                    stage = ?,
                    ceo_session_state = ?,
                    synchronized_at = ?
                WHERE map_id = ? AND stage = ?
                """,
                (
                    card["project_id"],
                    card["repository"],
                    card["issue_number"],
                    card["issue_url"],
                    card["title"],
                    card["stage"],
                    card["ceo_session_state"],
                    card["synchronized_at"],
                    card["id"],
                    expected_stage,
                ),
            )
            if cursor.rowcount == 1:
                return None
            row = connection.execute(
                "SELECT stage FROM map_projections WHERE map_id = ?",
                (card["id"],),
            ).fetchone()
            return str(row["stage"]) if row is not None else "missing"

    def board_rows(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with self._connect() as connection:
            projects = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT
                        binding.project_id,
                        binding.project_url,
                        binding.owner_login,
                        binding.owner_type,
                        binding.project_number,
                        projection.title,
                        projection.synchronized_at
                    FROM project_projections AS projection
                    JOIN ceo_projects AS binding USING(project_id)
                    ORDER BY binding.owner_login, binding.project_number, binding.project_id
                    """
                )
            ]
            maps = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT
                        map_id, project_id, repository, issue_number, issue_url,
                        title, stage, ceo_session_state, synchronized_at
                    FROM map_projections
                    ORDER BY project_id, repository, issue_number, map_id
                    """
                )
            ]
        return projects, maps

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            self._ensure_schema(connection)
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS plugin_metadata (
                namespace TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL
            )
            """
        )
        row = connection.execute(
            "SELECT schema_version FROM plugin_metadata WHERE namespace = ?",
            (PLUGIN_STORAGE_NAMESPACE,),
        ).fetchone()
        if row is not None and row["schema_version"] > SCHEMA_VERSION:
            raise RuntimeError(
                "Map Governance storage schema is newer than this plugin"
            )
        connection.execute(
            """
            INSERT INTO plugin_metadata(namespace, schema_version)
            VALUES (?, ?)
            ON CONFLICT(namespace) DO UPDATE SET
                schema_version = excluded.schema_version
            """,
            (PLUGIN_STORAGE_NAMESPACE, SCHEMA_VERSION),
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ceo_projects (
                project_id TEXT PRIMARY KEY,
                project_url TEXT NOT NULL UNIQUE,
                owner_login TEXT NOT NULL,
                owner_type TEXT NOT NULL,
                project_number INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS map_bindings (
                map_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES ceo_projects(project_id),
                issue_url TEXT NOT NULL UNIQUE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS project_projections (
                project_id TEXT PRIMARY KEY REFERENCES ceo_projects(project_id),
                title TEXT NOT NULL,
                synchronized_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS map_projections (
                map_id TEXT PRIMARY KEY REFERENCES map_bindings(map_id),
                project_id TEXT NOT NULL REFERENCES ceo_projects(project_id),
                repository TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                issue_url TEXT NOT NULL,
                title TEXT NOT NULL,
                stage TEXT NOT NULL,
                ceo_session_state TEXT NOT NULL,
                synchronized_at TEXT NOT NULL
            )
            """
        )
