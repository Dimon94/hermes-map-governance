"""Plugin-owned persistence isolated from Hermes Kanban storage."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .approvals import normalized_json


PLUGIN_STORAGE_NAMESPACE = "map-governance"
SCHEMA_VERSION = 7


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
                    failure_code, tracker_record_id, tracker_record_url,
                    confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(map_id, record_id) DO UPDATE SET
                    report_type = excluded.report_type,
                    summary = excluded.summary,
                    reported_at = excluded.reported_at,
                    evidence_json = excluded.evidence_json,
                    blocking = excluded.blocking,
                    continuation_requirement = excluded.continuation_requirement,
                    failure_code = excluded.failure_code,
                    tracker_record_id = excluded.tracker_record_id,
                    tracker_record_url = excluded.tracker_record_url,
                    confirmed_at = excluded.confirmed_at
                """,
                self._pm_report_projection_values(map_id, report),
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
                    failure_code, tracker_record_id, tracker_record_url,
                    confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    self._pm_report_projection_values(map_id, report)
                    for report in reports
                ],
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
                tracker_record_id TEXT NOT NULL,
                tracker_record_url TEXT NOT NULL,
                confirmed_at TEXT NOT NULL,
                PRIMARY KEY(map_id, record_id)
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
