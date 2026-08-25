"""Durable at-least-once execution records for external governance effects."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from typing import Any, Callable, Iterator, Mapping, Protocol

from .approvals import normalized_hash, normalized_json
from .events import append_board_event, content_event_id
from .storage import PluginStorage


logger = logging.getLogger(__name__)


class OutboxConflictError(ValueError):
    """Raised when one stable effect identity is reused for other content."""


class OutboxLeaseError(RuntimeError):
    """Raised when a worker no longer owns an unexpired durable lease."""


class EffectRetryableError(RuntimeError):
    """An external boundary failure that may converge on a later attempt."""

    retryable = True


class EffectTerminalError(RuntimeError):
    """An external boundary rejection that requires explicit repair."""

    retryable = False


@dataclass(frozen=True)
class OutboxIntent:
    """One immutable external-effect intent plus its mutable delivery state."""

    effect_id: str
    effect_type: str
    map_id: str
    payload_hash: str
    payload: dict[str, Any]
    state: str
    owner_id: str | None
    lease_expires_at: str | None
    attempt_count: int
    next_attempt_at: str | None
    created_at: str
    updated_at: str
    acknowledged_at: str | None
    acknowledgment: dict[str, Any] | None
    last_error_type: str | None
    last_error_message: str | None
    terminal_reason: str | None


@dataclass(frozen=True)
class EnqueuedIntent:
    intent: OutboxIntent
    created: bool


@dataclass(frozen=True)
class _PreparedIntent:
    effect_id: str
    effect_type: str
    map_id: str
    payload_hash: str
    payload_json: str
    created_at: str


@dataclass(frozen=True)
class OutboxClaim:
    effect_id: str
    owner_id: str
    attempt_number: int
    attempt_in_cycle: int
    lease_expires_at: str
    intent: OutboxIntent


@dataclass(frozen=True)
class OutboxAttempt:
    effect_id: str
    attempt_number: int
    owner_id: str
    started_at: str
    lease_expires_at: str
    finished_at: str | None
    outcome: str
    error_type: str | None
    error_message: str | None


@dataclass(frozen=True)
class EffectConfirmation:
    """Downstream readback proving one semantic effect is already present."""

    acknowledgment: dict[str, Any]
    reconciled_by_readback: bool

    def __init__(
        self,
        acknowledgment: Mapping[str, Any],
        *,
        reconciled_by_readback: bool = False,
    ) -> None:
        if not isinstance(acknowledgment, Mapping):
            raise ValueError("Effect acknowledgment must be a JSON object")
        object.__setattr__(
            self,
            "acknowledgment",
            json.loads(normalized_json(dict(acknowledgment))),
        )
        object.__setattr__(self, "reconciled_by_readback", reconciled_by_readback)


class EffectAdapter(Protocol):
    def readback(self, intent: OutboxIntent) -> EffectConfirmation | None: ...

    def apply(self, intent: OutboxIntent) -> None: ...


@dataclass(frozen=True)
class OutboxSettings:
    lease_seconds: int = 30
    poll_seconds: int = 1
    base_retry_seconds: int = 5
    max_retry_seconds: int = 300
    max_attempts: int = 5

    def __post_init__(self) -> None:
        for name in (
            "lease_seconds",
            "poll_seconds",
            "base_retry_seconds",
            "max_retry_seconds",
            "max_attempts",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"Outbox {name} must be a positive integer")
        if self.max_retry_seconds < self.base_retry_seconds:
            raise ValueError(
                "Outbox max_retry_seconds must be at least base_retry_seconds"
            )


@dataclass(frozen=True)
class DispatchOutcome:
    effect_id: str
    state: str
    attempt_number: int
    reconciled_by_readback: bool
    acknowledgment: dict[str, Any] | None = None
    next_attempt_at: str | None = None
    terminal_reason: str | None = None
    error_type: str | None = None
    error_message: str | None = None


class OutboxDispatcher:
    """Claim, reconcile, apply, and acknowledge durable effects."""

    def __init__(
        self,
        *,
        repository: "OutboxRepository",
        adapters: Mapping[str, EffectAdapter],
        clock: Callable[[], str | datetime],
        settings: OutboxSettings | None = None,
        completion: Callable[[OutboxIntent, EffectConfirmation], None] | None = None,
        interlock: Callable[[OutboxIntent], None] | None = None,
        failure: Callable[[OutboxIntent, Exception], None] | None = None,
        crash_injector: Callable[[str, OutboxIntent], None] | None = None,
    ) -> None:
        self._repository = repository
        self._adapters = dict(adapters)
        self._clock = clock
        self._settings = settings or OutboxSettings()
        self._completion = completion or (lambda _intent, _confirmation: None)
        self._interlock = interlock or (lambda _intent: None)
        self._failure = failure or (lambda _intent, _error: None)
        self._crash_injector = crash_injector

    def dispatch_next(
        self,
        *,
        owner_id: str,
        pending_grace_seconds: float = 0,
    ) -> DispatchOutcome | None:
        """Dispatch one due intent, reading downstream truth before every call."""
        now = self._now()
        if isinstance(pending_grace_seconds, bool) or not isinstance(
            pending_grace_seconds, (int, float)
        ):
            raise ValueError("Outbox pending_grace_seconds must be numeric")
        if pending_grace_seconds < 0:
            raise ValueError("Outbox pending_grace_seconds must not be negative")
        pending_before = None
        if pending_grace_seconds:
            pending_before = OutboxRepository._format_timestamp(
                OutboxRepository._timestamp(now, name="now")
                - timedelta(seconds=pending_grace_seconds)
            )
        claim = self._repository.claim_next(
            owner_id=owner_id,
            now=now,
            lease_seconds=self._settings.lease_seconds,
            effect_types=tuple(self._adapters),
            pending_before=pending_before,
        )
        return self._dispatch_claim(claim, owner_id=owner_id)

    def dispatch_effect(
        self,
        *,
        effect_id: str,
        owner_id: str,
        expedite_retry: bool = False,
    ) -> DispatchOutcome | None:
        """Synchronously dispatch one named due intent without claiming another."""
        now = self._now()
        if expedite_retry:
            self._repository.expedite_retry(effect_id=effect_id, now=now)
        claim = self._repository.claim(
            effect_id=effect_id,
            owner_id=owner_id,
            now=now,
            lease_seconds=self._settings.lease_seconds,
        )
        return self._dispatch_claim(claim, owner_id=owner_id)

    def _dispatch_claim(
        self,
        claim: OutboxClaim | None,
        *,
        owner_id: str,
    ) -> DispatchOutcome | None:
        if claim is None:
            return None
        intent = claim.intent
        self._inject("after_claim", intent)
        heartbeat = _LeaseHeartbeat(
            repository=self._repository,
            claim=claim,
            clock=self._now,
            lease_seconds=self._settings.lease_seconds,
        )
        heartbeat.start()
        try:
            try:
                self._interlock(intent)
                adapter = self._adapters[intent.effect_type]
            except KeyError as error:
                raise EffectTerminalError(
                    f"No external effect adapter is configured for {intent.effect_type}"
                ) from error
            confirmation = adapter.readback(intent)
            self._inject("after_initial_readback", intent)
            if (
                intent.effect_type == "publisher.execute"
                and confirmation is not None
                and not self._repository.external_call_started(intent.effect_id)
            ):
                raise EffectTerminalError(
                    "Pre-existing remote state cannot retroactively satisfy a new "
                    "publication grant"
                )
            reconciled_by_readback = confirmation is not None
            if confirmation is None:
                if (
                    intent.effect_type == "publisher.execute"
                    and self._repository.external_call_started(intent.effect_id)
                ):
                    raise EffectTerminalError(
                        "Publisher outcome is uncertain after a prior external call"
                    )
                self._inject("before_external_call", intent)
                adapter.apply(intent)
                self._inject("after_external_call", intent)
                confirmation = adapter.readback(intent)
                if confirmation is None:
                    if intent.effect_type == "publisher.execute":
                        raise EffectTerminalError(
                            "Publisher call returned without immutable remote evidence"
                        )
                    raise EffectRetryableError(
                        "External effect was not confirmed by downstream readback"
                    )
            self._inject("after_confirmation", intent)
            confirmation = EffectConfirmation(
                confirmation.acknowledgment,
                reconciled_by_readback=reconciled_by_readback,
            )
            self._completion(intent, confirmation)
            self._inject("after_completion", intent)
            self._inject("before_acknowledgment", intent)
            heartbeat.stop()
            heartbeat.raise_if_failed()
            succeeded = self._repository.acknowledge(
                effect_id=intent.effect_id,
                owner_id=owner_id,
                now=self._now(),
                acknowledgment=confirmation.acknowledgment,
            )
        except Exception as error:
            heartbeat.stop()
            return self._record_failure(claim, error)
        finally:
            heartbeat.stop()
        self._inject("after_acknowledgment", succeeded)
        return DispatchOutcome(
            effect_id=intent.effect_id,
            state=succeeded.state,
            attempt_number=claim.attempt_number,
            reconciled_by_readback=reconciled_by_readback,
            acknowledgment=succeeded.acknowledgment,
        )

    def _record_failure(
        self,
        claim: OutboxClaim,
        error: Exception,
    ) -> DispatchOutcome:
        failed_at = self._now()
        error_type = type(error).__name__
        error_message = str(error) or error_type
        self._failure(claim.intent, error)
        retryable = getattr(error, "retryable", True) is not False
        if retryable and claim.attempt_in_cycle < self._settings.max_attempts:
            delay = min(
                self._settings.base_retry_seconds * (2 ** (claim.attempt_in_cycle - 1)),
                self._settings.max_retry_seconds,
            )
            next_attempt_at = OutboxRepository._format_timestamp(
                OutboxRepository._timestamp(failed_at, name="failed_at")
                + timedelta(seconds=delay)
            )
            failed = self._repository.schedule_retry(
                effect_id=claim.effect_id,
                owner_id=claim.owner_id,
                now=failed_at,
                next_attempt_at=next_attempt_at,
                error_type=error_type,
                error_message=error_message,
            )
            return DispatchOutcome(
                effect_id=failed.effect_id,
                state=failed.state,
                attempt_number=claim.attempt_number,
                reconciled_by_readback=False,
                next_attempt_at=failed.next_attempt_at,
                error_type=error_type,
                error_message=error_message,
            )
        reason_prefix = "retry_exhausted" if retryable else "terminal_failure"
        failed = self._repository.mark_terminal(
            effect_id=claim.effect_id,
            owner_id=claim.owner_id,
            now=failed_at,
            error_type=error_type,
            error_message=error_message,
            terminal_reason=f"{reason_prefix}: {error_message}",
        )
        return DispatchOutcome(
            effect_id=failed.effect_id,
            state=failed.state,
            attempt_number=claim.attempt_number,
            reconciled_by_readback=False,
            terminal_reason=failed.terminal_reason,
            error_type=error_type,
            error_message=error_message,
        )

    def _inject(self, point: str, intent: OutboxIntent) -> None:
        if self._crash_injector is not None:
            self._crash_injector(point, intent)

    def _now(self) -> str:
        value = self._clock()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return OutboxRepository._format_timestamp(value.astimezone(timezone.utc))
        return OutboxRepository._format_timestamp(
            OutboxRepository._timestamp(value, name="clock")
        )


class OutboxRuntime:
    """Recurring dispatcher lifecycle backed exclusively by durable claims."""

    def __init__(
        self,
        *,
        dispatcher: OutboxDispatcher,
        owner_id: str,
        poll_seconds: float = 1.0,
    ) -> None:
        if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float)):
            raise ValueError("Outbox poll_seconds must be numeric")
        if poll_seconds <= 0:
            raise ValueError("Outbox poll_seconds must be positive")
        self._dispatcher = dispatcher
        self._owner_id = OutboxRepository._text(owner_id, name="owner_id")
        self._poll_seconds = float(poll_seconds)
        self._stop = Event()
        self._thread = Thread(
            target=self._run,
            name="map-governance-outbox-dispatcher",
            daemon=True,
        )

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                while not self._stop.is_set():
                    outcome = self._dispatcher.dispatch_next(
                        owner_id=self._owner_id,
                        pending_grace_seconds=self._poll_seconds,
                    )
                    if outcome is None:
                        break
            except Exception:
                logger.exception("Map Governance Outbox recovery tick failed")
            self._stop.wait(self._poll_seconds)


class _LeaseHeartbeat:
    """Keep one durable owner fenced throughout a potentially slow call."""

    def __init__(
        self,
        *,
        repository: "OutboxRepository",
        claim: OutboxClaim,
        clock: Callable[[], str],
        lease_seconds: int,
    ) -> None:
        self._repository = repository
        self._claim = claim
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._interval = max(0.05, lease_seconds / 3)
        self._stop = Event()
        self._thread = Thread(
            target=self._run,
            name=f"map-governance-outbox-lease:{claim.effect_id}",
            daemon=True,
        )
        self._error: Exception | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self._interval * 2))

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise self._error

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._repository.renew(
                    effect_id=self._claim.effect_id,
                    owner_id=self._claim.owner_id,
                    now=self._clock(),
                    lease_seconds=self._lease_seconds,
                )
            except Exception as error:  # fenced owner must stop applying
                self._error = error
                self._stop.set()
                return


class OutboxRepository:
    """Persist Outbox intents in the plugin-owned SQLite registry."""

    def __init__(self, storage_root: Path, *, shared_gid: int | None = None) -> None:
        storage = PluginStorage(storage_root, shared_gid=shared_gid)
        storage.check_readiness()
        self.database = storage.database

    def enqueue(
        self,
        *,
        effect_id: str,
        effect_type: str,
        map_id: str,
        payload: Mapping[str, Any],
        created_at: str,
    ) -> EnqueuedIntent:
        """Commit one stable intent or return the identical existing record."""
        prepared = self._prepare_intent(
            effect_id=effect_id,
            effect_type=effect_type,
            map_id=map_id,
            payload=payload,
            created_at=created_at,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = self._enqueue_prepared(connection, prepared)
            connection.commit()
        return result

    def enqueue_occurrence(
        self,
        *,
        effect_id_base: str,
        effect_type: str,
        map_id: str,
        payload: Mapping[str, Any],
        created_at: str,
    ) -> EnqueuedIntent:
        """Reuse unfinished work, or append a new occurrence after success."""
        prepared = self._prepare_intent(
            effect_id=effect_id_base,
            effect_type=effect_type,
            map_id=map_id,
            payload=payload,
            created_at=created_at,
        )
        prefix = f"{prepared.effect_id}:"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM outbox_intents
                WHERE substr(effect_id, 1, ?) = ?
                ORDER BY created_at, effect_id
                """,
                (len(prefix), prefix),
            ).fetchall()
            prior_intents = [self._intent(row) for row in rows]
            if any(
                intent.effect_type != prepared.effect_type
                or intent.map_id != prepared.map_id
                or intent.payload_hash != prepared.payload_hash
                or normalized_json(intent.payload) != prepared.payload_json
                for intent in prior_intents
            ):
                raise OutboxConflictError(
                    "Outbox occurrence identity has a different payload: "
                    f"{prepared.effect_id}"
                )
            unfinished = next(
                (row for row in reversed(rows) if row["state"] != "succeeded"),
                None,
            )
            if unfinished is not None:
                result = self._enqueue_prepared(
                    connection,
                    _PreparedIntent(
                        effect_id=str(unfinished["effect_id"]),
                        effect_type=prepared.effect_type,
                        map_id=prepared.map_id,
                        payload_hash=prepared.payload_hash,
                        payload_json=prepared.payload_json,
                        created_at=prepared.created_at,
                    ),
                )
                connection.commit()
                return result
            sequence = 1
            if rows:
                try:
                    sequence = (
                        max(int(str(row["effect_id"])[len(prefix) :]) for row in rows)
                        + 1
                    )
                except ValueError as error:
                    raise OutboxConflictError(
                        f"Outbox occurrence identity is malformed: {prefix}"
                    ) from error
            result = self._enqueue_prepared(
                connection,
                _PreparedIntent(
                    effect_id=f"{prefix}{sequence}",
                    effect_type=prepared.effect_type,
                    map_id=prepared.map_id,
                    payload_hash=prepared.payload_hash,
                    payload_json=prepared.payload_json,
                    created_at=prepared.created_at,
                ),
            )
            connection.commit()
        return result

    def enqueue_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        effect_id: str,
        effect_type: str,
        map_id: str,
        payload: Mapping[str, Any],
        created_at: str,
    ) -> EnqueuedIntent:
        """Insert an intent through a caller-owned plugin DB transaction."""
        if Path(connection.execute("PRAGMA database_list").fetchone()[2]).resolve() != (
            Path(self.database).resolve()
        ):
            raise ValueError("Outbox transaction belongs to another database")
        return self._enqueue_prepared(
            connection,
            self._prepare_intent(
                effect_id=effect_id,
                effect_type=effect_type,
                map_id=map_id,
                payload=payload,
                created_at=created_at,
            ),
        )

    def _prepare_intent(
        self,
        *,
        effect_id: str,
        effect_type: str,
        map_id: str,
        payload: Mapping[str, Any],
        created_at: str,
    ) -> _PreparedIntent:
        identity = self._text(effect_id, name="effect_id", maximum=256)
        kind = self._text(effect_type, name="effect_type", maximum=128)
        bound_map = self._text(map_id, name="map_id", maximum=256)
        timestamp = self._format_timestamp(
            self._timestamp(created_at, name="created_at")
        )
        if not isinstance(payload, Mapping):
            raise ValueError("Outbox payload must be a JSON object")
        payload_json = normalized_json(dict(payload))
        return _PreparedIntent(
            effect_id=identity,
            effect_type=kind,
            map_id=bound_map,
            payload_hash=normalized_hash(
                {
                    "effect_type": kind,
                    "map_id": bound_map,
                    "payload": json.loads(payload_json),
                }
            ),
            payload_json=payload_json,
            created_at=timestamp,
        )

    def _enqueue_prepared(
        self,
        connection: sqlite3.Connection,
        prepared: _PreparedIntent,
    ) -> EnqueuedIntent:
        self._text(prepared.effect_id, name="effect_id", maximum=256)
        existing = connection.execute(
            "SELECT * FROM outbox_intents WHERE effect_id = ?",
            (prepared.effect_id,),
        ).fetchone()
        if existing is not None:
            intent = self._intent(existing)
            if (
                intent.effect_type != prepared.effect_type
                or intent.map_id != prepared.map_id
                or intent.payload_hash != prepared.payload_hash
                or normalized_json(intent.payload) != prepared.payload_json
            ):
                raise OutboxConflictError(
                    "Outbox effect identity has a different payload: "
                    f"{prepared.effect_id}"
                )
            return EnqueuedIntent(intent=intent, created=False)
        connection.execute(
            """
            INSERT INTO outbox_intents(
                effect_id, effect_type, map_id, payload_hash, payload_json,
                state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                prepared.effect_id,
                prepared.effect_type,
                prepared.map_id,
                prepared.payload_hash,
                prepared.payload_json,
                prepared.created_at,
                prepared.created_at,
            ),
        )
        row = connection.execute(
            "SELECT * FROM outbox_intents WHERE effect_id = ?",
            (prepared.effect_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - SQLite contract guard
            raise RuntimeError("Outbox intent disappeared after commit")
        self._emit_update(connection, row)
        return EnqueuedIntent(intent=self._intent(row), created=True)

    def intent(self, effect_id: str) -> OutboxIntent | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM outbox_intents WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
        return self._intent(row) if row is not None else None

    def external_call_started(self, effect_id: str) -> bool:
        """Return whether a privileged publisher call crossed its mutation point."""
        return self.external_call_started_at(effect_id) is not None

    def external_call_started_at(self, effect_id: str) -> str | None:
        """Return the durable authorization-window attempt timestamp, if any."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT started_at FROM outbox_external_calls WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
        return str(row["started_at"]) if row is not None else None

    def mark_external_call_started(
        self,
        *,
        effect_id: str,
        owner_id: str,
        now: str,
    ) -> None:
        """Durably fence a publisher intent before its first remote mutation."""
        started_at = self._format_timestamp(self._timestamp(now, name="now"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._owned_lease(
                connection,
                effect_id=effect_id,
                owner_id=owner_id,
                now=started_at,
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO outbox_external_calls(effect_id, started_at)
                VALUES (?, ?)
                """,
                (effect_id, started_at),
            )
            connection.commit()

    def effect_intents(
        self,
        *,
        map_id: str,
        effect_type: str,
    ) -> tuple[OutboxIntent, ...]:
        """Return all durable effects of one kind for a Map."""
        bound_map = self._text(map_id, name="map_id", maximum=256)
        kind = self._text(effect_type, name="effect_type", maximum=128)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM outbox_intents
                WHERE map_id = ? AND effect_type = ?
                ORDER BY created_at, effect_id
                """,
                (bound_map, kind),
            ).fetchall()
        return tuple(self._intent(row) for row in rows)

    def unfinished_intents(
        self,
        *,
        map_id: str,
        effect_type: str,
    ) -> tuple[OutboxIntent, ...]:
        """Return durable work that has not reached acknowledged success."""
        return tuple(
            intent
            for intent in self.effect_intents(map_id=map_id, effect_type=effect_type)
            if intent.state != "succeeded"
        )

    def all_unfinished_intents(self) -> tuple[OutboxIntent, ...]:
        """Return all durable work still visible to restart reconciliation."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM outbox_intents
                WHERE state <> 'succeeded'
                ORDER BY created_at, effect_id
                """
            ).fetchall()
        return tuple(self._intent(row) for row in rows)

    def expedite_retry(self, *, effect_id: str, now: str) -> None:
        """Make a scheduled retry due after an explicit synchronous user retry."""
        timestamp = self._format_timestamp(self._timestamp(now, name="now"))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox_intents
                SET next_attempt_at = ?, updated_at = ?
                WHERE effect_id = ? AND state = 'retry_scheduled'
                """,
                (timestamp, timestamp, effect_id),
            )
            if cursor.rowcount:
                row = connection.execute(
                    "SELECT * FROM outbox_intents WHERE effect_id = ?",
                    (effect_id,),
                ).fetchone()
                if row is not None:
                    self._emit_update(connection, row)
            connection.commit()

    def claim_next(
        self,
        *,
        owner_id: str,
        now: str,
        lease_seconds: int,
        effect_types: tuple[str, ...] | None = None,
        pending_before: str | None = None,
    ) -> OutboxClaim | None:
        """Atomically claim one due intent, reclaiming only expired leases."""
        return self._claim(
            effect_id=None,
            owner_id=owner_id,
            now=now,
            lease_seconds=lease_seconds,
            effect_types=effect_types,
            pending_before=pending_before,
        )

    def claim(
        self,
        *,
        effect_id: str,
        owner_id: str,
        now: str,
        lease_seconds: int,
    ) -> OutboxClaim | None:
        """Atomically claim one named intent only when it is due."""
        return self._claim(
            effect_id=self._text(effect_id, name="effect_id", maximum=256),
            owner_id=owner_id,
            now=now,
            lease_seconds=lease_seconds,
            effect_types=None,
            pending_before=None,
        )

    def _claim(
        self,
        *,
        effect_id: str | None,
        owner_id: str,
        now: str,
        lease_seconds: int,
        effect_types: tuple[str, ...] | None,
        pending_before: str | None,
    ) -> OutboxClaim | None:
        owner = self._text(owner_id, name="owner_id", maximum=256)
        claimed_at = self._timestamp(now, name="now")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            raise ValueError("Outbox lease_seconds must be an integer")
        if lease_seconds < 1:
            raise ValueError("Outbox lease_seconds must be positive")
        claimed_at_text = self._format_timestamp(claimed_at)
        lease_expires_at = self._format_timestamp(
            claimed_at + timedelta(seconds=lease_seconds)
        )
        normalized_types = (
            tuple(
                sorted(
                    {
                        self._text(value, name="effect_type", maximum=128)
                        for value in effect_types
                    }
                )
            )
            if effect_types is not None
            else None
        )
        if normalized_types == ():
            return None
        pending_filter = ""
        normalized_pending_before = None
        if pending_before is not None:
            normalized_pending_before = self._format_timestamp(
                self._timestamp(pending_before, name="pending_before")
            )
            pending_filter = "AND created_at <= ?"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            effect_filter = "" if effect_id is None else "effect_id = ? AND"
            type_filter = ""
            parameters: list[str] = []
            if normalized_types is not None:
                placeholders = ", ".join("?" for _ in normalized_types)
                type_filter = f"effect_type IN ({placeholders}) AND"
                parameters.extend(normalized_types)
            if effect_id is not None:
                parameters.append(effect_id)
            if normalized_pending_before is not None:
                parameters.append(normalized_pending_before)
            parameters.extend((claimed_at_text, claimed_at_text))
            row = connection.execute(
                f"""
                SELECT * FROM outbox_intents
                WHERE {type_filter} {effect_filter}
                    (
                    (state = 'pending' {pending_filter})
                    OR (
                        state = 'retry_scheduled'
                        AND next_attempt_at IS NOT NULL
                        AND next_attempt_at <= ?
                    )
                    OR (
                        state = 'leased'
                        AND lease_expires_at IS NOT NULL
                        AND lease_expires_at <= ?
                    )
                    )
                ORDER BY created_at, effect_id
                LIMIT 1
                """,
                tuple(parameters),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            claimed_effect_id = str(row["effect_id"])
            if row["state"] == "leased":
                connection.execute(
                    """
                    UPDATE outbox_attempts
                    SET outcome = 'lease_expired', finished_at = ?
                    WHERE effect_id = ? AND attempt_number = ?
                      AND outcome = 'claimed'
                    """,
                    (
                        claimed_at_text,
                        claimed_effect_id,
                        int(row["attempt_count"]),
                    ),
                )
            attempt_number = int(row["attempt_count"]) + 1
            repaired_attempts = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(prior_attempt_count), 0) AS count
                    FROM outbox_repairs
                    WHERE effect_id = ?
                    """,
                    (claimed_effect_id,),
                ).fetchone()["count"]
            )
            connection.execute(
                """
                UPDATE outbox_intents
                SET state = 'leased', owner_id = ?, lease_expires_at = ?,
                    attempt_count = ?, next_attempt_at = NULL, updated_at = ?
                WHERE effect_id = ?
                """,
                (
                    owner,
                    lease_expires_at,
                    attempt_number,
                    claimed_at_text,
                    claimed_effect_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO outbox_attempts(
                    effect_id, attempt_number, owner_id, started_at,
                    lease_expires_at, outcome
                ) VALUES (?, ?, ?, ?, ?, 'claimed')
                """,
                (
                    claimed_effect_id,
                    attempt_number,
                    owner,
                    claimed_at_text,
                    lease_expires_at,
                ),
            )
            claimed_row = connection.execute(
                "SELECT * FROM outbox_intents WHERE effect_id = ?",
                (claimed_effect_id,),
            ).fetchone()
            if claimed_row is not None:
                self._emit_update(connection, claimed_row)
            connection.commit()
        if claimed_row is None:  # pragma: no cover - SQLite contract guard
            raise RuntimeError("Claimed Outbox intent disappeared")
        intent = self._intent(claimed_row)
        return OutboxClaim(
            effect_id=claimed_effect_id,
            owner_id=owner,
            attempt_number=attempt_number,
            attempt_in_cycle=attempt_number - repaired_attempts,
            lease_expires_at=lease_expires_at,
            intent=intent,
        )

    def acknowledge(
        self,
        *,
        effect_id: str,
        owner_id: str,
        now: str,
        acknowledgment: Mapping[str, Any],
    ) -> OutboxIntent:
        """Fence the owner and mark one confirmed semantic effect succeeded."""
        if not isinstance(acknowledgment, Mapping):
            raise ValueError("Outbox acknowledgment must be a JSON object")
        acknowledgment_json = normalized_json(dict(acknowledgment))
        acknowledged_at = self._format_timestamp(self._timestamp(now, name="now"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._owned_lease(
                connection,
                effect_id=effect_id,
                owner_id=owner_id,
                now=acknowledged_at,
            )
            connection.execute(
                """
                UPDATE outbox_intents
                SET state = 'succeeded', owner_id = NULL,
                    lease_expires_at = NULL, acknowledged_at = ?,
                    acknowledgment_json = ?, last_error_type = NULL,
                    last_error_message = NULL, terminal_reason = NULL,
                    updated_at = ?
                WHERE effect_id = ?
                """,
                (acknowledged_at, acknowledgment_json, acknowledged_at, effect_id),
            )
            connection.execute(
                """
                UPDATE outbox_attempts
                SET outcome = 'succeeded', finished_at = ?
                WHERE effect_id = ? AND attempt_number = ?
                  AND owner_id = ? AND outcome = 'claimed'
                """,
                (
                    acknowledged_at,
                    effect_id,
                    int(row["attempt_count"]),
                    owner_id,
                ),
            )
            succeeded = connection.execute(
                "SELECT * FROM outbox_intents WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if succeeded is not None:
                self._emit_update(connection, succeeded)
            connection.commit()
        if succeeded is None:  # pragma: no cover
            raise RuntimeError("Acknowledged Outbox intent disappeared")
        return self._intent(succeeded)

    def renew(
        self,
        *,
        effect_id: str,
        owner_id: str,
        now: str,
        lease_seconds: int,
    ) -> OutboxClaim:
        """Extend only the current owner's still-live durable lease."""
        renewed_at = self._timestamp(now, name="now")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            raise ValueError("Outbox lease_seconds must be an integer")
        if lease_seconds < 1:
            raise ValueError("Outbox lease_seconds must be positive")
        renewed_at_text = self._format_timestamp(renewed_at)
        lease_expires_at = self._format_timestamp(
            renewed_at + timedelta(seconds=lease_seconds)
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._owned_lease(
                connection,
                effect_id=effect_id,
                owner_id=owner_id,
                now=renewed_at_text,
            )
            attempt_number = int(row["attempt_count"])
            repaired_attempts = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(prior_attempt_count), 0) AS count
                    FROM outbox_repairs
                    WHERE effect_id = ?
                    """,
                    (effect_id,),
                ).fetchone()["count"]
            )
            connection.execute(
                """
                UPDATE outbox_intents
                SET lease_expires_at = ?, updated_at = ?
                WHERE effect_id = ?
                """,
                (lease_expires_at, renewed_at_text, effect_id),
            )
            connection.execute(
                """
                UPDATE outbox_attempts
                SET lease_expires_at = ?
                WHERE effect_id = ? AND attempt_number = ?
                  AND owner_id = ? AND outcome = 'claimed'
                """,
                (lease_expires_at, effect_id, attempt_number, owner_id),
            )
            renewed = connection.execute(
                "SELECT * FROM outbox_intents WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if renewed is not None:
                self._emit_update(connection, renewed)
            connection.commit()
        if renewed is None:  # pragma: no cover
            raise RuntimeError("Renewed Outbox intent disappeared")
        intent = self._intent(renewed)
        return OutboxClaim(
            effect_id=effect_id,
            owner_id=owner_id,
            attempt_number=attempt_number,
            attempt_in_cycle=attempt_number - repaired_attempts,
            lease_expires_at=lease_expires_at,
            intent=intent,
        )

    def release(
        self,
        *,
        effect_id: str,
        owner_id: str,
        now: str,
    ) -> OutboxIntent:
        """Return a live lease to pending without erasing its attempt."""
        released_at = self._format_timestamp(self._timestamp(now, name="now"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._owned_lease(
                connection,
                effect_id=effect_id,
                owner_id=owner_id,
                now=released_at,
            )
            connection.execute(
                """
                UPDATE outbox_intents
                SET state = 'pending', owner_id = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE effect_id = ?
                """,
                (released_at, effect_id),
            )
            connection.execute(
                """
                UPDATE outbox_attempts
                SET outcome = 'released', finished_at = ?
                WHERE effect_id = ? AND attempt_number = ?
                  AND owner_id = ? AND outcome = 'claimed'
                """,
                (
                    released_at,
                    effect_id,
                    int(row["attempt_count"]),
                    owner_id,
                ),
            )
            released = connection.execute(
                "SELECT * FROM outbox_intents WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if released is not None:
                self._emit_update(connection, released)
            connection.commit()
        if released is None:  # pragma: no cover
            raise RuntimeError("Released Outbox intent disappeared")
        return self._intent(released)

    def schedule_retry(
        self,
        *,
        effect_id: str,
        owner_id: str,
        now: str,
        next_attempt_at: str,
        error_type: str,
        error_message: str,
    ) -> OutboxIntent:
        """Record one retryable attempt and its durable due time."""
        failed_at = self._format_timestamp(self._timestamp(now, name="now"))
        retry_at = self._format_timestamp(
            self._timestamp(next_attempt_at, name="next_attempt_at")
        )
        if retry_at <= failed_at:
            raise ValueError("Outbox next_attempt_at must be after now")
        return self._finish_failed_attempt(
            effect_id=effect_id,
            owner_id=owner_id,
            now=failed_at,
            state="retry_scheduled",
            attempt_outcome="retry_scheduled",
            error_type=error_type,
            error_message=error_message,
            next_attempt_at=retry_at,
            terminal_reason=None,
        )

    def mark_terminal(
        self,
        *,
        effect_id: str,
        owner_id: str,
        now: str,
        error_type: str,
        error_message: str,
        terminal_reason: str,
    ) -> OutboxIntent:
        """Stop automatic delivery while retaining an operator-visible reason."""
        failed_at = self._format_timestamp(self._timestamp(now, name="now"))
        return self._finish_failed_attempt(
            effect_id=effect_id,
            owner_id=owner_id,
            now=failed_at,
            state="terminal",
            attempt_outcome="terminal",
            error_type=error_type,
            error_message=error_message,
            next_attempt_at=None,
            terminal_reason=self._text(terminal_reason, name="terminal_reason"),
        )

    def _finish_failed_attempt(
        self,
        *,
        effect_id: str,
        owner_id: str,
        now: str,
        state: str,
        attempt_outcome: str,
        error_type: str,
        error_message: str,
        next_attempt_at: str | None,
        terminal_reason: str | None,
    ) -> OutboxIntent:
        normalized_error_type = self._text(error_type, name="error_type", maximum=256)
        normalized_error_message = self._text(error_message, name="error_message")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._owned_lease(
                connection,
                effect_id=effect_id,
                owner_id=owner_id,
                now=now,
            )
            connection.execute(
                """
                UPDATE outbox_intents
                SET state = ?, owner_id = NULL, lease_expires_at = NULL,
                    next_attempt_at = ?, last_error_type = ?,
                    last_error_message = ?, terminal_reason = ?, updated_at = ?
                WHERE effect_id = ?
                """,
                (
                    state,
                    next_attempt_at,
                    normalized_error_type,
                    normalized_error_message,
                    terminal_reason,
                    now,
                    effect_id,
                ),
            )
            connection.execute(
                """
                UPDATE outbox_attempts
                SET outcome = ?, finished_at = ?, error_type = ?, error_message = ?
                WHERE effect_id = ? AND attempt_number = ?
                  AND owner_id = ? AND outcome = 'claimed'
                """,
                (
                    attempt_outcome,
                    now,
                    normalized_error_type,
                    normalized_error_message,
                    effect_id,
                    int(row["attempt_count"]),
                    owner_id,
                ),
            )
            failed = connection.execute(
                "SELECT * FROM outbox_intents WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if failed is not None:
                self._emit_update(connection, failed)
            connection.commit()
        if failed is None:  # pragma: no cover
            raise RuntimeError("Failed Outbox intent disappeared")
        return self._intent(failed)

    def attempt_history(self, effect_id: str) -> list[OutboxAttempt]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM outbox_attempts
                WHERE effect_id = ?
                ORDER BY attempt_number
                """,
                (effect_id,),
            ).fetchall()
        return [
            OutboxAttempt(
                effect_id=str(row["effect_id"]),
                attempt_number=int(row["attempt_number"]),
                owner_id=str(row["owner_id"]),
                started_at=str(row["started_at"]),
                lease_expires_at=str(row["lease_expires_at"]),
                finished_at=row["finished_at"],
                outcome=str(row["outcome"]),
                error_type=row["error_type"],
                error_message=row["error_message"],
            )
            for row in rows
        ]

    def repair(
        self,
        *,
        effect_id: str,
        repair_id: str,
        note: str,
        requested_at: str,
    ) -> EnqueuedIntent:
        """Audit an explicit operator repair and reset the same semantic effect."""
        identity = self._text(repair_id, name="repair_id", maximum=256)
        repair_note = self._text(note, name="repair_note")
        repair_time = self._format_timestamp(
            self._timestamp(requested_at, name="requested_at")
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_repair = connection.execute(
                "SELECT * FROM outbox_repairs WHERE repair_id = ?",
                (identity,),
            ).fetchone()
            if existing_repair is not None:
                if (
                    existing_repair["effect_id"] != effect_id
                    or existing_repair["note"] != repair_note
                ):
                    raise OutboxConflictError(
                        f"Outbox repair identity has different content: {identity}"
                    )
                row = connection.execute(
                    "SELECT * FROM outbox_intents WHERE effect_id = ?",
                    (effect_id,),
                ).fetchone()
                connection.commit()
                if row is None:  # pragma: no cover - foreign key contract
                    raise RuntimeError("Repaired Outbox intent disappeared")
                return EnqueuedIntent(intent=self._intent(row), created=False)

            row = connection.execute(
                "SELECT * FROM outbox_intents WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Outbox effect does not exist: {effect_id}")
            if row["state"] != "terminal" or not row["terminal_reason"]:
                raise ValueError("Only a terminal Outbox effect can be repaired")
            connection.execute(
                """
                INSERT INTO outbox_repairs(
                    repair_id, effect_id, note, requested_at,
                    prior_terminal_reason, prior_attempt_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    identity,
                    effect_id,
                    repair_note,
                    repair_time,
                    row["terminal_reason"],
                    int(row["attempt_count"]),
                ),
            )
            connection.execute(
                """
                UPDATE outbox_intents
                SET state = 'pending', owner_id = NULL, lease_expires_at = NULL,
                    next_attempt_at = NULL, last_error_type = NULL,
                    last_error_message = NULL, terminal_reason = NULL,
                    updated_at = ?
                WHERE effect_id = ? AND state = 'terminal'
                """,
                (repair_time, effect_id),
            )
            repaired = connection.execute(
                "SELECT * FROM outbox_intents WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if repaired is not None:
                self._emit_update(connection, repaired)
            connection.commit()
        if repaired is None:  # pragma: no cover
            raise RuntimeError("Repaired Outbox intent disappeared")
        return EnqueuedIntent(intent=self._intent(repaired), created=True)

    def acknowledge_terminal_readback(
        self,
        *,
        effect_id: str,
        resolution_id: str,
        note: str,
        acknowledgment: Mapping[str, Any],
        resolved_at: str,
        successor_effect_id: str,
        successor_effect_type: str,
        successor_map_id: str,
        successor_payload: Mapping[str, Any],
    ) -> OutboxIntent:
        """Atomically record the evidence successor and resolve a terminal effect."""
        identity = self._text(resolution_id, name="resolution_id", maximum=256)
        resolution_note = self._text(note, name="resolution_note")
        resolution_time = self._format_timestamp(
            self._timestamp(resolved_at, name="resolved_at")
        )
        if not isinstance(acknowledgment, Mapping):
            raise ValueError("Outbox acknowledgment must be a JSON object")
        acknowledgment_json = normalized_json(dict(acknowledgment))
        successor = self._prepare_intent(
            effect_id=successor_effect_id,
            effect_type=successor_effect_type,
            map_id=successor_map_id,
            payload=successor_payload,
            created_at=resolution_time,
        )
        resolved: sqlite3.Row | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_resolution = connection.execute(
                "SELECT * FROM outbox_repairs WHERE repair_id = ?",
                (identity,),
            ).fetchone()
            row = connection.execute(
                "SELECT * FROM outbox_intents WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Outbox effect does not exist: {effect_id}")
            if existing_resolution is not None:
                if (
                    existing_resolution["effect_id"] != effect_id
                    or existing_resolution["note"] != resolution_note
                    or row["state"] != "succeeded"
                    or row["acknowledgment_json"] != acknowledgment_json
                ):
                    raise OutboxConflictError(
                        "Outbox evidence resolution identity has different content: "
                        f"{identity}"
                    )
                self._enqueue_prepared(connection, successor)
                connection.commit()
                return self._intent(row)
            if row["state"] != "terminal" or not row["terminal_reason"]:
                raise ValueError(
                    "Only a terminal Outbox effect can be resolved by readback"
                )
            connection.execute(
                """
                INSERT INTO outbox_repairs(
                    repair_id, effect_id, note, requested_at,
                    prior_terminal_reason, prior_attempt_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    identity,
                    effect_id,
                    resolution_note,
                    resolution_time,
                    row["terminal_reason"],
                    int(row["attempt_count"]),
                ),
            )
            self._enqueue_prepared(connection, successor)
            connection.execute(
                """
                UPDATE outbox_intents
                SET state = 'succeeded', owner_id = NULL,
                    lease_expires_at = NULL, next_attempt_at = NULL,
                    acknowledged_at = ?, acknowledgment_json = ?,
                    last_error_type = NULL, last_error_message = NULL,
                    terminal_reason = NULL, updated_at = ?
                WHERE effect_id = ? AND state = 'terminal'
                """,
                (
                    resolution_time,
                    acknowledgment_json,
                    resolution_time,
                    effect_id,
                ),
            )
            resolved = connection.execute(
                "SELECT * FROM outbox_intents WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if resolved is not None:
                self._emit_update(connection, resolved)
            connection.commit()
        if resolved is None:  # pragma: no cover
            raise RuntimeError("Resolved Outbox intent disappeared")
        return self._intent(resolved)

    def operator_status(self, effect_id: str) -> dict[str, Any]:
        """Return an operator-safe delivery record with explicit repair action."""
        intent = self.intent(effect_id)
        if intent is None:
            raise ValueError(f"Outbox effect does not exist: {effect_id}")
        with self._connect() as connection:
            repair_rows = connection.execute(
                """
                SELECT repair_id, note, requested_at, prior_terminal_reason
                FROM outbox_repairs
                WHERE effect_id = ?
                ORDER BY requested_at, repair_id
                """,
                (effect_id,),
            ).fetchall()
        return {
            "effect_id": intent.effect_id,
            "effect_type": intent.effect_type,
            "map_id": intent.map_id,
            "payload_hash": intent.payload_hash,
            "state": intent.state,
            "owner_id": intent.owner_id,
            "lease_expires_at": intent.lease_expires_at,
            "attempt_count": intent.attempt_count,
            "next_attempt_at": intent.next_attempt_at,
            "created_at": intent.created_at,
            "updated_at": intent.updated_at,
            "acknowledged_at": intent.acknowledged_at,
            "terminal_outcome": (
                {
                    "reason": intent.terminal_reason,
                    "error_type": intent.last_error_type,
                    "message": intent.last_error_message,
                }
                if intent.state == "terminal"
                else None
            ),
            "repair_action": (
                {
                    "action": "repair_outbox",
                    "effect_id": intent.effect_id,
                    "requires": ["repair_id", "note"],
                }
                if intent.state == "terminal"
                else None
            ),
            "attempts": [
                {
                    "attempt_number": attempt.attempt_number,
                    "owner_id": attempt.owner_id,
                    "started_at": attempt.started_at,
                    "lease_expires_at": attempt.lease_expires_at,
                    "finished_at": attempt.finished_at,
                    "outcome": attempt.outcome,
                    "error_type": attempt.error_type,
                    "error_message": attempt.error_message,
                }
                for attempt in self.attempt_history(effect_id)
            ],
            "repairs": [dict(row) for row in repair_rows],
        }

    def map_summary(self, map_id: str) -> dict[str, Any]:
        """Summarize delivery health for one executive Map card."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT state, COUNT(*) AS count
                FROM outbox_intents
                WHERE map_id = ?
                GROUP BY state
                """,
                (map_id,),
            ).fetchall()
            latest_terminal = connection.execute(
                """
                SELECT effect_id
                FROM outbox_intents
                WHERE map_id = ? AND state = 'terminal'
                ORDER BY updated_at DESC, effect_id DESC
                LIMIT 1
                """,
                (map_id,),
            ).fetchone()
        counts = {str(row["state"]): int(row["count"]) for row in rows}
        terminal = (
            self.operator_status(str(latest_terminal["effect_id"]))
            if latest_terminal is not None
            else None
        )
        state = "healthy"
        if terminal is not None:
            state = "needs_repair"
        elif counts.get("retry_scheduled", 0):
            state = "retrying"
        elif counts.get("pending", 0) or counts.get("leased", 0):
            state = "in_progress"
        return {
            "state": state,
            "pending_count": counts.get("pending", 0),
            "retry_scheduled_count": counts.get("retry_scheduled", 0),
            "leased_count": counts.get("leased", 0),
            "succeeded_count": counts.get("succeeded", 0),
            "terminal_count": counts.get("terminal", 0),
            "latest_terminal": terminal,
        }

    @staticmethod
    def _owned_lease(
        connection: sqlite3.Connection,
        *,
        effect_id: str,
        owner_id: str,
        now: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM outbox_intents WHERE effect_id = ?",
            (effect_id,),
        ).fetchone()
        if (
            row is None
            or row["state"] != "leased"
            or row["owner_id"] != owner_id
            or row["lease_expires_at"] is None
            or row["lease_expires_at"] <= now
        ):
            raise OutboxLeaseError(
                f"Outbox owner {owner_id!r} does not own an unexpired lease for "
                f"{effect_id!r}"
            )
        return row

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _emit_update(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        """Append one absolute Outbox resource version in the same commit."""
        map_id = str(row["map_id"])
        binding = connection.execute(
            "SELECT project_id FROM map_bindings WHERE map_id = ?", (map_id,)
        ).fetchone()
        project_id = str(binding["project_id"]) if binding is not None else "unbound"
        effect = {
            "effect_id": str(row["effect_id"]),
            "effect_type": str(row["effect_type"]),
            "map_id": map_id,
            "state": str(row["state"]),
            "attempt_count": int(row["attempt_count"]),
            "next_attempt_at": row["next_attempt_at"],
            "acknowledged_at": row["acknowledged_at"],
            "last_error_type": row["last_error_type"],
            "last_error_message": row["last_error_message"],
            "terminal_reason": row["terminal_reason"],
            "updated_at": str(row["updated_at"]),
        }
        counts = {
            str(item["state"]): int(item["count"])
            for item in connection.execute(
                """
                SELECT state, COUNT(*) AS count
                FROM outbox_intents WHERE map_id = ? GROUP BY state
                """,
                (map_id,),
            ).fetchall()
        }
        terminal = connection.execute(
            """
            SELECT effect_id, effect_type, terminal_reason, updated_at
            FROM outbox_intents
            WHERE map_id = ? AND state = 'terminal'
            ORDER BY updated_at DESC, effect_id DESC LIMIT 1
            """,
            (map_id,),
        ).fetchone()
        summary_state = (
            "needs_repair"
            if counts.get("terminal", 0)
            else "retrying"
            if counts.get("retry_scheduled", 0)
            else "in_progress"
            if counts.get("pending", 0) or counts.get("leased", 0)
            else "healthy"
        )
        summary = {
            "state": summary_state,
            "pending_count": counts.get("pending", 0),
            "retry_scheduled_count": counts.get("retry_scheduled", 0),
            "leased_count": counts.get("leased", 0),
            "succeeded_count": counts.get("succeeded", 0),
            "terminal_count": counts.get("terminal", 0),
            "latest_terminal": (
                {
                    "effect_id": str(terminal["effect_id"]),
                    "effect_type": str(terminal["effect_type"]),
                    "terminal_outcome": {
                        "message": str(terminal["terminal_reason"]),
                    },
                    "updated_at": str(terminal["updated_at"]),
                }
                if terminal is not None
                else None
            ),
        }
        payload = {"effect": effect, "summary": summary}
        append_board_event(
            connection,
            event_id=content_event_id("outbox.updated", str(row["effect_id"]), payload),
            resource_key=f"outbox.updated:{row['effect_id']}",
            project_id=project_id,
            map_id=map_id,
            event_type="outbox.updated",
            payload=payload,
            committed_at=str(row["updated_at"]),
        )

    @staticmethod
    def _intent(row: sqlite3.Row) -> OutboxIntent:
        acknowledgment = row["acknowledgment_json"]
        return OutboxIntent(
            effect_id=str(row["effect_id"]),
            effect_type=str(row["effect_type"]),
            map_id=str(row["map_id"]),
            payload_hash=str(row["payload_hash"]),
            payload=json.loads(row["payload_json"]),
            state=str(row["state"]),
            owner_id=row["owner_id"],
            lease_expires_at=row["lease_expires_at"],
            attempt_count=int(row["attempt_count"]),
            next_attempt_at=row["next_attempt_at"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            acknowledged_at=row["acknowledged_at"],
            acknowledgment=(json.loads(acknowledgment) if acknowledgment else None),
            last_error_type=row["last_error_type"],
            last_error_message=row["last_error_message"],
            terminal_reason=row["terminal_reason"],
        )

    @staticmethod
    def _text(value: Any, *, name: str, maximum: int | None = None) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Outbox {name} must be a non-empty string")
        normalized = value.strip()
        if maximum is not None and len(normalized) > maximum:
            raise ValueError(f"Outbox {name} must not exceed {maximum} characters")
        return normalized

    @staticmethod
    def _timestamp(value: Any, *, name: str) -> datetime:
        normalized = OutboxRepository._text(value, name=name)
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"Outbox {name} must be RFC 3339") from error
        if parsed.tzinfo is None:
            raise ValueError(f"Outbox {name} must include a timezone")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _format_timestamp(value: datetime) -> str:
        # SQLite compares these columns lexically, so every persisted/queried value
        # must use the same precision.  Mixed ``...:01Z`` and
        # ``...:01.500000Z`` forms do not sort chronologically.
        return (
            value.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
