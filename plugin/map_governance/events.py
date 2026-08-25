"""Durable committed board-event journal and resumable cursor reads."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .approvals import normalized_hash, normalized_json


class BoardEventConflict(ValueError):
    """One stable board-event identity was reused for different content."""


@dataclass(frozen=True)
class BoardEventSettings:
    """Bound durable history and each connection's catch-up batch."""

    max_events: int = 10_000
    retention_seconds: int = 86_400
    batch_size: int = 200
    poll_seconds: float = 0.25
    send_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        for name in ("max_events", "retention_seconds", "batch_size"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"Board event {name} must be a positive integer")
        if (
            isinstance(self.poll_seconds, bool)
            or not isinstance(self.poll_seconds, (int, float))
            or self.poll_seconds <= 0
        ):
            raise ValueError("Board event poll_seconds must be positive")
        if (
            isinstance(self.send_timeout_seconds, bool)
            or not isinstance(self.send_timeout_seconds, (int, float))
            or self.send_timeout_seconds <= 0
        ):
            raise ValueError("Board event send_timeout_seconds must be positive")
        if self.batch_size > 500:
            raise ValueError("Board event batch_size must not exceed 500")


def append_board_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    project_id: str,
    map_id: str | None,
    event_type: str,
    payload: Mapping[str, Any],
    committed_at: str,
    resource_key: str | None = None,
) -> bool:
    """Append through a caller-owned transaction after its state mutation."""
    normalized_payload = json.loads(normalized_json(dict(payload)))
    payload_json = normalized_json(normalized_payload)
    payload_hash = normalized_hash(normalized_payload)
    stable_resource = resource_key or event_id
    existing = connection.execute(
        "SELECT * FROM board_event_identities WHERE event_id = ?", (event_id,)
    ).fetchone()
    if existing is not None:
        same = (
            existing["resource_key"] == stable_resource
            and existing["project_id"] == project_id
            and existing["map_id"] == map_id
            and existing["event_type"] == event_type
            and existing["payload_hash"] == payload_hash
        )
        if not same:
            raise BoardEventConflict(
                f"Board event identity has different content: {event_id}"
            )
        return False
    latest = connection.execute(
        """
        SELECT event_id, payload_hash, payload_json
        FROM board_event_resource_heads
        WHERE resource_key = ?
        """,
        (stable_resource,),
    ).fetchone()
    if (
        latest is not None
        and latest["payload_hash"] == payload_hash
        and latest["payload_json"] == payload_json
    ):
        if existing is None:
            connection.execute(
                """
                INSERT INTO board_event_identities(
                    event_id, resource_key, project_id, map_id, event_type,
                    payload_hash
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    stable_resource,
                    project_id,
                    map_id,
                    event_type,
                    payload_hash,
                ),
            )
        return False
    connection.execute(
        """
        INSERT INTO board_event_identities(
            event_id, resource_key, project_id, map_id, event_type,
            payload_hash
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            stable_resource,
            project_id,
            map_id,
            event_type,
            payload_hash,
        ),
    )
    connection.execute(
        """
        INSERT INTO board_events(
            event_id, resource_key, project_id, map_id, event_type, payload_hash,
            payload_json, committed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            stable_resource,
            project_id,
            map_id,
            event_type,
            payload_hash,
            payload_json,
            committed_at,
        ),
    )
    cursor = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    connection.execute(
        """
        UPDATE board_event_meta
        SET latest_cursor = MAX(latest_cursor, ?)
        WHERE singleton = 1
        """,
        (cursor,),
    )
    connection.execute(
        """
        INSERT INTO board_event_resource_heads(
            resource_key, event_id, payload_hash, payload_json
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(resource_key) DO UPDATE SET
            event_id = excluded.event_id,
            payload_hash = excluded.payload_hash,
            payload_json = excluded.payload_json
        """,
        (stable_resource, event_id, payload_hash, payload_json),
    )
    _prune_configured_events(connection, now=committed_at)
    return True


def _prune_configured_events(connection: sqlite3.Connection, *, now: str) -> None:
    metadata = connection.execute(
        """
        SELECT latest_cursor, max_events, retention_seconds
        FROM board_event_meta WHERE singleton = 1
        """
    ).fetchone()
    latest = int(metadata["latest_cursor"])
    count_floor = max(0, latest - int(metadata["max_events"]))
    expired = connection.execute(
        """
        SELECT MAX(cursor) AS cursor
        FROM board_events
        WHERE cursor <= ?
           OR julianday(committed_at) < (
                julianday(?) - (? / 86400.0)
           )
        """,
        (count_floor, now, int(metadata["retention_seconds"])),
    ).fetchone()["cursor"]
    if expired is None:
        return
    floor = int(expired)
    connection.execute("DELETE FROM board_events WHERE cursor <= ?", (floor,))
    connection.execute(
        """
        UPDATE board_event_meta
        SET retained_after_cursor = MAX(retained_after_cursor, ?)
        WHERE singleton = 1
        """,
        (floor,),
    )


def content_event_id(event_type: str, identity: str, payload: Mapping[str, Any]) -> str:
    """Build a stable semantic identity for one absolute resource version."""
    digest = normalized_hash(dict(payload)).removeprefix("sha256:")[:32]
    return f"{event_type}:{identity}:{digest}"


class BoardEventJournal:
    """Read committed events from the plugin SQLite database."""

    def __init__(
        self,
        database: Path,
        *,
        settings: BoardEventSettings | None = None,
        clock: Callable[[], str | datetime] | None = None,
    ) -> None:
        self.database = database.resolve()
        self.settings = settings or BoardEventSettings()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE board_event_meta
                SET max_events = ?, retention_seconds = ?
                WHERE singleton = 1
                """,
                (self.settings.max_events, self.settings.retention_seconds),
            )
            connection.commit()

    def commit(
        self,
        *,
        event_id: str,
        project_id: str,
        map_id: str | None,
        event_type: str,
        payload: Mapping[str, Any],
        committed_at: str,
    ) -> bool:
        """Commit one event, or accept an identical stable-id replay."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            created = append_board_event(
                connection,
                event_id=event_id,
                project_id=project_id,
                map_id=map_id,
                event_type=event_type,
                payload=payload,
                committed_at=committed_at,
                resource_key=event_id,
            )
            connection.commit()
        return created

    def append_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        event_id: str,
        project_id: str,
        map_id: str | None,
        event_type: str,
        payload: Mapping[str, Any],
        committed_at: str,
    ) -> bool:
        """Append through an explicitly owned registry transaction."""
        database = Path(connection.execute("PRAGMA database_list").fetchone()[2])
        if database.resolve() != self.database:
            raise ValueError("Board event transaction belongs to another database")
        return append_board_event(
            connection,
            event_id=event_id,
            project_id=project_id,
            map_id=map_id,
            event_type=event_type,
            payload=payload,
            committed_at=committed_at,
            resource_key=event_id,
        )

    @contextmanager
    def atomic(self) -> Iterator[sqlite3.Connection]:
        """Open one immediate transaction for state plus event commit."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def read(self, *, cursor: int, limit: int = 200) -> dict[str, Any]:
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValueError("Board event cursor must be a non-negative integer")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
        ):
            raise ValueError("Board event limit must be between 1 and 500")
        with self._connect() as connection:
            connection.execute("BEGIN")
            metadata = connection.execute(
                """
                SELECT retained_after_cursor, latest_cursor
                FROM board_event_meta WHERE singleton = 1
                """
            ).fetchone()
            retained_after_cursor = int(metadata["retained_after_cursor"])
            latest = int(metadata["latest_cursor"])
            logical_floor = max(
                retained_after_cursor,
                self._logical_retention_floor(connection, latest=latest),
            )
            if cursor < logical_floor:
                connection.commit()
                return {
                    "status": "refresh_required",
                    "reason": "cursor_expired",
                    "cursor": cursor,
                    "retained_after_cursor": logical_floor,
                    "latest_cursor": latest,
                }
            rows = connection.execute(
                """
                SELECT cursor, event_id, project_id, map_id, event_type,
                       payload_hash, payload_json, committed_at
                FROM board_events
                WHERE cursor > ?
                ORDER BY cursor
                LIMIT ?
                """,
                (cursor, limit),
            ).fetchall()
            connection.commit()
        events = [self._event(row) for row in rows]
        next_cursor = events[-1]["cursor"] if events else cursor
        return {
            "status": "open",
            "events": events,
            "cursor": next_cursor,
            "latest_cursor": latest,
            "has_more": next_cursor < latest,
        }

    def latest_cursor(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT latest_cursor FROM board_event_meta WHERE singleton = 1"
            ).fetchone()
        return int(row["latest_cursor"])

    def _logical_retention_floor(
        self, connection: sqlite3.Connection, *, latest: int
    ) -> int:
        if latest == 0:
            return 0
        now = self._clock()
        if isinstance(now, datetime):
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            now_text = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        else:
            now_text = str(now)
        expired = connection.execute(
            """
            SELECT MAX(cursor) AS cursor
            FROM board_events
            WHERE julianday(committed_at) < (
                julianday(?) - (? / 86400.0)
            )
            """,
            (now_text, self.settings.retention_seconds),
        ).fetchone()["cursor"]
        return max(
            0,
            latest - self.settings.max_events,
            int(expired) if expired is not None else 0,
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "cursor": int(row["cursor"]),
            "event_id": row["event_id"],
            "project_id": row["project_id"],
            "map_id": row["map_id"],
            "type": row["event_type"],
            "payload_hash": row["payload_hash"],
            "payload": json.loads(row["payload_json"]),
            "committed_at": row["committed_at"],
        }
