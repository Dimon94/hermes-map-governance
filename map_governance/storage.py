"""Plugin-owned persistence isolated from Hermes Kanban storage."""

from __future__ import annotations

import fcntl
import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


PLUGIN_STORAGE_NAMESPACE = "map-governance"
SCHEMA_VERSION = 4


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

    def map_session_context(self, map_id: str) -> dict[str, Any] | None:
        """Return the bound Map fields needed to establish session content."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    binding.map_id,
                    binding.project_id,
                    binding.issue_url,
                    projection.repository,
                    projection.issue_number,
                    projection.title,
                    projection.stage
                FROM map_bindings AS binding
                JOIN map_projections AS projection USING(map_id)
                WHERE binding.map_id = ?
                """,
                (map_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def ceo_session_binding(self, map_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    map_id, profile_name, canonical_identity, canonical_title,
                    root_session_id, live_session_id, state, last_activity_at,
                    bootstrap_hash, repair_reason, repair_candidate_count,
                    updated_at
                FROM ceo_session_bindings
                WHERE map_id = ?
                """,
                (map_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def ceo_session_bindings(self, *, profile_name: str) -> list[dict[str, Any]]:
        """Return canonical session coordinates for one explicit profile."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    map_id, profile_name, root_session_id, live_session_id, state
                FROM ceo_session_bindings
                WHERE profile_name = ? AND state = 'ready'
                ORDER BY map_id
                """,
                (profile_name,),
            ).fetchall()
        return [dict(row) for row in rows]

    @contextmanager
    def ceo_session_lease(self, map_id: str) -> Iterator[None]:
        """Serialize one Map's adopt-or-mint flow across plugin processes."""
        with self._map_lease(namespace="session", map_id=map_id):
            yield

    @contextmanager
    def decision_lease(self, map_id: str) -> Iterator[None]:
        """Serialize one Map's tracker idempotency check and decision write."""
        with self._map_lease(namespace="decision", map_id=map_id):
            yield

    @contextmanager
    def _map_lease(self, *, namespace: str, map_id: str) -> Iterator[None]:
        lease_root = self.root / f".{namespace}-leases"
        lease_root.mkdir(parents=True, exist_ok=True)
        lease_name = hashlib.sha256(map_id.encode()).hexdigest()
        lease_path = lease_root / f"{lease_name}.lock"
        with lease_path.open("a+b") as lease:
            fcntl.flock(lease.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lease.fileno(), fcntl.LOCK_UN)

    def save_ceo_session_ready(
        self,
        *,
        map_id: str,
        profile_name: str,
        canonical_identity: str,
        canonical_title: str,
        root_session_id: str,
        live_session_id: str,
        last_activity_at: str | None,
        bootstrap_hash: str,
        updated_at: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ceo_session_bindings(
                    map_id, profile_name, canonical_identity, canonical_title,
                    root_session_id, live_session_id, state, last_activity_at,
                    bootstrap_hash, repair_reason, repair_candidate_count,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, ?, NULL, NULL, ?)
                ON CONFLICT(map_id) DO UPDATE SET
                    profile_name = excluded.profile_name,
                    canonical_identity = excluded.canonical_identity,
                    canonical_title = excluded.canonical_title,
                    root_session_id = excluded.root_session_id,
                    live_session_id = excluded.live_session_id,
                    state = 'ready',
                    last_activity_at = excluded.last_activity_at,
                    bootstrap_hash = excluded.bootstrap_hash,
                    repair_reason = NULL,
                    repair_candidate_count = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    map_id,
                    profile_name,
                    canonical_identity,
                    canonical_title,
                    root_session_id,
                    live_session_id,
                    last_activity_at,
                    bootstrap_hash,
                    updated_at,
                ),
            )

    def save_ceo_session_repair_required(
        self,
        *,
        map_id: str,
        profile_name: str,
        canonical_identity: str,
        canonical_title: str,
        reason: str,
        candidate_count: int,
        updated_at: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ceo_session_bindings(
                    map_id, profile_name, canonical_identity, canonical_title,
                    root_session_id, live_session_id, state, last_activity_at,
                    bootstrap_hash, repair_reason, repair_candidate_count,
                    updated_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, 'repair_required', NULL,
                          NULL, ?, ?, ?)
                ON CONFLICT(map_id) DO UPDATE SET
                    profile_name = excluded.profile_name,
                    canonical_identity = excluded.canonical_identity,
                    canonical_title = excluded.canonical_title,
                    state = 'repair_required',
                    repair_reason = excluded.repair_reason,
                    repair_candidate_count = excluded.repair_candidate_count,
                    updated_at = excluded.updated_at
                """,
                (
                    map_id,
                    profile_name,
                    canonical_identity,
                    canonical_title,
                    reason,
                    candidate_count,
                    updated_at,
                ),
            )

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
                        projection.map_id,
                        projection.project_id,
                        projection.repository,
                        projection.issue_number,
                        projection.issue_url,
                        projection.title,
                        projection.stage,
                        COALESCE(session.state, 'unbound') AS ceo_session_state,
                        session.last_activity_at AS ceo_session_last_activity_at,
                        session.repair_reason AS ceo_session_repair_reason,
                        session.repair_candidate_count AS ceo_session_candidate_count,
                        projection.synchronized_at
                    FROM map_projections AS projection
                    LEFT JOIN ceo_session_bindings AS session USING(map_id)
                    ORDER BY
                        projection.project_id,
                        projection.repository,
                        projection.issue_number,
                        projection.map_id
                    """
                )
            ]
        return projects, maps

    def save_authorization_denial(
        self,
        *,
        action: str,
        map_id: str,
        profile_name: str,
        session_id: str,
        reason: str,
        denied_at: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO governance_authorization_denials(
                    action, map_id, profile_name, session_id, reason, denied_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (action, map_id, profile_name, session_id, reason, denied_at),
            )

    def save_decision_projection(
        self,
        *,
        map_id: str,
        decision: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO decision_projections(
                    map_id, decision_id, decision_type, rationale, authority,
                    affected_stage, decided_at, tracker_record_id,
                    tracker_record_url, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(map_id, decision_id) DO UPDATE SET
                    decision_type = excluded.decision_type,
                    rationale = excluded.rationale,
                    authority = excluded.authority,
                    affected_stage = excluded.affected_stage,
                    decided_at = excluded.decided_at,
                    tracker_record_id = excluded.tracker_record_id,
                    tracker_record_url = excluded.tracker_record_url,
                    confirmed_at = excluded.confirmed_at
                """,
                self._decision_projection_values(map_id, decision),
            )

    def replace_decision_projections(
        self,
        *,
        map_id: str,
        decisions: list[dict[str, Any]],
    ) -> None:
        """Atomically rebuild one Map's decisions from tracker truth."""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM decision_projections WHERE map_id = ?",
                (map_id,),
            )
            connection.executemany(
                """
                INSERT INTO decision_projections(
                    map_id, decision_id, decision_type, rationale, authority,
                    affected_stage, decided_at, tracker_record_id,
                    tracker_record_url, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    self._decision_projection_values(map_id, decision)
                    for decision in decisions
                ],
            )

    @staticmethod
    def _decision_projection_values(
        map_id: str,
        decision: dict[str, Any],
    ) -> tuple[Any, ...]:
        return (
            map_id,
            decision["decision_id"],
            decision["type"],
            decision["rationale"],
            decision["authority"],
            decision["affected_stage"],
            decision["timestamp"],
            decision["tracker"]["id"],
            decision["tracker"]["url"],
            decision["confirmed_at"],
        )

    def recent_decisions(
        self,
        *,
        map_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    decision_id, decision_type, rationale, authority,
                    affected_stage, decided_at, tracker_record_id,
                    tracker_record_url, confirmed_at
                FROM decision_projections
                WHERE map_id = ?
                ORDER BY julianday(decided_at) DESC, decision_id DESC
                LIMIT ?
                """,
                (map_id, limit),
            )
            return [self._decision_row(dict(row)) for row in rows]

    def decision_summary(self, *, map_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM decision_projections WHERE map_id = ?",
                (map_id,),
            ).fetchone()["count"]
        latest = self.recent_decisions(map_id=map_id, limit=1)
        return {"count": int(count), "latest": latest[0] if latest else None}

    @staticmethod
    def _decision_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "decision_id": row["decision_id"],
            "type": row["decision_type"],
            "rationale": row["rationale"],
            "authority": row["authority"],
            "affected_stage": row["affected_stage"],
            "timestamp": row["decided_at"],
            "tracker": {
                "id": row["tracker_record_id"],
                "url": row["tracker_record_url"],
            },
            "confirmed_at": row["confirmed_at"],
        }

    def authorization_denials(
        self,
        *,
        map_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT action, map_id, profile_name, session_id, reason, denied_at
            FROM governance_authorization_denials
        """
        parameters: tuple[str, ...] = ()
        if map_id is not None:
            query += " WHERE map_id = ?"
            parameters = (map_id,)
        query += " ORDER BY denial_id"
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters)]

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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ceo_session_bindings (
                map_id TEXT PRIMARY KEY REFERENCES map_bindings(map_id),
                profile_name TEXT NOT NULL,
                canonical_identity TEXT NOT NULL UNIQUE,
                canonical_title TEXT NOT NULL,
                root_session_id TEXT UNIQUE,
                live_session_id TEXT,
                state TEXT NOT NULL CHECK(state IN ('ready', 'repair_required')),
                last_activity_at TEXT,
                bootstrap_hash TEXT,
                repair_reason TEXT,
                repair_candidate_count INTEGER,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS governance_authorization_denials (
                denial_id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                map_id TEXT NOT NULL,
                profile_name TEXT NOT NULL,
                session_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                denied_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS decision_projections (
                map_id TEXT NOT NULL REFERENCES map_bindings(map_id),
                decision_id TEXT NOT NULL,
                decision_type TEXT NOT NULL,
                rationale TEXT NOT NULL,
                authority TEXT NOT NULL,
                affected_stage TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                tracker_record_id TEXT NOT NULL,
                tracker_record_url TEXT NOT NULL,
                confirmed_at TEXT NOT NULL,
                PRIMARY KEY(map_id, decision_id)
            )
            """
        )
