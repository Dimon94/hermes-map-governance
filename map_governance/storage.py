"""Plugin-owned persistence isolated from Hermes Kanban storage."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .approvals import normalized_json
from .events import append_board_event, content_event_id


PLUGIN_STORAGE_NAMESPACE = "map-governance"
SCHEMA_VERSION = 16


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

    def project_binding_for_url(self, project_url: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT project_id, project_url, owner_login, owner_type, project_number
                FROM ceo_projects
                WHERE project_url = ?
                """,
                (project_url,),
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

    def ceo_session_registry(self) -> list[dict[str, Any]]:
        """Return complete binding evidence for explicit operator recovery."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    map_id, profile_name, canonical_identity, canonical_title,
                    root_session_id, live_session_id, state, last_activity_at,
                    bootstrap_hash, repair_reason, repair_candidate_count,
                    updated_at
                FROM ceo_session_bindings
                ORDER BY map_id
                """,
            ).fetchall()
        return [dict(row) for row in rows]

    def ceo_session_root_owner(self, root_session_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT map_id FROM ceo_session_bindings
                WHERE root_session_id = ?
                """,
                (root_session_id,),
            ).fetchone()
        return str(row["map_id"]) if row is not None else None

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
    def approval_lease(self, map_id: str) -> Iterator[None]:
        """Serialize one Map's approval history and protected consumption."""
        with self._map_lease(namespace="approval", map_id=map_id):
            yield

    @contextmanager
    def pm_report_lease(self, map_id: str) -> Iterator[None]:
        """Serialize one Map's PM report confirmation and projection."""
        with self._map_lease(namespace="pm-report", map_id=map_id):
            yield

    @contextmanager
    def pm_turn_lease(self, map_id: str) -> Iterator[None]:
        """Serialize one Map's coordinator turn reservation across processes."""
        with self._map_lease(namespace="pm-turn", map_id=map_id):
            yield

    def save_pm_assignment(
        self,
        *,
        map_id: str,
        profile_name: str,
        session_id: str,
        coordinator_id: str,
        assigned_at: str,
    ) -> bool:
        """Persist one immutable PM request identity to Map relationship."""
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM pm_assignments WHERE map_id = ?",
                (map_id,),
            ).fetchone()
            if existing is not None:
                same = (
                    existing["profile_name"] == profile_name
                    and existing["session_id"] == session_id
                    and existing["coordinator_id"] == coordinator_id
                )
                if not same:
                    raise ValueError("Map already has a different PM assignment")
                return True
            try:
                connection.execute(
                    """
                    INSERT INTO pm_assignments(
                        map_id, profile_name, session_id, coordinator_id,
                        state, assigned_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'idle', ?, ?)
                    """,
                    (
                        map_id,
                        profile_name,
                        session_id,
                        coordinator_id,
                        assigned_at,
                        assigned_at,
                    ),
                )
                self._append_map_resource_event(
                    connection,
                    map_id=map_id,
                    event_type="pm.updated",
                    identity=map_id,
                    payload={
                        "assignment": {
                            "state": "idle",
                            "updated_at": assigned_at,
                        }
                    },
                    committed_at=assigned_at,
                    occurrence_id=f"assignment:{session_id}",
                )
            except sqlite3.IntegrityError as error:
                raise ValueError(
                    "PM request identity is already assigned to another Map"
                ) from error
        return False

    def pm_assignment(self, map_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pm_assignments WHERE map_id = ?",
                (map_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def pm_assignment_for_request(
        self,
        *,
        profile_name: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM pm_assignments
                WHERE profile_name = ? AND session_id = ?
                """,
                (profile_name, session_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def save_pm_control_plane_binding(
        self,
        *,
        profile_name: str,
        session_id: str,
        map_id: str,
        control_profile: str,
        coordinator_id: str,
        registered_at: str,
    ) -> bool:
        """Bind one PM request identity to its profile-scoped CEO control plane."""
        values = tuple(
            str(value).strip()
            for value in (
                profile_name,
                session_id,
                map_id,
                control_profile,
                coordinator_id,
                registered_at,
            )
        )
        if any(not value or len(value) > 512 for value in values):
            raise ValueError("PM control-plane binding has an invalid identity")
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM pm_control_plane_bindings
                WHERE profile_name = ? AND session_id = ?
                """,
                (values[0], values[1]),
            ).fetchone()
            if existing is not None:
                same = all(
                    existing[name] == value
                    for name, value in {
                        "map_id": values[2],
                        "control_profile": values[3],
                        "coordinator_id": values[4],
                    }.items()
                )
                if not same:
                    raise ValueError("PM control-plane identity already has a binding")
                return True
            connection.execute(
                """
                INSERT INTO pm_control_plane_bindings(
                    profile_name, session_id, map_id, control_profile,
                    coordinator_id, registered_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        return False

    def pm_control_plane_for_request(
        self, *, profile_name: str, session_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT profile_name, session_id, map_id, control_profile,
                       coordinator_id, registered_at
                FROM pm_control_plane_bindings
                WHERE profile_name = ? AND session_id = ?
                """,
                (profile_name, session_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def coordinator_lifecycle(self, *, created_at: str) -> str:
        """Return the stable identity for this plugin-owned runtime lifecycle."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO coordinator_lifecycle(
                    singleton, lifecycle_id, created_at
                ) VALUES (1, ?, ?)
                """,
                (str(uuid.uuid4()), created_at),
            )
            row = connection.execute(
                """
                SELECT lifecycle_id FROM coordinator_lifecycle
                WHERE singleton = 1
                """
            ).fetchone()
        if row is None:  # pragma: no cover - guarded by the insert above
            raise RuntimeError("Coordinator lifecycle identity was not persisted")
        return str(row["lifecycle_id"])

    def coordinator_session(self, namespace: str) -> dict[str, Any] | None:
        """Read proof that one deterministic Herdr namespace is plugin-owned."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT session_namespace, lifecycle_id, ownership_marker,
                       state, updated_at
                FROM coordinator_sessions
                WHERE session_namespace = ?
                """,
                (namespace,),
            ).fetchone()
        return dict(row) if row is not None else None

    @contextmanager
    def pm_runtime_lease(self, map_id: str) -> Iterator[None]:
        """Serialize commission/resume across processes for one Map."""
        with self._map_lease(namespace="pm-runtime", map_id=map_id):
            yield

    @contextmanager
    def commission_lease(self, map_id: str) -> Iterator[None]:
        """Serialize the complete authorization-to-delivery state machine."""
        with self._map_lease(namespace="commission", map_id=map_id):
            yield

    @contextmanager
    def coordinator_session_lease(self) -> Iterator[None]:
        """Serialize discovery/start of the one shared plugin Herdr session."""
        with self._map_lease(namespace="coordinator-session", map_id="singleton"):
            yield

    def reserve_pm_runtime(
        self,
        *,
        map_id: str,
        session_namespace: str,
        workspace_label: str,
        agent_id: str,
        ownership_marker: str,
        lifecycle_id: str,
        context: Any,
        updated_at: str,
        session_ownership_marker: str | None = None,
    ) -> bool:
        """Atomically reserve stable identities before any Herdr mutation."""
        session_marker = session_ownership_marker or ownership_marker
        values = {
            "project_id": str(getattr(context, "project_id")),
            "project_url": str(getattr(context, "project_url")),
            "repository": str(getattr(context, "repository")),
            "repository_path": str(getattr(context, "repository_path")),
            "pm_profile": str(getattr(context, "pm_profile")),
            "routing_policy": str(getattr(context, "routing_policy")),
            "herdr_executable": str(getattr(context, "herdr_executable")),
        }
        with self._connect() as connection:
            session = connection.execute(
                """
                SELECT lifecycle_id, ownership_marker
                FROM coordinator_sessions WHERE session_namespace = ?
                """,
                (session_namespace,),
            ).fetchone()
            if session is not None and (
                session["lifecycle_id"] != lifecycle_id
                or session["ownership_marker"] != session_marker
            ):
                raise ValueError("Coordinator session ownership proof changed")
            connection.execute(
                """
                INSERT OR IGNORE INTO coordinator_sessions(
                    session_namespace, lifecycle_id, ownership_marker,
                    state, updated_at
                ) VALUES (?, ?, ?, 'reserved', ?)
                """,
                (
                    session_namespace,
                    lifecycle_id,
                    session_marker,
                    updated_at,
                ),
            )
            existing = connection.execute(
                "SELECT * FROM pm_runtime_bindings WHERE map_id = ?",
                (map_id,),
            ).fetchone()
            if existing is not None:
                same = all(
                    str(existing[name]) == value
                    for name, value in {
                        "session_namespace": session_namespace,
                        "workspace_label": workspace_label,
                        "agent_id": agent_id,
                        "ownership_marker": ownership_marker,
                        "lifecycle_id": lifecycle_id,
                        **values,
                    }.items()
                )
                if not same:
                    raise ValueError("Map runtime reservation changed identity")
                return True
            connection.execute(
                """
                INSERT INTO pm_runtime_bindings(
                    map_id, project_id, project_url, repository,
                    repository_path, pm_profile, routing_policy,
                    herdr_executable, session_namespace, workspace_label, agent_id,
                    ownership_marker, lifecycle_id, state, commissioned_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
                """,
                (
                    map_id,
                    values["project_id"],
                    values["project_url"],
                    values["repository"],
                    values["repository_path"],
                    values["pm_profile"],
                    values["routing_policy"],
                    values["herdr_executable"],
                    session_namespace,
                    workspace_label,
                    agent_id,
                    ownership_marker,
                    lifecycle_id,
                    updated_at,
                    updated_at,
                ),
            )
        return False

    def update_pm_runtime(
        self,
        *,
        map_id: str,
        state: str,
        workspace_id: str | None = None,
        window_id: str | None = None,
        pane_id: str | None = None,
        agent_session_id: str | None = None,
        ready_record_id: str | None = None,
        failure: dict[str, Any] | None = None,
        updated_at: str,
    ) -> None:
        """Advance one runtime record with safe, opaque resume coordinates."""
        failure_json = normalized_json(failure) if failure is not None else None
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE pm_runtime_bindings
                SET state = ?,
                    workspace_id = COALESCE(?, workspace_id),
                    window_id = COALESCE(?, window_id),
                    pane_id = COALESCE(?, pane_id),
                    agent_session_id = COALESCE(?, agent_session_id),
                    ready_record_id = COALESCE(?, ready_record_id),
                    failure_json = ?, updated_at = ?
                WHERE map_id = ?
                """,
                (
                    state,
                    workspace_id,
                    window_id,
                    pane_id,
                    agent_session_id,
                    ready_record_id,
                    failure_json,
                    updated_at,
                    map_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Map runtime has not been reserved")
            connection.execute(
                """
                UPDATE coordinator_sessions
                SET state = CASE
                    WHEN ? = 'repair_required' THEN state
                    ELSE 'owned'
                END, updated_at = ?
                WHERE session_namespace = (
                    SELECT session_namespace FROM pm_runtime_bindings
                    WHERE map_id = ?
                )
                """,
                (state, updated_at, map_id),
            )

    def pm_runtime(self, map_id: str) -> dict[str, Any] | None:
        """Return an executive-safe runtime registry projection."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pm_runtime_bindings WHERE map_id = ?",
                (map_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        failure_json = result.pop("failure_json")
        result["failure"] = json.loads(failure_json) if failure_json else None
        return result

    def pm_runtimes(self) -> list[dict[str, Any]]:
        """Return every durable PM runtime coordinate for restart recovery."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pm_runtime_bindings ORDER BY map_id"
            ).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            failure_json = result.pop("failure_json")
            result["failure"] = json.loads(failure_json) if failure_json else None
            results.append(result)
        return results

    def apply_pm_runtime_binding_repair(
        self,
        *,
        repair_id: str,
        plan_id: str,
        action_id: str,
        map_id: str,
        authorizer: str,
        before: dict[str, Any],
        after: dict[str, Any],
        evidence: dict[str, Any],
        applied_at: str,
    ) -> bool:
        """Atomically replace proven opaque coordinates and record authority."""
        if after.get("state") != "pm_ready":
            raise ValueError("Runtime repair must return to verified PM-ready state")
        with self._connect() as connection:
            if self._identity_repair_replayed(
                connection,
                repair_id=repair_id,
                plan_id=plan_id,
                action_id=action_id,
                map_id=map_id,
                resource_type="pm_runtime",
                authorizer=authorizer,
                before=before,
                after=after,
                evidence=evidence,
            ):
                return False
            row = connection.execute(
                "SELECT * FROM pm_runtime_bindings WHERE map_id = ?",
                (map_id,),
            ).fetchone()
            failure = (
                json.loads(row["failure_json"]) if row and row["failure_json"] else {}
            )
            current = {
                "workspace_id": row["workspace_id"] if row else None,
                "window_id": row["window_id"] if row else None,
                "pane_id": row["pane_id"] if row else None,
                "agent_session_id": row["agent_session_id"] if row else None,
                "state": row["state"] if row else None,
                "repair_reason": failure.get("reason"),
            }
            if row is None or current != before:
                raise ValueError("Repair preview no longer matches the runtime binding")
            connection.execute(
                """
                UPDATE pm_runtime_bindings
                SET workspace_id = ?, window_id = ?, pane_id = ?,
                    agent_session_id = ?, state = 'pm_ready', failure_json = NULL,
                    updated_at = ?
                WHERE map_id = ?
                """,
                (
                    after["workspace_id"],
                    after["window_id"],
                    after["pane_id"],
                    after["agent_session_id"],
                    applied_at,
                    map_id,
                ),
            )
            self._insert_identity_repair_audit(
                connection,
                repair_id=repair_id,
                plan_id=plan_id,
                action_id=action_id,
                map_id=map_id,
                resource_type="pm_runtime",
                authorizer=authorizer,
                before=before,
                after=after,
                evidence=evidence,
                applied_at=applied_at,
            )
            self._append_map_resource_event(
                connection,
                map_id=map_id,
                event_type="pm.updated",
                identity=map_id,
                payload={"runtime": {"state": "pm_ready"}},
                committed_at=applied_at,
                occurrence_id=f"identity-repair:{repair_id}",
            )
        return True

    def begin_pm_turn(
        self,
        *,
        map_id: str,
        coordinator_id: str,
        turn_id: str,
        started_at: str,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pm_assignments WHERE map_id = ?",
                (map_id,),
            ).fetchone()
            if row is None or row["coordinator_id"] != coordinator_id:
                raise ValueError("PM coordinator does not own this assignment")
            if row["state"] == "active":
                if row["active_turn_id"] == turn_id:
                    return True
                raise ValueError("PM assignment already has an active turn")
            connection.execute(
                """
                UPDATE pm_assignments
                SET state = 'active', active_turn_id = ?, updated_at = ?
                WHERE map_id = ? AND state = 'idle'
                """,
                (turn_id, started_at, map_id),
            )
            updated = dict(row)
            updated.update(
                state="active", active_turn_id=turn_id, updated_at=started_at
            )
            self._append_map_resource_event(
                connection,
                map_id=map_id,
                event_type="pm.updated",
                identity=map_id,
                payload={"assignment": self._pm_assignment_event(updated)},
                committed_at=started_at,
                occurrence_id=f"begin:{turn_id}",
            )
        return False

    def finish_pm_turn(
        self,
        *,
        map_id: str,
        turn_id: str,
        outcome: str,
        outcome_id: str,
        finished_at: str,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pm_assignments WHERE map_id = ?",
                (map_id,),
            ).fetchone()
            if row is None:
                raise ValueError("PM assignment is missing")
            if row["state"] == "idle":
                if (
                    row["last_turn_id"] == turn_id
                    and row["last_outcome"] == outcome
                    and row["last_outcome_id"] == outcome_id
                ):
                    return True
                raise ValueError("PM turn is not active")
            if row["active_turn_id"] != turn_id:
                raise ValueError("PM turn identity does not match the active turn")
            connection.execute(
                """
                UPDATE pm_assignments
                SET state = 'idle', active_turn_id = NULL, last_turn_id = ?,
                    last_outcome = ?, last_outcome_id = ?, updated_at = ?
                WHERE map_id = ?
                """,
                (turn_id, outcome, outcome_id, finished_at, map_id),
            )
            updated = dict(row)
            updated.update(
                state="idle",
                active_turn_id=None,
                last_turn_id=turn_id,
                last_outcome=outcome,
                last_outcome_id=outcome_id,
                updated_at=finished_at,
            )
            self._append_map_resource_event(
                connection,
                map_id=map_id,
                event_type="pm.updated",
                identity=map_id,
                payload={"assignment": self._pm_assignment_event(updated)},
                committed_at=finished_at,
                occurrence_id=f"finish:{turn_id}:{outcome}:{outcome_id}",
            )
        return False

    def save_pm_decision_acknowledgment(
        self,
        *,
        map_id: str,
        correlation_id: str,
        turn_id: str,
        outcome: str,
        tracker_record_id: str,
        tracker_record_url: str,
        acknowledged_at: str,
    ) -> bool:
        """Persist the PM transport acknowledgment for one authoritative answer."""
        values = (
            map_id,
            correlation_id,
            turn_id,
            outcome,
            tracker_record_id,
            tracker_record_url,
            acknowledged_at,
        )
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM pm_decision_acknowledgments
                WHERE map_id = ? AND correlation_id = ?
                """,
                (map_id, correlation_id),
            ).fetchone()
            if existing is not None:
                stable = (
                    existing["map_id"],
                    existing["correlation_id"],
                    existing["turn_id"],
                    existing["outcome"],
                    existing["tracker_record_id"],
                    existing["tracker_record_url"],
                    existing["acknowledged_at"],
                )
                if stable != values:
                    raise ValueError(
                        "PM decision correlation has a different acknowledgment"
                    )
                return True
            connection.execute(
                """
                INSERT INTO pm_decision_acknowledgments(
                    map_id, correlation_id, turn_id, outcome, tracker_record_id,
                    tracker_record_url, acknowledged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            acknowledgment = {
                "correlation_id": correlation_id,
                "outcome": outcome,
                "turn_id": turn_id,
                "tracker": {
                    "id": tracker_record_id,
                    "url": tracker_record_url,
                },
                "acknowledged_at": acknowledged_at,
            }
            self._append_map_resource_event(
                connection,
                map_id=map_id,
                event_type="decision-ack.updated",
                identity=f"{map_id}:{correlation_id}",
                payload={"acknowledgment": acknowledgment},
                committed_at=acknowledged_at,
            )
        return False

    def pm_decision_acknowledgments(self, *, map_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM pm_decision_acknowledgments
                WHERE map_id = ?
                ORDER BY julianday(acknowledged_at), correlation_id
                """,
                (map_id,),
            ).fetchall()
        return [
            {
                "correlation_id": row["correlation_id"],
                "outcome": row["outcome"],
                "turn_id": row["turn_id"],
                "tracker": {
                    "id": row["tracker_record_id"],
                    "url": row["tracker_record_url"],
                },
                "acknowledged_at": row["acknowledged_at"],
            }
            for row in rows
        ]

    def pm_resume_receipt(self, *, map_id: str, turn_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM pm_resume_receipts
                WHERE map_id = ? AND turn_id = ?
                """,
                (map_id, turn_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def prepare_pm_resume_receipt(
        self,
        *,
        map_id: str,
        turn_id: str,
        profile_name: str,
        session_id: str,
        coordinator_id: str,
        content_hash: str,
        turn_marker: str,
        prepared_at: str,
    ) -> bool:
        values = (
            map_id,
            turn_id,
            profile_name,
            session_id,
            coordinator_id,
            content_hash,
            turn_marker,
        )
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM pm_resume_receipts
                WHERE map_id = ? AND turn_id = ?
                """,
                (map_id, turn_id),
            ).fetchone()
            if existing is not None:
                stable = tuple(
                    existing[name]
                    for name in (
                        "map_id",
                        "turn_id",
                        "profile_name",
                        "session_id",
                        "coordinator_id",
                        "content_hash",
                        "turn_marker",
                    )
                )
                if stable != values:
                    raise ValueError("PM resume turn has a different receipt")
                if existing["retry_allowed"]:
                    connection.execute(
                        """
                        UPDATE pm_resume_receipts
                        SET delivery_rejected = 0, retry_allowed = 0,
                            prepared_at = ?, prompted_at = ?
                        WHERE map_id = ? AND turn_id = ?
                          AND state = 'dispatching' AND retry_allowed = 1
                        """,
                        (prepared_at, prepared_at, map_id, turn_id),
                    )
                return True
            connection.execute(
                """
                INSERT INTO pm_resume_receipts(
                    map_id, turn_id, profile_name, session_id, coordinator_id,
                    content_hash, turn_marker, state, prepared_at, prompted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'dispatching', ?, ?)
                """,
                (*values, prepared_at, prepared_at),
            )
        return False

    def mark_pm_resume_delivery_rejected(
        self,
        *,
        map_id: str,
        turn_id: str,
        content_hash: str,
    ) -> None:
        """Record an explicit pre-acceptance command rejection for readback."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT state, content_hash FROM pm_resume_receipts
                WHERE map_id = ? AND turn_id = ?
                """,
                (map_id, turn_id),
            ).fetchone()
            if (
                row is None
                or row["state"] != "dispatching"
                or row["content_hash"] != content_hash
            ):
                raise ValueError("PM resume rejection has no matching dispatch")
            connection.execute(
                """
                UPDATE pm_resume_receipts
                SET delivery_rejected = 1, retry_allowed = 0
                WHERE map_id = ? AND turn_id = ? AND state = 'dispatching'
                """,
                (map_id, turn_id),
            )

    def allow_pm_resume_retry(
        self,
        *,
        map_id: str,
        turn_id: str,
        content_hash: str,
    ) -> None:
        """Re-arm only a rejected dispatch whose marker is authoritatively absent."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT state, content_hash, delivery_rejected
                FROM pm_resume_receipts
                WHERE map_id = ? AND turn_id = ?
                """,
                (map_id, turn_id),
            ).fetchone()
            if (
                row is None
                or row["state"] != "dispatching"
                or row["content_hash"] != content_hash
                or not row["delivery_rejected"]
            ):
                raise ValueError("PM resume dispatch is not safe to retry")
            connection.execute(
                """
                UPDATE pm_resume_receipts
                SET retry_allowed = 1
                WHERE map_id = ? AND turn_id = ? AND state = 'dispatching'
                """,
                (map_id, turn_id),
            )

    def confirm_pm_resume_receipt(
        self,
        *,
        map_id: str,
        turn_id: str,
        content_hash: str,
        prompted_at: str,
    ) -> bool:
        """Confirm a prepared exact prompt after apply or owned-agent readback."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM pm_resume_receipts
                WHERE map_id = ? AND turn_id = ?
                """,
                (map_id, turn_id),
            ).fetchone()
            if row is None or row["content_hash"] != content_hash:
                raise ValueError("PM resume receipt was not prepared for this content")
            if row["state"] == "prompted":
                return True
            connection.execute(
                """
                UPDATE pm_resume_receipts
                SET state = 'prompted', prompted_at = ?,
                    delivery_rejected = 0, retry_allowed = 0
                WHERE map_id = ? AND turn_id = ? AND state = 'dispatching'
                """,
                (prompted_at, map_id, turn_id),
            )
        return False

    def save_pm_report_projection(
        self,
        *,
        map_id: str,
        report: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO pm_report_projections(
                    map_id, record_id, report_type, summary, reported_at,
                    evidence_json, blocking, continuation_requirement,
                    failure_code, correlation_id, decision_class, scope_json,
                    options_json, tracker_record_id, tracker_record_url, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(map_id, record_id) DO UPDATE SET
                    report_type = excluded.report_type,
                    summary = excluded.summary,
                    reported_at = excluded.reported_at,
                    evidence_json = excluded.evidence_json,
                    blocking = excluded.blocking,
                    continuation_requirement = excluded.continuation_requirement,
                    failure_code = excluded.failure_code,
                    correlation_id = excluded.correlation_id,
                    decision_class = excluded.decision_class,
                    scope_json = excluded.scope_json,
                    options_json = excluded.options_json,
                    tracker_record_id = excluded.tracker_record_id,
                    tracker_record_url = excluded.tracker_record_url,
                    confirmed_at = excluded.confirmed_at
                """,
                self._pm_report_projection_values(map_id, report),
            )
            self._append_map_resource_event(
                connection,
                map_id=map_id,
                event_type="pm-report.upserted",
                identity=f"{map_id}:{report['record_id']}",
                payload={"report": report},
                committed_at=report["confirmed_at"],
            )

    def replace_pm_report_projections(
        self,
        *,
        map_id: str,
        reports: list[dict[str, Any]],
    ) -> None:
        """Atomically rebuild one Map's PM summary cache from tracker truth."""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM pm_report_projections WHERE map_id = ?",
                (map_id,),
            )
            connection.executemany(
                """
                INSERT INTO pm_report_projections(
                    map_id, record_id, report_type, summary, reported_at,
                    evidence_json, blocking, continuation_requirement,
                    failure_code, correlation_id, decision_class, scope_json,
                    options_json, tracker_record_id, tracker_record_url, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    self._pm_report_projection_values(map_id, report)
                    for report in reports
                ],
            )
            for report in reports:
                self._append_map_resource_event(
                    connection,
                    map_id=map_id,
                    event_type="pm-report.upserted",
                    identity=f"{map_id}:{report['record_id']}",
                    payload={"report": report},
                    committed_at=report["confirmed_at"],
                )

    @staticmethod
    def _pm_report_projection_values(
        map_id: str,
        report: dict[str, Any],
    ) -> tuple[Any, ...]:
        return (
            map_id,
            report["record_id"],
            report["type"],
            report["summary"],
            report["timestamp"],
            json.dumps(report.get("evidence", []), ensure_ascii=False),
            (int(report["blocking"]) if report.get("blocking") is not None else None),
            report.get("continuation_requirement"),
            report.get("failure_code"),
            report.get("correlation_id"),
            report.get("decision_class"),
            (
                json.dumps(report["scope"], ensure_ascii=False, sort_keys=True)
                if report.get("scope") is not None
                else None
            ),
            (
                json.dumps(report.get("options", []), ensure_ascii=False)
                if report.get("correlation_id") is not None
                else None
            ),
            report["tracker"]["id"],
            report["tracker"]["url"],
            report["confirmed_at"],
        )

    def recent_pm_reports(
        self,
        *,
        map_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM pm_report_projections
                WHERE map_id = ?
                ORDER BY julianday(reported_at) DESC, record_id DESC
                LIMIT ?
                """,
                (map_id, limit),
            ).fetchall()
        reports = []
        for row in rows:
            report = {
                "assignment_map_id": map_id,
                "record_id": row["record_id"],
                "type": row["report_type"],
                "summary": row["summary"],
                "timestamp": row["reported_at"],
                "evidence": json.loads(row["evidence_json"]),
                "tracker": {
                    "id": row["tracker_record_id"],
                    "url": row["tracker_record_url"],
                },
                "confirmed_at": row["confirmed_at"],
            }
            if row["blocking"] is not None:
                report["blocking"] = bool(row["blocking"])
            if row["continuation_requirement"] is not None:
                report["continuation_requirement"] = row["continuation_requirement"]
            if row["failure_code"] is not None:
                report["failure_code"] = row["failure_code"]
            if row["correlation_id"] is not None:
                report["correlation_id"] = row["correlation_id"]
                report["decision_class"] = row["decision_class"]
                report["scope"] = json.loads(row["scope_json"])
                report["options"] = json.loads(row["options_json"])
            reports.append(report)
        return reports

    def pm_delivery_summary(self, *, map_id: str) -> dict[str, Any]:
        latest = self.recent_pm_reports(map_id=map_id, limit=1)
        if not latest:
            return {"state": "not_reported"}
        with self._connect() as connection:
            count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM pm_report_projections
                    WHERE map_id = ?
                    """,
                    (map_id,),
                ).fetchone()["count"]
            )
        latest_report = dict(latest[0])
        latest_report.pop("evidence", None)
        badge_type = {
            ("question", False): "non_blocking_question",
            ("question", True): "blocking_question",
            ("blocker", False): "localized_blocker",
            ("blocker", True): "whole_map_blocker",
            ("acceptance", None): "acceptance_request",
            ("failure", None): "terminal_failure",
        }.get(
            (
                latest_report["type"],
                latest_report.get("blocking"),
            )
        )
        return {
            "state": "reported",
            "count": count,
            "latest": latest_report,
            "badges": (
                [{"type": badge_type, "count": 1}] if badge_type is not None else []
            ),
        }

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
            self._append_map_resource_event(
                connection,
                map_id=map_id,
                event_type="session.updated",
                identity=map_id,
                payload={
                    "ceo_session": {
                        "state": "ready",
                        "last_activity_at": last_activity_at,
                    }
                },
                committed_at=updated_at,
                occurrence_id=(
                    f"ready:{root_session_id}:{live_session_id}:{updated_at}"
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
        preserve_existing_identity: bool = False,
    ) -> None:
        with self._connect() as connection:
            if preserve_existing_identity:
                cursor = connection.execute(
                    """
                    UPDATE ceo_session_bindings
                    SET state = 'repair_required', repair_reason = ?,
                        repair_candidate_count = ?, updated_at = ?
                    WHERE map_id = ?
                    """,
                    (reason, candidate_count, updated_at, map_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("CEO session binding is unavailable")
            else:
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
            self._append_map_resource_event(
                connection,
                map_id=map_id,
                event_type="session.updated",
                identity=map_id,
                payload={
                    "ceo_session": {
                        "state": "repair_required",
                        "repair": {
                            "reason": reason,
                            "candidate_count": candidate_count,
                        },
                    }
                },
                committed_at=updated_at,
                occurrence_id=f"repair:{reason}:{candidate_count}:{updated_at}",
            )

    def apply_ceo_session_binding_repair(
        self,
        *,
        repair_id: str,
        plan_id: str,
        action_id: str,
        map_id: str,
        authorizer: str,
        before: dict[str, Any],
        after: dict[str, Any],
        evidence: dict[str, Any],
        last_activity_at: str | None,
        applied_at: str,
    ) -> bool:
        """Atomically rebind one proven lineage and append its operator audit."""
        with self._connect() as connection:
            if self._identity_repair_replayed(
                connection,
                repair_id=repair_id,
                plan_id=plan_id,
                action_id=action_id,
                map_id=map_id,
                resource_type="ceo_session",
                authorizer=authorizer,
                before=before,
                after=after,
                evidence=evidence,
            ):
                return False

            row = connection.execute(
                "SELECT * FROM ceo_session_bindings WHERE map_id = ?",
                (map_id,),
            ).fetchone()
            if row is None or any(
                row[name] != expected
                for name, expected in {
                    "root_session_id": before["root_session_id"],
                    "live_session_id": before["live_session_id"],
                    "state": before["state"],
                    "repair_reason": before["repair_reason"],
                }.items()
            ):
                raise ValueError("Repair preview no longer matches the binding")
            owner = connection.execute(
                """
                SELECT map_id FROM ceo_session_bindings
                WHERE root_session_id = ? AND map_id <> ?
                """,
                (after["root_session_id"], map_id),
            ).fetchone()
            if owner is not None:
                raise ValueError("Replacement CEO lineage belongs to another Map")
            connection.execute(
                """
                UPDATE ceo_session_bindings
                SET root_session_id = ?, live_session_id = ?, state = 'ready',
                    last_activity_at = ?, repair_reason = NULL,
                    repair_candidate_count = NULL, updated_at = ?
                WHERE map_id = ?
                """,
                (
                    after["root_session_id"],
                    after["live_session_id"],
                    last_activity_at,
                    applied_at,
                    map_id,
                ),
            )
            self._insert_identity_repair_audit(
                connection,
                repair_id=repair_id,
                plan_id=plan_id,
                action_id=action_id,
                map_id=map_id,
                resource_type="ceo_session",
                authorizer=authorizer,
                before=before,
                after=after,
                evidence=evidence,
                applied_at=applied_at,
            )
            self._append_map_resource_event(
                connection,
                map_id=map_id,
                event_type="session.updated",
                identity=map_id,
                payload={
                    "ceo_session": {
                        "state": "ready",
                        "last_activity_at": last_activity_at,
                    }
                },
                committed_at=applied_at,
                occurrence_id=f"identity-repair:{repair_id}",
            )
        return True

    @staticmethod
    def _identity_repair_audit_values(
        *,
        plan_id: str,
        action_id: str,
        map_id: str,
        resource_type: str,
        authorizer: str,
        before: dict[str, Any],
        after: dict[str, Any],
        evidence: dict[str, Any],
    ) -> dict[str, str]:
        return {
            "plan_id": plan_id,
            "action_id": action_id,
            "map_id": map_id,
            "resource_type": resource_type,
            "authorizer": authorizer,
            "before_json": normalized_json(before),
            "after_json": normalized_json(after),
            "evidence_json": normalized_json(evidence),
        }

    @classmethod
    def _identity_repair_replayed(
        cls,
        connection: sqlite3.Connection,
        *,
        repair_id: str,
        plan_id: str,
        action_id: str,
        map_id: str,
        resource_type: str,
        authorizer: str,
        before: dict[str, Any],
        after: dict[str, Any],
        evidence: dict[str, Any],
    ) -> bool:
        audit = connection.execute(
            "SELECT * FROM identity_repair_audit WHERE repair_id = ?",
            (repair_id,),
        ).fetchone()
        if audit is None:
            return False
        values = cls._identity_repair_audit_values(
            plan_id=plan_id,
            action_id=action_id,
            map_id=map_id,
            resource_type=resource_type,
            authorizer=authorizer,
            before=before,
            after=after,
            evidence=evidence,
        )
        if any(audit[name] != value for name, value in values.items()):
            raise ValueError("Repair identity was reused with different content")
        return True

    @classmethod
    def _insert_identity_repair_audit(
        cls,
        connection: sqlite3.Connection,
        *,
        repair_id: str,
        plan_id: str,
        action_id: str,
        map_id: str,
        resource_type: str,
        authorizer: str,
        before: dict[str, Any],
        after: dict[str, Any],
        evidence: dict[str, Any],
        applied_at: str,
    ) -> None:
        values = cls._identity_repair_audit_values(
            plan_id=plan_id,
            action_id=action_id,
            map_id=map_id,
            resource_type=resource_type,
            authorizer=authorizer,
            before=before,
            after=after,
            evidence=evidence,
        )
        connection.execute(
            """
            INSERT INTO identity_repair_audit(
                repair_id, plan_id, action_id, map_id, resource_type,
                authorizer, before_json, after_json, evidence_json, applied_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                repair_id,
                values["plan_id"],
                values["action_id"],
                values["map_id"],
                values["resource_type"],
                values["authorizer"],
                values["before_json"],
                values["after_json"],
                values["evidence_json"],
                applied_at,
            ),
        )

    def identity_repair_history(self, *, map_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT repair_id, plan_id, action_id, map_id, resource_type,
                       authorizer, before_json, after_json, evidence_json,
                       applied_at
                FROM identity_repair_audit
                WHERE map_id = ?
                ORDER BY applied_at, repair_id
                """,
                (map_id,),
            ).fetchall()
        return [
            {
                "repair_id": row["repair_id"],
                "plan_id": row["plan_id"],
                "action_id": row["action_id"],
                "map_id": row["map_id"],
                "resource_type": row["resource_type"],
                "authorizer": row["authorizer"],
                "before": json.loads(row["before_json"]),
                "after": json.loads(row["after_json"]),
                "evidence": json.loads(row["evidence_json"]),
                "applied_at": row["applied_at"],
            }
            for row in rows
        ]

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
            payload = {"project": project, "synchronized_at": synchronized_at}
            append_board_event(
                connection,
                event_id=content_event_id("project.upserted", project["id"], payload),
                resource_key=f"project.upserted:{project['id']}",
                project_id=project["id"],
                map_id=None,
                event_type="project.upserted",
                payload=payload,
                committed_at=synchronized_at,
            )

    def save_configured_project(
        self,
        *,
        project: dict[str, Any],
        synchronized_at: str,
    ) -> None:
        """Commit a project binding, projection, and one ordered board event."""
        payload = {"project": project, "synchronized_at": synchronized_at}
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
            connection.execute(
                """
                INSERT INTO project_reachability(
                    project_id, source, state, last_success_at, reason, updated_at
                ) VALUES (?, 'tracker', 'healthy', ?, NULL, ?)
                ON CONFLICT(project_id, source) DO UPDATE SET
                    last_success_at = excluded.last_success_at,
                    updated_at = excluded.updated_at
                WHERE project_reachability.state = 'healthy'
                """,
                (project["id"], synchronized_at, synchronized_at),
            )
            append_board_event(
                connection,
                event_id=content_event_id("project.upserted", project["id"], payload),
                resource_key=f"project.upserted:{project['id']}",
                project_id=project["id"],
                map_id=None,
                event_type="project.upserted",
                payload=payload,
                committed_at=synchronized_at,
            )

    def save_bound_map(self, *, card: dict[str, Any], issue_url: str) -> None:
        """Commit a Map binding, projection, and one ordered board event."""
        payload = {"card": card}
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT project_id, issue_url FROM map_bindings WHERE map_id = ?",
                (card["id"],),
            ).fetchone()
            if existing is not None and (
                existing["project_id"] != card["project_id"]
                or existing["issue_url"] != issue_url
            ):
                raise ValueError("Map Issue is already bound to a different project")
            connection.execute(
                """
                INSERT OR IGNORE INTO map_bindings(map_id, project_id, issue_url)
                VALUES (?, ?, ?)
                """,
                (card["id"], card["project_id"], issue_url),
            )
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
            append_board_event(
                connection,
                event_id=content_event_id("map.upserted", card["id"], payload),
                resource_key=f"map.upserted:{card['id']}",
                project_id=card["project_id"],
                map_id=card["id"],
                event_type="map.upserted",
                payload=payload,
                committed_at=card["synchronized_at"],
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
            payload = {"card": card}
            append_board_event(
                connection,
                event_id=content_event_id("map.upserted", card["id"], payload),
                resource_key=f"map.upserted:{card['id']}",
                project_id=card["project_id"],
                map_id=card["id"],
                event_type="map.upserted",
                payload=payload,
                committed_at=card["synchronized_at"],
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
                payload = {"card": card}
                append_board_event(
                    connection,
                    event_id=content_event_id("map.upserted", card["id"], payload),
                    resource_key=f"map.upserted:{card['id']}",
                    project_id=card["project_id"],
                    map_id=card["id"],
                    event_type="map.upserted",
                    payload=payload,
                    committed_at=card["synchronized_at"],
                )
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
                        projection.synchronized_at,
                        reachability.state AS authority_state,
                        reachability.last_success_at AS authority_last_success_at,
                        reachability.reason AS authority_reason
                    FROM project_projections AS projection
                    JOIN ceo_projects AS binding USING(project_id)
                    LEFT JOIN project_reachability AS reachability
                      ON reachability.project_id = binding.project_id
                     AND reachability.source = 'tracker'
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

    def project_reachability(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT project_id, source, state, last_success_at, reason, updated_at
                FROM project_reachability
                WHERE project_id = ?
                ORDER BY source
                """,
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_project_reachability(
        self,
        *,
        project_id: str,
        source: str,
        state: str,
        reason: str | None,
        changed_at: str,
    ) -> None:
        """Commit one project/source authority state and observable event."""
        if state not in {"healthy", "stale", "reconciling"}:
            raise ValueError(f"Unsupported reachability state: {state}")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT state, last_success_at FROM project_reachability
                WHERE project_id = ? AND source = ?
                """,
                (project_id, source),
            ).fetchone()
            if row is None:
                projection = connection.execute(
                    """
                    SELECT synchronized_at FROM project_projections
                    WHERE project_id = ?
                    """,
                    (project_id,),
                ).fetchone()
                last_success_at = (
                    str(projection["synchronized_at"])
                    if projection is not None
                    else changed_at
                )
            else:
                last_success_at = str(row["last_success_at"])
            if state == "healthy":
                last_success_at = changed_at
                reason = None
            connection.execute(
                """
                INSERT INTO project_reachability(
                    project_id, source, state, last_success_at, reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, source) DO UPDATE SET
                    state = excluded.state,
                    last_success_at = excluded.last_success_at,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (project_id, source, state, last_success_at, reason, changed_at),
            )
            payload = {
                "source": source,
                "state": state,
                "previous_state": str(row["state"]) if row is not None else None,
                "last_success_at": last_success_at,
                "reason": reason,
                "updated_at": changed_at,
            }
            append_board_event(
                connection,
                event_id=content_event_id(
                    "reachability.updated", f"{project_id}:{source}", payload
                ),
                resource_key=f"reachability.updated:{project_id}:{source}",
                project_id=project_id,
                map_id=None,
                event_type="reachability.updated",
                payload=payload,
                committed_at=changed_at,
            )

    def apply_project_reconcile(
        self,
        *,
        project: dict[str, Any],
        cards: list[dict[str, Any]],
        decisions: dict[str, list[dict[str, Any]]],
        reports: dict[str, list[dict[str, Any]]],
        approvals: dict[str, list[dict[str, Any]]],
        synchronized_at: str,
    ) -> None:
        """Atomically publish one complete authoritative project reconcile."""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ceo_projects
                SET project_url = ?, owner_login = ?, owner_type = ?, project_number = ?
                WHERE project_id = ?
                """,
                (
                    project["url"],
                    project["owner"],
                    project["owner_type"],
                    project["number"],
                    project["id"],
                ),
            )
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
            for card in cards:
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
                connection.execute(
                    "DELETE FROM decision_projections WHERE map_id = ?",
                    (card["id"],),
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
                        self._decision_projection_values(card["id"], decision)
                        for decision in decisions.get(card["id"], [])
                    ],
                )
                connection.execute(
                    "DELETE FROM pm_report_projections WHERE map_id = ?",
                    (card["id"],),
                )
                connection.executemany(
                    """
                    INSERT INTO pm_report_projections(
                        map_id, record_id, report_type, summary, reported_at,
                        evidence_json, blocking, continuation_requirement,
                        failure_code, correlation_id, decision_class, scope_json,
                        options_json, tracker_record_id, tracker_record_url,
                        confirmed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        self._pm_report_projection_values(card["id"], report)
                        for report in reports.get(card["id"], [])
                    ],
                )
                self._replace_approval_projections_in_transaction(
                    connection,
                    map_id=card["id"],
                    approvals=approvals.get(card["id"], []),
                )
            connection.execute(
                """
                INSERT INTO project_reachability(
                    project_id, source, state, last_success_at, reason, updated_at
                ) VALUES (?, 'tracker', 'healthy', ?, NULL, ?)
                ON CONFLICT(project_id, source) DO UPDATE SET
                    state = 'healthy', last_success_at = excluded.last_success_at,
                    reason = NULL, updated_at = excluded.updated_at
                """,
                (project["id"], synchronized_at, synchronized_at),
            )
            payload = {
                "project": project,
                "cards": cards,
                "decisions": decisions,
                "pm_reports": reports,
                "approvals": {
                    map_id: [approval["public"] for approval in map_approvals]
                    for map_id, map_approvals in approvals.items()
                },
                "last_success_at": synchronized_at,
            }
            append_board_event(
                connection,
                event_id=content_event_id(
                    "reconcile.completed", project["id"], payload
                ),
                resource_key=f"reconcile.completed:{project['id']}",
                project_id=project["id"],
                map_id=None,
                event_type="reconcile.completed",
                payload=payload,
                committed_at=synchronized_at,
            )

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

    def approval(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM approval_ledger
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
        return self._approval_row(dict(row)) if row is not None else None

    def approvals(self, *, map_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM approval_ledger
                WHERE map_id = ?
                ORDER BY julianday(requested_at) DESC, request_id DESC
                """,
                (map_id,),
            ).fetchall()
        return [self._approval_row(dict(row)) for row in rows]

    def replace_approval_projections(
        self,
        *,
        map_id: str,
        approvals: list[dict[str, Any]],
        reconciled_at: str,
    ) -> None:
        """Rebuild one Map's approval ledger from authoritative Issue history."""
        with self._connect() as connection:
            self._replace_approval_projections_in_transaction(
                connection,
                map_id=map_id,
                approvals=approvals,
            )
            for approval in approvals:
                self._append_map_resource_event(
                    connection,
                    map_id=map_id,
                    event_type="approval.upserted",
                    identity=str(approval["request_id"]),
                    payload={"approval": approval["public"]},
                    committed_at=reconciled_at,
                )

    def _replace_approval_projections_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        map_id: str,
        approvals: list[dict[str, Any]],
    ) -> None:
        requested_ids = {str(approval["request_id"]) for approval in approvals}
        if len(requested_ids) != len(approvals):
            raise ValueError("Authoritative approval history has duplicate requests")
        local_ids = {
            str(row["request_id"])
            for row in connection.execute(
                "SELECT request_id FROM approval_ledger WHERE map_id = ?",
                (map_id,),
            ).fetchall()
        }
        missing = local_ids - requested_ids
        if missing:
            raise ValueError(
                "Authoritative approval history is missing local enforcement records: "
                + ", ".join(sorted(missing))
            )
        for approval in approvals:
            if approval["map_id"] != map_id:
                raise ValueError("Authoritative approval belongs to another Map")
            request_id = str(approval["request_id"])
            local_events = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT event_id, request_id, event_type, occurred_at,
                           actor_id, actor_profile, note, expires_at, payload_hash,
                           tracker_record_id, tracker_record_url
                    FROM approval_ledger_events
                    WHERE request_id = ? AND event_type IN ('expired', 'consumed')
                    ORDER BY event_sequence
                    """,
                    (request_id,),
                ).fetchall()
            ]
            connection.execute(
                """
                INSERT INTO approval_ledger(
                    request_id, map_id, decision_class, proposed_action,
                    alternatives_json, rationale, cost_risk, evidence_json,
                    requested_scope_json, decision_payload_json, payload_hash,
                    packet_hash, status, requested_by_profile,
                    requested_by_session, requested_at, tracker_request_id,
                    tracker_request_url, decided_by, decided_by_profile,
                    decision_note, decided_at, expires_at, tracker_decision_id,
                    tracker_decision_url, consumed_by_mutation_id, consumed_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    map_id = excluded.map_id,
                    decision_class = excluded.decision_class,
                    proposed_action = excluded.proposed_action,
                    alternatives_json = excluded.alternatives_json,
                    rationale = excluded.rationale,
                    cost_risk = excluded.cost_risk,
                    evidence_json = excluded.evidence_json,
                    requested_scope_json = excluded.requested_scope_json,
                    decision_payload_json = excluded.decision_payload_json,
                    payload_hash = excluded.payload_hash,
                    packet_hash = excluded.packet_hash,
                    status = excluded.status,
                    requested_by_profile = excluded.requested_by_profile,
                    requested_by_session = excluded.requested_by_session,
                    requested_at = excluded.requested_at,
                    tracker_request_id = excluded.tracker_request_id,
                    tracker_request_url = excluded.tracker_request_url,
                    decided_by = excluded.decided_by,
                    decided_by_profile = excluded.decided_by_profile,
                    decision_note = excluded.decision_note,
                    decided_at = excluded.decided_at,
                    expires_at = excluded.expires_at,
                    tracker_decision_id = excluded.tracker_decision_id,
                    tracker_decision_url = excluded.tracker_decision_url,
                    consumed_by_mutation_id = excluded.consumed_by_mutation_id,
                    consumed_at = excluded.consumed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    request_id,
                    map_id,
                    approval["decision_class"],
                    approval["proposed_action"],
                    normalized_json(approval["alternatives"]),
                    approval["rationale"],
                    approval["cost_risk"],
                    normalized_json(approval["evidence"]),
                    normalized_json(approval["requested_scope"]),
                    normalized_json(approval["decision_payload"]),
                    approval["payload_hash"],
                    approval["packet_hash"],
                    approval["status"],
                    approval["requested_by_profile"],
                    approval["requested_by_session"],
                    approval["requested_at"],
                    approval["tracker_request_id"],
                    approval["tracker_request_url"],
                    approval["decided_by"],
                    approval["decided_by_profile"],
                    approval["decision_note"],
                    approval["decided_at"],
                    approval["expires_at"],
                    approval["tracker_decision_id"],
                    approval["tracker_decision_url"],
                    approval["consumed_by_mutation_id"],
                    approval["consumed_at"],
                    approval["updated_at"],
                ),
            )
            connection.execute(
                "DELETE FROM approval_ledger_events WHERE request_id = ?",
                (request_id,),
            )
            all_events = [*approval["events"], *local_events]
            connection.executemany(
                """
                INSERT INTO approval_ledger_events(
                    event_id, request_id, event_type, occurred_at, actor_id,
                    actor_profile, note, expires_at, payload_hash,
                    tracker_record_id, tracker_record_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        event["event_id"],
                        request_id,
                        event["event_type"],
                        event["occurred_at"],
                        event.get("actor_id"),
                        event.get("actor_profile"),
                        event.get("note"),
                        event.get("expires_at"),
                        event["payload_hash"],
                        event.get("tracker_record_id"),
                        event.get("tracker_record_url"),
                    )
                    for event in all_events
                ],
            )

    def save_approval_request(
        self,
        *,
        map_id: str,
        packet: dict[str, Any],
        packet_hash: str,
        requested_by_profile: str,
        requested_by_session: str,
        requested_at: str,
        tracker_record_id: str,
        tracker_record_url: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO approval_ledger(
                    request_id, map_id, decision_class, proposed_action,
                    alternatives_json, rationale, cost_risk, evidence_json,
                    requested_scope_json, decision_payload_json, payload_hash,
                    packet_hash, status, requested_by_profile,
                    requested_by_session, requested_at, tracker_request_id,
                    tracker_request_url, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending',
                          ?, ?, ?, ?, ?, ?)
                """,
                (
                    packet["request_id"],
                    map_id,
                    packet["decision_class"],
                    packet["proposed_action"],
                    normalized_json(packet["alternatives"]),
                    packet["rationale"],
                    packet["cost_risk"],
                    normalized_json(packet["evidence"]),
                    normalized_json(packet["requested_scope"]),
                    normalized_json(packet["decision_payload"]),
                    packet["payload_hash"],
                    packet_hash,
                    requested_by_profile,
                    requested_by_session,
                    requested_at,
                    tracker_record_id,
                    tracker_record_url,
                    requested_at,
                ),
            )
            self._append_map_resource_event(
                connection,
                map_id=map_id,
                event_type="approval.upserted",
                identity=packet["request_id"],
                payload={
                    "approval": {
                        **packet,
                        "status": "pending",
                        "requested_at": requested_at,
                        "expires_at": None,
                    }
                },
                committed_at=requested_at,
            )
            connection.execute(
                """
                INSERT INTO approval_ledger_events(
                    event_id, request_id, event_type, occurred_at,
                    actor_profile, payload_hash, tracker_record_id,
                    tracker_record_url
                ) VALUES (?, ?, 'requested', ?, ?, ?, ?, ?)
                """,
                (
                    f"approval:{packet['request_id']}:request",
                    packet["request_id"],
                    requested_at,
                    requested_by_profile,
                    packet["payload_hash"],
                    tracker_record_id,
                    tracker_record_url,
                ),
            )

    def apply_approval_decision(
        self,
        *,
        request_id: str,
        decision: str,
        actor_id: str,
        actor_profile: str,
        note: str,
        decided_at: str,
        expires_at: str | None,
        tracker_record_id: str,
        tracker_record_url: str,
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE approval_ledger
                SET status = ?, decided_by = ?, decided_by_profile = ?,
                    decision_note = ?, decided_at = ?, expires_at = ?,
                    tracker_decision_id = ?, tracker_decision_url = ?,
                    updated_at = ?
                WHERE request_id = ? AND status = 'pending'
                """,
                (
                    decision,
                    actor_id,
                    actor_profile,
                    note,
                    decided_at,
                    expires_at,
                    tracker_record_id,
                    tracker_record_url,
                    decided_at,
                    request_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Approval is no longer pending")
            connection.execute(
                """
                INSERT INTO approval_ledger_events(
                    event_id, request_id, event_type, occurred_at, actor_id,
                    actor_profile, note, expires_at, payload_hash,
                    tracker_record_id, tracker_record_url
                )
                SELECT ?, request_id, ?, ?, ?, ?, ?, ?, payload_hash, ?, ?
                FROM approval_ledger WHERE request_id = ?
                """,
                (
                    f"approval:{request_id}:decision",
                    decision,
                    decided_at,
                    actor_id,
                    actor_profile,
                    note,
                    expires_at,
                    tracker_record_id,
                    tracker_record_url,
                    request_id,
                ),
            )
            approval = connection.execute(
                "SELECT map_id, request_id, status, expires_at FROM approval_ledger WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if approval is not None:
                self._append_map_resource_event(
                    connection,
                    map_id=str(approval["map_id"]),
                    event_type="approval.upserted",
                    identity=request_id,
                    payload={
                        "approval": {
                            "request_id": request_id,
                            "status": decision,
                            "expires_at": expires_at,
                            "decision": {
                                "actor_id": actor_id,
                                "actor_profile": actor_profile,
                                "note": note,
                                "decided_at": decided_at,
                            },
                        }
                    },
                    committed_at=decided_at,
                )

    def revoke_approval(
        self,
        *,
        request_id: str,
        actor_id: str,
        actor_profile: str,
        note: str,
        revoked_at: str,
        tracker_record_id: str,
        tracker_record_url: str,
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE approval_ledger
                SET status = 'revoked', decided_by = ?, decided_by_profile = ?,
                    decision_note = ?, decided_at = ?, expires_at = NULL,
                    tracker_decision_id = ?, tracker_decision_url = ?,
                    updated_at = ?
                WHERE request_id = ? AND status = 'approved'
                """,
                (
                    actor_id,
                    actor_profile,
                    note,
                    revoked_at,
                    tracker_record_id,
                    tracker_record_url,
                    revoked_at,
                    request_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Approval is no longer revocable")
            connection.execute(
                """
                INSERT INTO approval_ledger_events(
                    event_id, request_id, event_type, occurred_at, actor_id,
                    actor_profile, note, payload_hash, tracker_record_id,
                    tracker_record_url
                )
                SELECT ?, request_id, 'revoked', ?, ?, ?, ?, payload_hash, ?, ?
                FROM approval_ledger WHERE request_id = ?
                """,
                (
                    f"approval:{request_id}:revocation",
                    revoked_at,
                    actor_id,
                    actor_profile,
                    note,
                    tracker_record_id,
                    tracker_record_url,
                    request_id,
                ),
            )
            approval = connection.execute(
                "SELECT map_id FROM approval_ledger WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if approval is not None:
                self._append_map_resource_event(
                    connection,
                    map_id=str(approval["map_id"]),
                    event_type="approval.upserted",
                    identity=request_id,
                    payload={
                        "approval": {
                            "request_id": request_id,
                            "status": "revoked",
                            "expires_at": None,
                            "decision": {
                                "actor_id": actor_id,
                                "actor_profile": actor_profile,
                                "note": note,
                                "decided_at": revoked_at,
                            },
                        }
                    },
                    committed_at=revoked_at,
                )

    def expire_approval(self, *, request_id: str, expired_at: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE approval_ledger
                SET status = 'expired', updated_at = ?
                WHERE request_id = ? AND status = 'approved'
                """,
                (expired_at, request_id),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO approval_ledger_events(
                        event_id, request_id, event_type, occurred_at, payload_hash
                    )
                    SELECT ?, request_id, 'expired', ?, payload_hash
                    FROM approval_ledger WHERE request_id = ?
                    """,
                    (f"approval:{request_id}:expired", expired_at, request_id),
                )
                approval = connection.execute(
                    "SELECT map_id FROM approval_ledger WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if approval is not None:
                    self._append_map_resource_event(
                        connection,
                        map_id=str(approval["map_id"]),
                        event_type="approval.upserted",
                        identity=request_id,
                        payload={
                            "approval": {
                                "request_id": request_id,
                                "status": "expired",
                                "expires_at": expired_at,
                            }
                        },
                        committed_at=expired_at,
                    )
        return cursor.rowcount == 1

    def approval_history(self, *, request_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, event_type, occurred_at, actor_id,
                       actor_profile, note, expires_at, payload_hash,
                       tracker_record_id, tracker_record_url
                FROM approval_ledger_events
                WHERE request_id = ?
                ORDER BY event_sequence
                """,
                (request_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def protected_mutation(self, mutation_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT mutation_id, map_id, request_id, action, scope_json,
                       payload_json, payload_hash, status, reserved_at,
                       confirmed_at
                FROM protected_mutations
                WHERE mutation_id = ?
                """,
                (mutation_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["scope"] = json.loads(result.pop("scope_json"))
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def reserve_protected_mutation(
        self,
        *,
        mutation_id: str,
        map_id: str,
        request_id: str,
        action: str,
        scope: dict[str, Any],
        payload: dict[str, Any],
        payload_hash: str,
        reserved_at: str,
    ) -> tuple[bool, str]:
        """Atomically bind one approved ledger record to exactly one mutation."""
        with self._connect() as connection:
            return self.reserve_protected_mutation_in_transaction(
                connection,
                mutation_id=mutation_id,
                map_id=map_id,
                request_id=request_id,
                action=action,
                scope=scope,
                payload=payload,
                payload_hash=payload_hash,
                reserved_at=reserved_at,
            )

    def reserve_protected_mutation_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        mutation_id: str,
        map_id: str,
        request_id: str,
        action: str,
        scope: dict[str, Any],
        payload: dict[str, Any],
        payload_hash: str,
        reserved_at: str,
    ) -> tuple[bool, str]:
        """Reserve through a caller-owned plugin DB transaction."""
        existing = connection.execute(
            """
                SELECT map_id, request_id, action, scope_json, payload_json,
                       payload_hash, status
                FROM protected_mutations
                WHERE mutation_id = ?
                """,
            (mutation_id,),
        ).fetchone()
        scope_json = normalized_json(scope)
        payload_json = normalized_json(payload)
        if existing is not None:
            same = (
                existing["map_id"] == map_id
                and existing["request_id"] == request_id
                and existing["action"] == action
                and existing["scope_json"] == scope_json
                and existing["payload_json"] == payload_json
                and existing["payload_hash"] == payload_hash
            )
            if not same:
                raise ValueError("Mutation identity belongs to another payload")
            return True, str(existing["status"])

        cursor = connection.execute(
            """
            UPDATE approval_ledger
            SET status = 'consumed', consumed_by_mutation_id = ?,
                consumed_at = ?, updated_at = ?
            WHERE request_id = ? AND map_id = ? AND status = 'approved'
            """,
            (mutation_id, reserved_at, reserved_at, request_id, map_id),
        )
        if cursor.rowcount != 1:
            row = connection.execute(
                "SELECT status FROM approval_ledger WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            return False, str(row["status"]) if row is not None else "missing"
        connection.execute(
            """
            INSERT INTO protected_mutations(
                mutation_id, map_id, request_id, action, scope_json,
                payload_json, payload_hash, status, reserved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?)
            """,
            (
                mutation_id,
                map_id,
                request_id,
                action,
                scope_json,
                payload_json,
                payload_hash,
                reserved_at,
            ),
        )
        self._append_map_resource_event(
            connection,
            map_id=map_id,
            event_type="approval.upserted",
            identity=request_id,
            payload={
                "approval": {
                    "request_id": request_id,
                    "status": "consumed",
                    "consumption": {
                        "mutation_id": mutation_id,
                        "consumed_at": reserved_at,
                    },
                }
            },
            committed_at=reserved_at,
        )
        connection.execute(
            """
            INSERT INTO approval_ledger_events(
                event_id, request_id, event_type, occurred_at,
                payload_hash
            ) VALUES (?, ?, 'consumed', ?, ?)
            """,
            (
                f"approval:{request_id}:consumed:{mutation_id}",
                request_id,
                reserved_at,
                payload_hash,
            ),
        )
        return False, "reserved"

    @contextmanager
    def atomic(self) -> Iterator[sqlite3.Connection]:
        """Open one immediate transaction across plugin-owned repositories."""
        with self._connect() as connection:
            connection.commit()  # finish readiness metadata before explicit BEGIN
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    def confirm_protected_mutation(
        self,
        *,
        mutation_id: str,
        confirmed_at: str,
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE protected_mutations
                SET status = 'confirmed', confirmed_at = ?
                WHERE mutation_id = ? AND status IN ('reserved', 'confirmed')
                """,
                (confirmed_at, mutation_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Protected mutation reservation is missing")

    @staticmethod
    def _approval_row(row: dict[str, Any]) -> dict[str, Any]:
        row["alternatives"] = json.loads(row.pop("alternatives_json"))
        row["evidence"] = json.loads(row.pop("evidence_json"))
        row["requested_scope"] = json.loads(row.pop("requested_scope_json"))
        row["decision_payload"] = json.loads(row.pop("decision_payload_json"))
        return row

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
            self._append_map_resource_event(
                connection,
                map_id=map_id,
                event_type="decision.upserted",
                identity=f"{map_id}:{decision['decision_id']}",
                payload={"decision": decision},
                committed_at=decision["confirmed_at"],
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
            for decision in decisions:
                self._append_map_resource_event(
                    connection,
                    map_id=map_id,
                    event_type="decision.upserted",
                    identity=f"{map_id}:{decision['decision_id']}",
                    payload={"decision": decision},
                    committed_at=decision["confirmed_at"],
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

    @staticmethod
    def _pm_assignment_event(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": row["state"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _append_map_resource_event(
        connection: sqlite3.Connection,
        *,
        map_id: str,
        event_type: str,
        identity: str,
        payload: dict[str, Any],
        committed_at: str,
        occurrence_id: str | None = None,
    ) -> None:
        binding = connection.execute(
            "SELECT project_id FROM map_bindings WHERE map_id = ?", (map_id,)
        ).fetchone()
        if binding is None:
            raise ValueError(f"Map is not bound: {map_id}")
        append_board_event(
            connection,
            event_id=content_event_id(
                event_type,
                (
                    identity
                    if occurrence_id is None
                    else (
                        identity
                        + ":occurrence:"
                        + hashlib.sha256(occurrence_id.encode()).hexdigest()[:24]
                    )
                ),
                payload,
            ),
            resource_key=f"{event_type}:{identity}",
            project_id=str(binding["project_id"]),
            map_id=map_id,
            event_type=event_type,
            payload=payload,
            committed_at=committed_at,
        )

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
            CREATE TABLE IF NOT EXISTS project_reachability (
                project_id TEXT NOT NULL REFERENCES ceo_projects(project_id),
                source TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('healthy', 'stale', 'reconciling')),
                last_success_at TEXT NOT NULL,
                reason TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(project_id, source)
            )
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO project_reachability(
                project_id, source, state, last_success_at, reason, updated_at
            )
            SELECT project_id, 'tracker', 'healthy', synchronized_at, NULL,
                   synchronized_at
            FROM project_projections
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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS approval_ledger (
                request_id TEXT PRIMARY KEY,
                map_id TEXT NOT NULL REFERENCES map_bindings(map_id),
                decision_class TEXT NOT NULL,
                proposed_action TEXT NOT NULL,
                alternatives_json TEXT NOT NULL,
                rationale TEXT NOT NULL,
                cost_risk TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                requested_scope_json TEXT NOT NULL,
                decision_payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                packet_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'pending', 'approved', 'rejected', 'revision',
                    'revoked', 'expired', 'consumed'
                )),
                requested_by_profile TEXT NOT NULL,
                requested_by_session TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                tracker_request_id TEXT NOT NULL,
                tracker_request_url TEXT NOT NULL,
                decided_by TEXT,
                decided_by_profile TEXT,
                decision_note TEXT,
                decided_at TEXT,
                expires_at TEXT,
                tracker_decision_id TEXT,
                tracker_decision_url TEXT,
                consumed_by_mutation_id TEXT UNIQUE,
                consumed_at TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS protected_mutations (
                mutation_id TEXT PRIMARY KEY,
                map_id TEXT NOT NULL REFERENCES map_bindings(map_id),
                request_id TEXT NOT NULL UNIQUE REFERENCES approval_ledger(request_id),
                action TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('reserved', 'confirmed')),
                reserved_at TEXT NOT NULL,
                confirmed_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS approval_ledger_events (
                event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                request_id TEXT NOT NULL REFERENCES approval_ledger(request_id),
                event_type TEXT NOT NULL CHECK(event_type IN (
                    'requested', 'approved', 'rejected', 'revision',
                    'revoked', 'expired', 'consumed'
                )),
                occurred_at TEXT NOT NULL,
                actor_id TEXT,
                actor_profile TEXT,
                note TEXT,
                expires_at TEXT,
                payload_hash TEXT NOT NULL,
                tracker_record_id TEXT,
                tracker_record_url TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pm_assignments (
                map_id TEXT PRIMARY KEY REFERENCES map_bindings(map_id),
                profile_name TEXT NOT NULL,
                session_id TEXT NOT NULL UNIQUE,
                coordinator_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('idle', 'active')),
                active_turn_id TEXT,
                last_turn_id TEXT,
                last_outcome TEXT,
                last_outcome_id TEXT,
                assigned_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(profile_name, session_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pm_control_plane_bindings (
                profile_name TEXT NOT NULL,
                session_id TEXT NOT NULL,
                map_id TEXT NOT NULL,
                control_profile TEXT NOT NULL,
                coordinator_id TEXT NOT NULL,
                registered_at TEXT NOT NULL,
                PRIMARY KEY(profile_name, session_id),
                UNIQUE(map_id, coordinator_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pm_decision_acknowledgments (
                map_id TEXT NOT NULL REFERENCES map_bindings(map_id),
                correlation_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('continue', 'blocked')),
                tracker_record_id TEXT NOT NULL,
                tracker_record_url TEXT NOT NULL,
                acknowledged_at TEXT NOT NULL,
                PRIMARY KEY(map_id, correlation_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pm_resume_receipts (
                map_id TEXT NOT NULL REFERENCES map_bindings(map_id),
                turn_id TEXT NOT NULL,
                profile_name TEXT NOT NULL,
                session_id TEXT NOT NULL,
                coordinator_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                turn_marker TEXT NOT NULL,
                delivery_rejected INTEGER NOT NULL DEFAULT 0
                    CHECK(delivery_rejected IN (0, 1)),
                retry_allowed INTEGER NOT NULL DEFAULT 0
                    CHECK(retry_allowed IN (0, 1)),
                state TEXT NOT NULL CHECK(state IN ('dispatching', 'prompted')),
                prepared_at TEXT NOT NULL,
                prompted_at TEXT NOT NULL,
                PRIMARY KEY(map_id, turn_id)
            )
            """
        )
        pm_resume_columns = {
            str(column[1])
            for column in connection.execute(
                "PRAGMA table_info(pm_resume_receipts)"
            ).fetchall()
        }
        if "state" not in pm_resume_columns:
            connection.execute(
                "ALTER TABLE pm_resume_receipts "
                "ADD COLUMN state TEXT NOT NULL DEFAULT 'prompted'"
            )
        if "prepared_at" not in pm_resume_columns:
            connection.execute(
                "ALTER TABLE pm_resume_receipts ADD COLUMN prepared_at TEXT"
            )
            connection.execute(
                "UPDATE pm_resume_receipts SET prepared_at = prompted_at "
                "WHERE prepared_at IS NULL"
            )
        if "turn_marker" not in pm_resume_columns:
            connection.execute(
                "ALTER TABLE pm_resume_receipts ADD COLUMN turn_marker TEXT"
            )
        if "delivery_rejected" not in pm_resume_columns:
            connection.execute(
                "ALTER TABLE pm_resume_receipts "
                "ADD COLUMN delivery_rejected INTEGER NOT NULL DEFAULT 0"
            )
        if "retry_allowed" not in pm_resume_columns:
            connection.execute(
                "ALTER TABLE pm_resume_receipts "
                "ADD COLUMN retry_allowed INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pm_report_projections (
                map_id TEXT NOT NULL REFERENCES map_bindings(map_id),
                record_id TEXT NOT NULL,
                report_type TEXT NOT NULL,
                summary TEXT NOT NULL,
                reported_at TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                blocking INTEGER,
                continuation_requirement TEXT,
                failure_code TEXT,
                correlation_id TEXT,
                decision_class TEXT,
                scope_json TEXT,
                options_json TEXT,
                tracker_record_id TEXT NOT NULL,
                tracker_record_url TEXT NOT NULL,
                confirmed_at TEXT NOT NULL,
                PRIMARY KEY(map_id, record_id)
            )
            """
        )
        pm_report_columns = {
            str(column[1])
            for column in connection.execute(
                "PRAGMA table_info(pm_report_projections)"
            ).fetchall()
        }
        for name, declaration in (
            ("correlation_id", "TEXT"),
            ("decision_class", "TEXT"),
            ("scope_json", "TEXT"),
            ("options_json", "TEXT"),
        ):
            if name not in pm_report_columns:
                connection.execute(
                    f"ALTER TABLE pm_report_projections ADD COLUMN {name} {declaration}"
                )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS coordinator_lifecycle (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                lifecycle_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS coordinator_sessions (
                session_namespace TEXT PRIMARY KEY,
                lifecycle_id TEXT NOT NULL,
                ownership_marker TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK(state IN ('reserved', 'owned')),
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pm_runtime_bindings (
                map_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                project_url TEXT NOT NULL,
                repository TEXT NOT NULL,
                repository_path TEXT NOT NULL,
                pm_profile TEXT NOT NULL,
                routing_policy TEXT NOT NULL,
                herdr_executable TEXT NOT NULL,
                session_namespace TEXT NOT NULL
                    REFERENCES coordinator_sessions(session_namespace),
                workspace_label TEXT NOT NULL UNIQUE,
                workspace_id TEXT UNIQUE,
                window_id TEXT UNIQUE,
                pane_id TEXT UNIQUE,
                agent_id TEXT NOT NULL UNIQUE,
                agent_session_id TEXT UNIQUE,
                ownership_marker TEXT NOT NULL,
                lifecycle_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN (
                    'reserved', 'workspace_ready', 'pm_ready',
                    'awaiting_ready', 'ready_confirmed', 'active',
                    'repair_required'
                )),
                ready_record_id TEXT,
                failure_json TEXT,
                commissioned_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS outbox_intents (
                effect_id TEXT PRIMARY KEY,
                effect_type TEXT NOT NULL,
                map_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN (
                    'pending', 'leased', 'succeeded',
                    'retry_scheduled', 'terminal'
                )),
                owner_id TEXT,
                lease_expires_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                acknowledged_at TEXT,
                acknowledgment_json TEXT,
                last_error_type TEXT,
                last_error_message TEXT,
                terminal_reason TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS outbox_attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                effect_id TEXT NOT NULL REFERENCES outbox_intents(effect_id),
                attempt_number INTEGER NOT NULL,
                owner_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                lease_expires_at TEXT NOT NULL,
                finished_at TEXT,
                outcome TEXT NOT NULL CHECK(outcome IN (
                    'claimed', 'lease_expired', 'released', 'succeeded',
                    'retry_scheduled', 'terminal'
                )),
                error_type TEXT,
                error_message TEXT,
                UNIQUE(effect_id, attempt_number)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS outbox_repairs (
                repair_id TEXT PRIMARY KEY,
                effect_id TEXT NOT NULL REFERENCES outbox_intents(effect_id),
                note TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                prior_terminal_reason TEXT NOT NULL,
                prior_attempt_count INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS identity_repair_audit (
                repair_id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                map_id TEXT NOT NULL REFERENCES map_bindings(map_id),
                resource_type TEXT NOT NULL CHECK(resource_type IN (
                    'ceo_session', 'pm_runtime'
                )),
                authorizer TEXT NOT NULL,
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                UNIQUE(plan_id, action_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS board_event_identities (
                event_id TEXT PRIMARY KEY,
                resource_key TEXT NOT NULL,
                project_id TEXT NOT NULL,
                map_id TEXT,
                event_type TEXT NOT NULL,
                payload_hash TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS board_events (
                cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                resource_key TEXT NOT NULL,
                project_id TEXT NOT NULL,
                map_id TEXT,
                event_type TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                committed_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS board_event_resource_heads (
                resource_key TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS board_events_project_cursor
            ON board_events(project_id, cursor)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS board_events_resource_cursor
            ON board_events(resource_key, cursor)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS board_event_meta (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                retained_after_cursor INTEGER NOT NULL DEFAULT 0,
                latest_cursor INTEGER NOT NULL DEFAULT 0,
                max_events INTEGER NOT NULL DEFAULT 10000,
                retention_seconds INTEGER NOT NULL DEFAULT 86400
            )
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO board_event_meta(
                singleton, retained_after_cursor, latest_cursor,
                max_events, retention_seconds
            ) VALUES (1, 0, 0, 10000, 86400)
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO board_event_identities(
                event_id, resource_key, project_id, map_id, event_type,
                payload_hash
            )
            SELECT event_id, resource_key, project_id, map_id, event_type,
                   payload_hash
            FROM board_events
            """
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO board_event_resource_heads(
                resource_key, event_id, payload_hash, payload_json
            )
            SELECT event.resource_key, event.event_id, event.payload_hash,
                   event.payload_json
            FROM board_events AS event
            WHERE event.cursor = (
                SELECT MAX(latest.cursor)
                FROM board_events AS latest
                WHERE latest.resource_key = event.resource_key
            )
            """
        )
