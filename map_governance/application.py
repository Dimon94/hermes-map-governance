"""The single application interface shared by every plugin adapter."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Callable, NoReturn, cast

from .approvals import (
    APPROVAL_DECISIONS,
    ApprovalHistoryEvent,
    ApprovalPacket,
    AuthorityEnvelopePolicy,
    GovernanceActorIdentity,
    approval_events_semantically_compatible,
    normalized_hash,
    normalized_json,
)
from .stages import (
    ALLOWED_TRANSITIONS,
    available_transitions,
    executive_stage,
    rejection_reason,
)
from .storage import PluginStorage
from .effects import (
    COORDINATOR_RESUME,
    SESSION_RESUME,
    TRACKER_APPROVAL_EVENT,
    TRACKER_DECISION,
    TRACKER_PM_REPORT,
    TRACKER_STAGE_TRANSITION,
    CoordinatorResumeBoundary,
    CoordinatorResumeEffectAdapter,
    SessionResumeBoundary,
    SessionResumeEffectAdapter,
    TrackerEffectAdapter,
    TrackerEffectPayloadConflict,
    TrackerStageEffectConflict,
)
from .outbox import (
    EffectConfirmation,
    EffectRetryableError,
    EffectTerminalError,
    OutboxConflictError,
    OutboxDispatcher,
    OutboxIntent,
    OutboxRepository,
    OutboxRuntime,
    OutboxSettings,
)
from .sessions import (
    CEOSessionRunner,
    CanonicalSession,
    canonical_session_identity,
    canonical_session_title,
)
from .reports import PMReport, PMReportDraft, TrackerPMReportRecord
from .tracker import (
    GitHubTrackerAdapter,
    TrackerAdapter,
    TrackerApprovalRecord,
    StructuredDecision,
    TrackerDecisionRecord,
    TrackerError,
    TrackerIssue,
    TrackerProject,
)


class MapBindingError(ValueError):
    """Raised when a requested binding violates governance identity rules."""


@dataclass(frozen=True)
class GovernanceRequestIdentity:
    """Profile and session identity supplied by the active Hermes request."""

    profile_name: str
    session_id: str


class GovernanceAuthorizationError(PermissionError):
    """Raised after an unauthorized governance request is durably audited."""

    def __init__(self, *, action: str, map_id: str, reason: str) -> None:
        self.action = action
        self.map_id = map_id
        self.reason = reason
        super().__init__(f"Governance request denied: {reason}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "authorization_denied",
            "action": self.action,
            "map_id": self.map_id,
            "reason": self.reason,
            "retryable": False,
        }


class StructuredDecisionConflict(ValueError):
    """Raised when one stable decision id is reused for another payload."""

    def __init__(self, *, decision_id: str) -> None:
        self.decision_id = decision_id
        super().__init__(
            f"Decision idempotency identity has a different payload: {decision_id}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "decision_conflict",
            "decision_id": self.decision_id,
            "reason": "stable decision identity already belongs to another payload",
            "retryable": False,
        }


class TrackerDecisionConfirmationError(TrackerError):
    """Raised when a tracker mutation is not visible in authoritative history."""


class TrackerApprovalConfirmationError(TrackerError):
    """Raised when an approval event is not visible in tracker history."""


class TrackerPMReportConfirmationError(TrackerError):
    """Raised when a PM report is not visible in tracker Issue history."""


class PMReportConflict(ValueError):
    """Raised when one stable PM report id is reused for other content."""

    def __init__(self, *, record_id: str) -> None:
        self.record_id = record_id
        super().__init__(f"PM report identity has a different payload: {record_id}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "pm_report_conflict",
            "record_id": self.record_id,
            "reason": "stable PM report identity belongs to another payload",
            "retryable": False,
        }


class ApprovalRequestConflict(ValueError):
    """Raised when a stable approval or mutation id belongs to other content."""

    def __init__(self, *, request_id: str, reason: str) -> None:
        self.request_id = request_id
        self.reason = reason
        super().__init__(f"Approval request conflict for {request_id}: {reason}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "approval_conflict",
            "request_id": self.request_id,
            "reason": self.reason,
            "retryable": False,
        }


class ApprovalEnforcementError(PermissionError):
    """Raised after policy rejects an approval request or protected action."""

    def __init__(self, *, action: str, map_id: str, reason: str) -> None:
        self.action = action
        self.map_id = map_id
        self.reason = reason
        super().__init__(f"Approval enforcement denied {action}: {reason}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "approval_denied",
            "action": self.action,
            "map_id": self.map_id,
            "reason": self.reason,
            "retryable": False,
        }


class MapTransitionError(ValueError):
    """Raised when the governance policy rejects a requested stage change."""

    def __init__(
        self, *, current_stage: str, requested_stage: str, reason: str
    ) -> None:
        self.current_stage = current_stage
        self.requested_stage = requested_stage
        self.reason = reason
        super().__init__(
            f"Cannot transition Map from {current_stage!r} to "
            f"{requested_stage!r}: {reason}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "invalid_transition",
            "current_stage": self.current_stage,
            "requested_stage": self.requested_stage,
            "reason": self.reason,
            "retryable": False,
        }


class MapTransitionConflict(MapTransitionError):
    """Raised when a concurrent tracker change requires refresh or retry."""

    def as_dict(self) -> dict[str, Any]:
        detail = super().as_dict()
        detail.update(type="transition_conflict", retryable=True)
        return detail


class CEOSessionRepairRequired(RuntimeError):
    """Raised when canonical session identity cannot be resolved safely."""

    def __init__(self, *, reason: str, candidate_count: int = 0) -> None:
        self.reason = reason
        self.candidate_count = candidate_count
        super().__init__(f"Canonical CEO session requires repair: {reason}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "repair_required",
            "reason": self.reason,
            "candidate_count": self.candidate_count,
            "retryable": False,
        }


class CEOSessionAmbiguityError(CEOSessionRepairRequired):
    """Raised when more than one exact canonical session candidate exists."""


_OPERATION_LOCKS: dict[tuple[str, str], RLock] = {}
_OPERATION_LOCKS_GUARD = Lock()


def _operation_lock(namespace: str, key: str) -> RLock:
    with _OPERATION_LOCKS_GUARD:
        return _OPERATION_LOCKS.setdefault((namespace, key), RLock())


class MapGovernanceApplication:
    """Coordinate Map Governance operations behind one public seam."""

    def __init__(
        self,
        *,
        plugin_root: Path,
        storage_root: Path,
        tracker: TrackerAdapter | None = None,
        session_runner: CEOSessionRunner | None = None,
        profile_name: str | None = None,
        clock: Callable[[], datetime] | None = None,
        authority_policy: AuthorityEnvelopePolicy | None = None,
        outbox_settings: OutboxSettings | None = None,
        outbox_owner_id: str | None = None,
        outbox_crash_injector: Callable[[str, OutboxIntent], None] | None = None,
        coordinator_resume: CoordinatorResumeBoundary | None = None,
    ) -> None:
        self._plugin_root = plugin_root.resolve()
        self._storage = PluginStorage(storage_root)
        self._tracker = tracker or GitHubTrackerAdapter()
        self._session_runner = session_runner
        self._profile_name = profile_name
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._authority_policy = (
            authority_policy or AuthorityEnvelopePolicy.from_settings(None)
        )
        self._outbox = OutboxRepository(storage_root)
        self._outbox_settings = outbox_settings or OutboxSettings()
        self._outbox_owner_id = outbox_owner_id or (
            f"map-governance:{os.getpid()}:{uuid.uuid4()}"
        )
        self._outbox_runtime: OutboxRuntime | None = None
        tracker_effects = TrackerEffectAdapter(self._tracker)
        effect_adapters: dict[str, Any] = {
            TRACKER_STAGE_TRANSITION: tracker_effects,
            TRACKER_DECISION: tracker_effects,
            TRACKER_APPROVAL_EVENT: tracker_effects,
            TRACKER_PM_REPORT: tracker_effects,
        }
        missing_session_effect_methods = (
            [
                name
                for name in ("has_resume_marker", "resume_once")
                if self._session_runner is not None
                and not hasattr(self._session_runner, name)
            ]
            if self._session_runner is not None
            else []
        )
        if missing_session_effect_methods:
            raise ValueError(
                "CEO session runner must provide durable resume readback/apply: "
                + ", ".join(missing_session_effect_methods)
            )
        self._session_resume_outbox = self._session_runner is not None
        if self._session_resume_outbox and self._session_runner is not None:
            effect_adapters[SESSION_RESUME] = SessionResumeEffectAdapter(
                cast(SessionResumeBoundary, self._session_runner)
            )
        self._coordinator_resume = coordinator_resume
        if coordinator_resume is not None:
            effect_adapters[COORDINATOR_RESUME] = CoordinatorResumeEffectAdapter(
                coordinator_resume
            )
        self._outbox_dispatcher = OutboxDispatcher(
            repository=self._outbox,
            adapters=effect_adapters,
            clock=self._clock,
            settings=self._outbox_settings,
            completion=self._complete_external_effect,
            crash_injector=outbox_crash_injector,
        )

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
            "status": "ready"
            if all(path.is_file() for path in required_files)
            else "not_ready",
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
        project_rows, map_rows = self._storage.board_rows()
        project_by_id = {
            row["project_id"]: self._project_projection(row) for row in project_rows
        }
        maps = []
        for row in map_rows:
            project = project_by_id.get(row["project_id"])
            if project is None:
                continue
            card = self._card_with_summary(
                row,
                project_url=project["tracker"]["url"],
            )
            project["maps"].append(card)
            maps.append(card)
        return {
            "projects": list(project_by_id.values()),
            "maps": maps,
            "empty_state": {
                "title": "No Maps are bound",
                "description": (
                    "Bind an existing GitHub Map Issue to start a governance board."
                ),
            },
        }

    def map_detail(self, *, map_id: str) -> dict[str, Any]:
        """Return one Map projection without session-inventory disclosure."""
        for card in self.board()["maps"]:
            if card["id"] == map_id:
                card["recent_decisions"] = self._storage.recent_decisions(map_id=map_id)
                card["approvals"] = self._approval_collection(map_id=map_id)
                card["pm_reports"] = self._storage.recent_pm_reports(map_id=map_id)
                card["external_effects"] = self._outbox.map_summary(map_id)
                return card
        raise MapBindingError(f"Map is not bound: {map_id}")

    def outbox_status(self, *, effect_id: str) -> dict[str, Any]:
        """Return one operator-visible durable execution record."""
        return self._outbox.operator_status(effect_id)

    def recover_outbox(self, *, limit: int = 100) -> dict[str, Any]:
        """Dispatch due durable effects during startup or an operator recovery."""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("Outbox recovery limit must be between 1 and 1000")
        outcomes = []
        while len(outcomes) < limit:
            outcome = self._outbox_dispatcher.dispatch_next(
                owner_id=self._outbox_owner_id
            )
            if outcome is None:
                break
            outcomes.append(
                {
                    "effect_id": outcome.effect_id,
                    "state": outcome.state,
                    "attempt_number": outcome.attempt_number,
                    "reconciled_by_readback": outcome.reconciled_by_readback,
                    "next_attempt_at": outcome.next_attempt_at,
                    "terminal_reason": outcome.terminal_reason,
                }
            )
        return {"processed_count": len(outcomes), "outcomes": outcomes}

    def start_outbox_runtime(
        self,
        *,
        poll_seconds: float | None = None,
    ) -> OutboxRuntime:
        """Start one recurring durable dispatcher for this composition root."""
        if self._outbox_runtime is None:
            self._outbox_runtime = OutboxRuntime(
                dispatcher=self._outbox_dispatcher,
                owner_id=f"{self._outbox_owner_id}:runtime",
                poll_seconds=(
                    self._outbox_settings.poll_seconds
                    if poll_seconds is None
                    else poll_seconds
                ),
            )
            self._outbox_runtime.start()
        return self._outbox_runtime

    def stop_outbox_runtime(self) -> None:
        """Stop the recurring dispatcher during an owned runtime shutdown."""
        if self._outbox_runtime is not None:
            self._outbox_runtime.stop()
            self._outbox_runtime = None

    def repair_outbox(
        self,
        *,
        effect_id: str,
        repair_id: str,
        note: str,
    ) -> dict[str, Any]:
        """Audit an explicit operator repair and requeue a terminal intent."""
        self._outbox.repair(
            effect_id=effect_id,
            repair_id=repair_id,
            note=note,
            requested_at=self._synchronized_at(),
        )
        return self._outbox.operator_status(effect_id)

    def executive_state(
        self,
        *,
        map_id: str,
        request_identity: GovernanceRequestIdentity,
    ) -> dict[str, Any]:
        """Return the executive read model for an authorized CEO request."""
        self._authorize_ceo_request(
            map_id=map_id,
            action="inspect",
            request_identity=request_identity,
        )
        return {
            "map": (detail := self.map_detail(map_id=map_id)),
            "recent_decisions": detail["recent_decisions"],
            "approvals": detail["approvals"],
            "authority_envelope": self._authority_policy.projection(),
            "delivery_summary": detail["delivery_summary"],
        }

    def assign_pm(
        self,
        *,
        map_id: str,
        request_identity: GovernanceRequestIdentity,
        coordinator_id: str,
    ) -> dict[str, Any]:
        """Establish one immutable request-scoped PM assignment."""
        if self._storage.map_binding(map_id) is None:
            raise MapBindingError(f"Map is not bound: {map_id}")
        if not request_identity.profile_name or not request_identity.session_id:
            self._deny_governance_request(
                map_id=map_id,
                action="pm:assign",
                request_identity=request_identity,
                reason="pm_request_identity_missing",
            )
        if not isinstance(coordinator_id, str) or not coordinator_id.strip():
            self._deny_governance_request(
                map_id=map_id,
                action="pm:assign",
                request_identity=request_identity,
                reason="pm_coordinator_identity_missing",
            )
        assigned_map = self._storage.pm_assignment_for_request(
            profile_name=request_identity.profile_name,
            session_id=request_identity.session_id,
        )
        if assigned_map is not None and assigned_map["map_id"] != map_id:
            self._deny_governance_request(
                map_id=map_id,
                action="pm:assign",
                request_identity=request_identity,
                reason="pm_identity_assigned_to_another_map",
            )
        map_assignment = self._storage.pm_assignment(map_id)
        if map_assignment is not None and (
            map_assignment["profile_name"] != request_identity.profile_name
            or map_assignment["session_id"] != request_identity.session_id
            or map_assignment["coordinator_id"] != coordinator_id.strip()
        ):
            self._deny_governance_request(
                map_id=map_id,
                action="pm:assign",
                request_identity=request_identity,
                reason="pm_assignment_conflict",
            )
        try:
            idempotent = self._storage.save_pm_assignment(
                map_id=map_id,
                profile_name=request_identity.profile_name,
                session_id=request_identity.session_id,
                coordinator_id=coordinator_id.strip(),
                assigned_at=self._synchronized_at(),
            )
        except ValueError:
            self._deny_governance_request(
                map_id=map_id,
                action="pm:assign",
                request_identity=request_identity,
                reason="pm_assignment_conflict",
            )
        assignment = self._storage.pm_assignment(map_id)
        return {
            "assignment": self._pm_assignment_projection(assignment),
            "idempotent": idempotent,
        }

    def begin_pm_turn(
        self,
        *,
        map_id: str,
        request_identity: GovernanceRequestIdentity,
        coordinator_id: str,
        turn_id: str,
    ) -> dict[str, Any]:
        """Resume an assigned PM for one coordinator-controlled turn."""
        lock_key = f"{self._storage.database}:{map_id}"
        with _operation_lock("pm-turn", lock_key):
            with self._storage.pm_turn_lease(map_id):
                return self._begin_pm_turn(
                    map_id=map_id,
                    request_identity=request_identity,
                    coordinator_id=coordinator_id,
                    turn_id=turn_id,
                )

    def _begin_pm_turn(
        self,
        *,
        map_id: str,
        request_identity: GovernanceRequestIdentity,
        coordinator_id: str,
        turn_id: str,
    ) -> dict[str, Any]:
        assignment = self._authorize_pm_coordinator(
            map_id=map_id,
            action="pm:begin_turn",
            request_identity=request_identity,
            coordinator_id=coordinator_id,
        )
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError("PM turn requires a stable turn identity")
        normalized_turn_id = turn_id.strip()
        if (
            assignment["state"] == "active"
            and assignment["active_turn_id"] != normalized_turn_id
        ):
            raise ValueError("PM assignment already has an active turn")
        unresolved_turns = {
            str(intent.payload.get("turn_id", ""))
            for intent in self._outbox.unfinished_intents(
                map_id=map_id,
                effect_type=COORDINATOR_RESUME,
            )
        }
        if unresolved_turns - {normalized_turn_id}:
            raise ValueError("PM assignment already has an unresolved coordinator turn")
        if self._coordinator_resume is not None:
            effect_id = f"coordinator-resume:{map_id}:{normalized_turn_id}"
            try:
                enqueued = self._outbox.enqueue(
                    effect_id=effect_id,
                    effect_type=COORDINATOR_RESUME,
                    map_id=map_id,
                    payload={
                        "profile_name": request_identity.profile_name,
                        "session_id": request_identity.session_id,
                        "coordinator_id": coordinator_id,
                        "turn_id": normalized_turn_id,
                    },
                    created_at=self._synchronized_at(),
                )
            except OutboxConflictError as error:
                raise ValueError(
                    "PM turn identity belongs to another coordinator resume"
                ) from error
            outcome = self._outbox_dispatcher.dispatch_effect(
                effect_id=effect_id,
                owner_id=self._outbox_owner_id,
                expedite_retry=not enqueued.created,
            )
            status = self._outbox.intent(effect_id)
            if status is None:  # pragma: no cover
                raise RuntimeError("Coordinator resume Outbox intent disappeared")
            if status.state != "succeeded":
                raise RuntimeError(
                    status.last_error_message
                    or status.terminal_reason
                    or "Coordinator resume is pending"
                )
            assignment = self._storage.pm_assignment(map_id)
            return {
                "coordinator": self._pm_assignment_projection(assignment),
                "idempotent": (
                    not enqueued.created
                    or (outcome is not None and outcome.reconciled_by_readback)
                ),
            }
        idempotent = self._storage.begin_pm_turn(
            map_id=map_id,
            coordinator_id=coordinator_id,
            turn_id=turn_id.strip(),
            started_at=self._synchronized_at(),
        )
        assignment = self._storage.pm_assignment(map_id)
        return {
            "coordinator": self._pm_assignment_projection(assignment),
            "idempotent": idempotent,
        }

    def complete_pm_dispatch(
        self,
        *,
        map_id: str,
        request_identity: GovernanceRequestIdentity,
        coordinator_id: str,
        turn_id: str,
        dispatch_id: str,
    ) -> dict[str, Any]:
        """Record the coordinator's dispatch boundary and leave the PM idle."""
        lock_key = f"{self._storage.database}:{map_id}"
        with _operation_lock("pm-turn", lock_key):
            with self._storage.pm_turn_lease(map_id):
                self._authorize_pm_coordinator(
                    map_id=map_id,
                    action="pm:dispatch",
                    request_identity=request_identity,
                    coordinator_id=coordinator_id,
                )
                for name, value in (
                    ("turn_id", turn_id),
                    ("dispatch_id", dispatch_id),
                ):
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError(f"PM dispatch requires a stable {name}")
                normalized_turn_id = turn_id.strip()
                if self._pm_report_outcome_reserved(
                    map_id=map_id,
                    turn_id=normalized_turn_id,
                ):
                    raise ValueError("PM turn already has a reserved report outcome")
                idempotent = self._storage.finish_pm_turn(
                    map_id=map_id,
                    turn_id=normalized_turn_id,
                    outcome="dispatch",
                    outcome_id=dispatch_id.strip(),
                    finished_at=self._synchronized_at(),
                )
                return {
                    "coordinator": self._pm_assignment_projection(
                        self._storage.pm_assignment(map_id)
                    ),
                    "idempotent": idempotent,
                }

    def report_pm(
        self,
        *,
        request_identity: GovernanceRequestIdentity,
        report: PMReportDraft,
    ) -> dict[str, Any]:
        """Append a request-authorized PM report, then end the turn idle."""
        assignment = self._storage.pm_assignment_for_request(
            profile_name=request_identity.profile_name,
            session_id=request_identity.session_id,
        )
        if assignment is None:
            self._deny_governance_request(
                map_id="unassigned",
                action="pm:report",
                request_identity=request_identity,
                reason="pm_assignment_missing",
            )
        map_id = str(assignment["map_id"])
        bound_report = report.assign_to(map_id)
        binding = self._storage.map_binding(map_id)
        if binding is None:
            raise MapBindingError(f"Map is not bound: {map_id}")
        lock_key = f"{self._storage.database}:{map_id}"
        with _operation_lock("pm-turn", lock_key):
            with self._storage.pm_turn_lease(map_id):
                assignment = self._storage.pm_assignment_for_request(
                    profile_name=request_identity.profile_name,
                    session_id=request_identity.session_id,
                )
                if assignment is None or assignment["map_id"] != map_id:
                    self._deny_governance_request(
                        map_id=map_id,
                        action="pm:report",
                        request_identity=request_identity,
                        reason="pm_assignment_missing",
                    )
                return self._report_pm_under_turn_lease(
                    map_id=map_id,
                    binding=binding,
                    assignment=assignment,
                    bound_report=bound_report,
                )

    def _report_pm_under_turn_lease(
        self,
        *,
        map_id: str,
        binding: dict[str, Any],
        assignment: dict[str, Any],
        bound_report: PMReport,
    ) -> dict[str, Any]:
        lock_key = f"{self._storage.database}:{map_id}"
        with _operation_lock("pm-report", lock_key):
            with self._storage.pm_report_lease(map_id):
                effect_id = (
                    f"tracker-pm-report:{map_id}:{bound_report.content.record_id}"
                )
                existing_intent = self._outbox.intent(effect_id)
                if existing_intent is None:
                    active_turn_id = assignment.get("active_turn_id")
                    if assignment["state"] != "active" or not active_turn_id:
                        raise ValueError(
                            "PM report requires an active coordinator turn"
                        )
                    if self._pm_report_outcome_reserved(
                        map_id=map_id,
                        turn_id=str(active_turn_id),
                    ):
                        raise ValueError(
                            "PM turn already has a reserved report outcome"
                        )
                    issue = self._tracker.get_issue(binding["issue_url"])
                    if issue.id != map_id:
                        raise MapBindingError("Bound GitHub Issue identity changed")
                    current_stage = self._executive_stage(issue)
                    requested_stage = self._pm_report_requested_stage(
                        current_stage=current_stage,
                        report=bound_report,
                    )
                    effect_payload = {
                        "issue_url": binding["issue_url"],
                        "issue_id": map_id,
                        "project_id": binding["project_id"],
                        "report": bound_report.payload(),
                        "active_turn_id": str(active_turn_id),
                        "expected_stage": current_stage,
                        "requested_stage": requested_stage,
                    }
                else:
                    persisted_report = existing_intent.payload.get("report")
                    if (
                        not isinstance(persisted_report, dict)
                        or PMReport.from_payload(persisted_report) != bound_report
                    ):
                        raise PMReportConflict(record_id=bound_report.content.record_id)
                    effect_payload = existing_intent.payload
                try:
                    enqueued = self._outbox.enqueue(
                        effect_id=effect_id,
                        effect_type=TRACKER_PM_REPORT,
                        map_id=map_id,
                        payload=effect_payload,
                        created_at=self._synchronized_at(),
                    )
                except OutboxConflictError as error:
                    raise PMReportConflict(
                        record_id=bound_report.content.record_id
                    ) from error
                outcome = self._outbox_dispatcher.dispatch_effect(
                    effect_id=effect_id,
                    owner_id=self._outbox_owner_id,
                    expedite_retry=not enqueued.created,
                )
                self._require_pm_effect_success(
                    effect_id=effect_id,
                    record_id=bound_report.content.record_id,
                )
                stage_effect_id = self._pm_stage_effect_id(
                    map_id=map_id,
                    record_id=bound_report.content.record_id,
                )
                if self._outbox.intent(stage_effect_id) is not None:
                    self._outbox_dispatcher.dispatch_effect(
                        effect_id=stage_effect_id,
                        owner_id=self._outbox_owner_id,
                        expedite_retry=True,
                    )
                    stage_status = self._outbox.intent(stage_effect_id)
                    if stage_status is None:  # pragma: no cover
                        raise RuntimeError("PM stage Outbox intent disappeared")
                    if stage_status.state != "succeeded":
                        raise TrackerError(
                            stage_status.last_error_message
                            or stage_status.terminal_reason
                            or "PM report stage transition is pending"
                        )
                stored_projection = next(
                    (
                        item
                        for item in self._storage.recent_pm_reports(map_id=map_id)
                        if item["record_id"] == bound_report.content.record_id
                    ),
                    None,
                )
                if stored_projection is None:  # pragma: no cover
                    raise RuntimeError("Confirmed PM report projection disappeared")
                report_status = self._outbox.intent(effect_id)
                if report_status is None or report_status.acknowledgment is None:
                    raise RuntimeError("Confirmed PM report acknowledgment disappeared")
                report_acknowledgment = report_status.acknowledgment
                projection = self._pm_report_projection(
                    TrackerPMReportRecord(
                        report=PMReport.from_payload(
                            dict(report_acknowledgment["report"])
                        ),
                        tracker_record_id=str(
                            report_acknowledgment["tracker_record_id"]
                        ),
                        tracker_record_url=str(
                            report_acknowledgment["tracker_record_url"]
                        ),
                    ),
                    confirmed_at=str(stored_projection["confirmed_at"]),
                )
                return {
                    "map_id": map_id,
                    "report": projection,
                    "idempotent": (
                        not enqueued.created
                        or (outcome is not None and outcome.reconciled_by_readback)
                    ),
                    "coordinator": self._pm_assignment_projection(
                        self._storage.pm_assignment(map_id)
                    ),
                }

    def _pm_report_outcome_reserved(self, *, map_id: str, turn_id: str) -> bool:
        return any(
            intent.payload.get("active_turn_id") == turn_id
            for intent in self._outbox.effect_intents(
                map_id=map_id,
                effect_type=TRACKER_PM_REPORT,
            )
        )

    def pm_state(
        self,
        *,
        request_identity: GovernanceRequestIdentity,
    ) -> dict[str, Any]:
        """Return the assigned PM's executive-only resumable state."""
        assignment = self._storage.pm_assignment_for_request(
            profile_name=request_identity.profile_name,
            session_id=request_identity.session_id,
        )
        if assignment is None:
            self._deny_governance_request(
                map_id="unassigned",
                action="pm:inspect",
                request_identity=request_identity,
                reason="pm_assignment_missing",
            )
        map_id = str(assignment["map_id"])
        detail = self.map_detail(map_id=map_id)
        return {
            "assignment": {
                "map": {
                    "id": map_id,
                    "tracker": detail["tracker"],
                    "title": detail["title"],
                    "stage": detail["stage"],
                },
                "coordinator": self._pm_assignment_projection(assignment),
            },
            "delivery_summary": detail["delivery_summary"],
            "recent_decisions": detail["recent_decisions"],
        }

    def enforce_assigned_pm_toolset(
        self,
        *,
        request_identity: GovernanceRequestIdentity,
        tool_name: str,
        allowed_tool_names: frozenset[str],
    ) -> bool:
        """Block non-PM tools only inside a persistently assigned PM session."""
        assignment = self._storage.pm_assignment_for_request(
            profile_name=request_identity.profile_name,
            session_id=request_identity.session_id,
        )
        if assignment is None:
            return False
        if tool_name in allowed_tool_names:
            return True
        self._deny_governance_request(
            map_id=str(assignment["map_id"]),
            action=f"invoke_tool:{tool_name}",
            request_identity=request_identity,
            reason="tool_outside_pm_toolset",
        )

    def _authorize_pm_coordinator(
        self,
        *,
        map_id: str,
        action: str,
        request_identity: GovernanceRequestIdentity,
        coordinator_id: str,
    ) -> dict[str, Any]:
        assignment = self._storage.pm_assignment(map_id)
        if assignment is None:
            self._deny_governance_request(
                map_id=map_id,
                action=action,
                request_identity=request_identity,
                reason="pm_assignment_missing",
            )
        if (
            assignment["profile_name"] != request_identity.profile_name
            or assignment["session_id"] != request_identity.session_id
        ):
            self._deny_governance_request(
                map_id=map_id,
                action=action,
                request_identity=request_identity,
                reason="pm_assignment_request_mismatch",
            )
        if assignment["coordinator_id"] != coordinator_id:
            self._deny_governance_request(
                map_id=map_id,
                action=action,
                request_identity=request_identity,
                reason="pm_coordinator_mismatch",
            )
        return assignment

    @staticmethod
    def _pm_report_requested_stage(
        *,
        current_stage: str,
        report: PMReport,
    ) -> str | None:
        if current_stage == "delivery":
            if report.content.report_type == "acceptance":
                return "acceptance"
            if (
                report.content.report_type in {"question", "blocker"}
                and report.content.blocking is True
            ):
                return "decision"
        return None

    def _require_pm_effect_success(self, *, effect_id: str, record_id: str) -> None:
        status = self._outbox.intent(effect_id)
        if status is None:  # pragma: no cover
            raise RuntimeError("PM report Outbox intent disappeared")
        if status.state == "succeeded":
            return
        if status.last_error_type == TrackerEffectPayloadConflict.__name__:
            raise PMReportConflict(record_id=record_id)
        raise TrackerPMReportConfirmationError(
            status.last_error_message
            or "Tracker did not confirm the PM report in Issue history"
        )

    @staticmethod
    def _pm_stage_effect_id(*, map_id: str, record_id: str) -> str:
        return f"stage-transition:pm-report:{map_id}:{record_id}"

    @staticmethod
    def _pm_report_records_by_id(
        records: list[TrackerPMReportRecord],
    ) -> dict[str, TrackerPMReportRecord]:
        by_id: dict[str, TrackerPMReportRecord] = {}
        for record in records:
            record_id = record.report.content.record_id
            existing = by_id.get(record_id)
            if existing is not None and existing.report != record.report:
                raise PMReportConflict(record_id=record_id)
            by_id.setdefault(record_id, record)
        return by_id

    @staticmethod
    def _pm_report_projection(
        record: TrackerPMReportRecord,
        *,
        confirmed_at: str,
    ) -> dict[str, Any]:
        return {
            **record.report.payload(),
            "tracker": {
                "id": record.tracker_record_id,
                "url": record.tracker_record_url,
            },
            "confirmed_at": confirmed_at,
        }

    @staticmethod
    def _pm_assignment_projection(
        assignment: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if assignment is None:
            raise RuntimeError("PM assignment disappeared")
        return {
            "map_id": assignment["map_id"],
            "state": assignment["state"],
            "active_turn_id": assignment.get("active_turn_id"),
            "last_turn_id": assignment.get("last_turn_id"),
            "last_outcome": assignment.get("last_outcome"),
            "last_outcome_id": assignment.get("last_outcome_id"),
        }

    def record_decision(
        self,
        *,
        map_id: str,
        request_identity: GovernanceRequestIdentity,
        decision: StructuredDecision,
    ) -> dict[str, Any]:
        """Append a CEO decision to tracker truth before projecting it locally."""
        self._authorize_ceo_request(
            map_id=map_id,
            action="record_decision",
            request_identity=request_identity,
        )
        if decision.authority != "ceo":
            self._deny_governance_request(
                map_id=map_id,
                action="record_decision",
                request_identity=request_identity,
                reason="decision_authority_mismatch",
            )
        authority_context = decision.authority_context or {}
        decision_authority = self._authority_policy.classify(
            decision.type,
            decision_payload=authority_context.get("decision_payload"),
            requested_scope=authority_context.get("requested_scope"),
        )
        if decision_authority != "ceo":
            self._deny_governance_request(
                map_id=map_id,
                action="record_decision",
                request_identity=request_identity,
                reason=(
                    "chairman_approval_required"
                    if decision_authority == "chairman"
                    else "decision_class_not_configured"
                ),
            )
        lock_key = f"{self._storage.database}:{map_id}"
        with _operation_lock("decision", lock_key):
            with self._storage.decision_lease(map_id):
                return self._record_decision(map_id=map_id, decision=decision)

    def _record_decision(
        self,
        *,
        map_id: str,
        decision: StructuredDecision,
    ) -> dict[str, Any]:
        binding = self._storage.map_binding(map_id)
        if binding is None:
            raise MapBindingError(f"Map is not bound: {map_id}")
        effect_id = f"tracker-decision:{map_id}:{decision.decision_id}"
        try:
            enqueued = self._outbox.enqueue(
                effect_id=effect_id,
                effect_type=TRACKER_DECISION,
                map_id=map_id,
                payload={
                    "issue_url": binding["issue_url"],
                    "issue_id": map_id,
                    "decision": decision.payload(),
                },
                created_at=self._synchronized_at(),
            )
        except OutboxConflictError as error:
            raise StructuredDecisionConflict(
                decision_id=decision.decision_id
            ) from error
        outcome = self._outbox_dispatcher.dispatch_effect(
            effect_id=effect_id,
            owner_id=self._outbox_owner_id,
            expedite_retry=not enqueued.created,
        )
        status = self._outbox.intent(effect_id)
        if status is None:  # pragma: no cover
            raise RuntimeError("Decision Outbox intent disappeared")
        if status.state != "succeeded":
            if status.last_error_type == TrackerEffectPayloadConflict.__name__:
                raise StructuredDecisionConflict(decision_id=decision.decision_id)
            message = status.last_error_message or "Tracker decision is pending retry"
            if status.last_error_type == EffectRetryableError.__name__:
                raise TrackerDecisionConfirmationError(message)
            raise TrackerError(message)
        acknowledgment = status.acknowledgment or {}
        confirmed_payload = acknowledgment.get("decision")
        if not isinstance(confirmed_payload, dict):  # pragma: no cover
            raise RuntimeError("Confirmed decision acknowledgment disappeared")
        projection = self._decision_projection(
            TrackerDecisionRecord(
                decision=StructuredDecision(**confirmed_payload),
                tracker_record_id=str(acknowledgment["tracker_record_id"]),
                tracker_record_url=str(acknowledgment["tracker_record_url"]),
            ),
            confirmed_at=(
                self._format_datetime(
                    datetime.fromisoformat(
                        status.acknowledged_at.replace("Z", "+00:00")
                    )
                )
                if status.acknowledged_at
                else self._synchronized_at()
            ),
        )
        return {
            "map_id": map_id,
            "decision": projection,
            "idempotent": (
                not enqueued.created
                or (outcome is not None and outcome.reconciled_by_readback)
            ),
        }

    @staticmethod
    def _decision_records_by_id(
        records: list[TrackerDecisionRecord],
    ) -> dict[str, TrackerDecisionRecord]:
        by_id: dict[str, TrackerDecisionRecord] = {}
        for record in records:
            decision_id = record.decision.decision_id
            existing = by_id.get(decision_id)
            if existing is not None and existing.decision != record.decision:
                raise StructuredDecisionConflict(decision_id=decision_id)
            by_id.setdefault(decision_id, record)
        return by_id

    @staticmethod
    def _decision_projection(
        record: TrackerDecisionRecord,
        *,
        confirmed_at: str,
    ) -> dict[str, Any]:
        return {
            **record.decision.payload(),
            "tracker": {
                "id": record.tracker_record_id,
                "url": record.tracker_record_url,
            },
            "confirmed_at": confirmed_at,
        }

    def request_approval(
        self,
        *,
        map_id: str,
        request_identity: GovernanceRequestIdentity,
        packet: ApprovalPacket,
    ) -> dict[str, Any]:
        """Persist one CEO escalation only after tracker history confirms it."""
        self._authorize_ceo_request(
            map_id=map_id,
            action="request_approval",
            request_identity=request_identity,
        )
        decision_authority = self._authority_policy.classify(
            packet.decision_class,
            decision_payload=packet.decision_payload,
            requested_scope=packet.requested_scope,
        )
        if decision_authority == "ceo":
            self._deny_approval_enforcement(
                map_id=map_id,
                action="request_approval",
                reason="decision_within_ceo_authority",
                profile_name=request_identity.profile_name,
                session_id=request_identity.session_id,
            )
        if decision_authority == "unconfigured":
            self._deny_approval_enforcement(
                map_id=map_id,
                action="request_approval",
                reason="decision_class_not_configured",
                profile_name=request_identity.profile_name,
                session_id=request_identity.session_id,
            )
        if packet.requested_scope.get("map_id") != map_id:
            self._deny_approval_enforcement(
                map_id=map_id,
                action="request_approval",
                reason="approval_scope_mismatch",
                profile_name=request_identity.profile_name,
                session_id=request_identity.session_id,
            )
        binding = self._storage.map_binding(map_id)
        if binding is None:
            raise MapBindingError(f"Map is not bound: {map_id}")
        lock_key = f"{self._storage.database}:{map_id}"
        with _operation_lock("approval", lock_key):
            with self._storage.approval_lease(map_id):
                existing = self._storage.approval(packet.request_id)
                if existing is not None:
                    if (
                        existing["map_id"] != map_id
                        or existing["packet_hash"] != packet.packet_hash
                    ):
                        raise ApprovalRequestConflict(
                            request_id=packet.request_id,
                            reason="stable request identity belongs to another packet",
                        )
                    return {
                        "map_id": map_id,
                        "approval": self._approval_projection(existing),
                        "idempotent": True,
                    }

                requested_at = self._synchronized_at()
                event = ApprovalHistoryEvent(
                    event_id=f"approval:{packet.request_id}:request",
                    request_id=packet.request_id,
                    event_type="requested",
                    occurred_at=requested_at,
                    payload_hash=packet.payload_hash,
                    details={**packet.payload(), "packet_hash": packet.packet_hash},
                )
                self._append_confirmed_approval_event(
                    issue_url=binding["issue_url"],
                    issue_id=map_id,
                    event=event,
                    completion={
                        "operation": "request",
                        "requested_by_profile": request_identity.profile_name,
                        "requested_by_session": request_identity.session_id,
                    },
                )
                stored = self._storage.approval(packet.request_id)
                if stored is None:  # pragma: no cover - SQLite contract guard
                    raise RuntimeError("Approval ledger did not persist the request")
                return {
                    "map_id": map_id,
                    "approval": self._approval_projection(stored),
                    "idempotent": False,
                }

    def decide_approval(
        self,
        *,
        map_id: str,
        request_id: str,
        actor_identity: GovernanceActorIdentity,
        decision: str,
        note: str,
    ) -> dict[str, Any]:
        """Record an explicit chairman approve/reject/revision decision."""
        self._authorize_chairman_request(
            map_id=map_id,
            action=f"approval:{decision}",
            actor_identity=actor_identity,
        )
        if decision not in APPROVAL_DECISIONS:
            raise ValueError("decision must be approved, rejected, or revision")
        if not isinstance(note, str) or not note.strip():
            raise ValueError("approval decision note must be a non-empty string")
        lock_key = f"{self._storage.database}:{map_id}"
        with _operation_lock("approval", lock_key):
            with self._storage.approval_lease(map_id):
                approval = self._current_approval(request_id=request_id)
                if approval is None or approval["map_id"] != map_id:
                    self._deny_approval_enforcement(
                        map_id=map_id,
                        action=f"approval:{decision}",
                        reason="approval_missing",
                        actor_identity=actor_identity,
                    )
                if approval["status"] == decision:
                    if (
                        approval["decided_by"] == actor_identity.actor_id
                        and approval["decided_by_profile"]
                        == actor_identity.profile_name
                        and approval["decision_note"] == note.strip()
                    ):
                        return {
                            "map_id": map_id,
                            "approval": self._approval_projection(approval),
                            "idempotent": True,
                        }
                    raise ApprovalRequestConflict(
                        request_id=request_id,
                        reason="chairman decision identity has different content",
                    )
                if approval["status"] != "pending":
                    self._deny_approval_enforcement(
                        map_id=map_id,
                        action=f"approval:{decision}",
                        reason=self._approval_status_reason(approval["status"]),
                        actor_identity=actor_identity,
                    )

                decided_at = self._synchronized_at()
                expires_at = (
                    self._format_datetime(
                        self._current_datetime() + self._authority_policy.approval_ttl
                    )
                    if decision == "approved"
                    else None
                )
                event = ApprovalHistoryEvent(
                    event_id=f"approval:{request_id}:decision",
                    request_id=request_id,
                    event_type=decision,
                    occurred_at=decided_at,
                    payload_hash=approval["payload_hash"],
                    details={
                        "decision": decision,
                        "actor_id": actor_identity.actor_id,
                        "actor_profile": actor_identity.profile_name,
                        "note": note.strip(),
                        "expires_at": expires_at,
                    },
                )
                binding = self._storage.map_binding(map_id)
                if binding is None:
                    raise MapBindingError(f"Map is not bound: {map_id}")
                self._append_confirmed_approval_event(
                    issue_url=binding["issue_url"],
                    issue_id=map_id,
                    event=event,
                    completion={"operation": "decision"},
                )
                stored = self._storage.approval(request_id)
                if stored is None:  # pragma: no cover - SQLite contract guard
                    raise RuntimeError("Approval ledger lost the decision")
                return {
                    "map_id": map_id,
                    "approval": self._approval_projection(stored),
                    "idempotent": False,
                }

    def revoke_approval(
        self,
        *,
        map_id: str,
        request_id: str,
        actor_identity: GovernanceActorIdentity,
        note: str,
    ) -> dict[str, Any]:
        """Revoke an unconsumed chairman approval through the application seam."""
        self._authorize_chairman_request(
            map_id=map_id,
            action="approval:revoke",
            actor_identity=actor_identity,
        )
        if not isinstance(note, str) or not note.strip():
            raise ValueError("approval revocation note must be a non-empty string")
        lock_key = f"{self._storage.database}:{map_id}"
        with _operation_lock("approval", lock_key):
            with self._storage.approval_lease(map_id):
                approval = self._current_approval(request_id=request_id)
                if approval is None or approval["map_id"] != map_id:
                    self._deny_approval_enforcement(
                        map_id=map_id,
                        action="approval:revoke",
                        reason="approval_missing",
                        actor_identity=actor_identity,
                    )
                if approval["status"] == "revoked":
                    if (
                        approval["decided_by"] == actor_identity.actor_id
                        and approval["decision_note"] == note.strip()
                    ):
                        return {
                            "map_id": map_id,
                            "approval": self._approval_projection(approval),
                            "idempotent": True,
                        }
                    raise ApprovalRequestConflict(
                        request_id=request_id,
                        reason="revocation identity has different content",
                    )
                if approval["status"] != "approved":
                    self._deny_approval_enforcement(
                        map_id=map_id,
                        action="approval:revoke",
                        reason=self._approval_status_reason(approval["status"]),
                        actor_identity=actor_identity,
                    )
                revoked_at = self._synchronized_at()
                event = ApprovalHistoryEvent(
                    event_id=f"approval:{request_id}:revocation",
                    request_id=request_id,
                    event_type="revoked",
                    occurred_at=revoked_at,
                    payload_hash=approval["payload_hash"],
                    details={
                        "actor_id": actor_identity.actor_id,
                        "actor_profile": actor_identity.profile_name,
                        "note": note.strip(),
                    },
                )
                binding = self._storage.map_binding(map_id)
                if binding is None:
                    raise MapBindingError(f"Map is not bound: {map_id}")
                self._append_confirmed_approval_event(
                    issue_url=binding["issue_url"],
                    issue_id=map_id,
                    event=event,
                    completion={"operation": "revocation"},
                )
                stored = self._storage.approval(request_id)
                if stored is None:  # pragma: no cover
                    raise RuntimeError("Approval ledger lost the revocation")
                return {
                    "map_id": map_id,
                    "approval": self._approval_projection(stored),
                    "idempotent": False,
                }

    def _append_confirmed_approval_event(
        self,
        *,
        issue_url: str,
        issue_id: str,
        event: ApprovalHistoryEvent,
        completion: dict[str, Any],
    ) -> TrackerApprovalRecord:
        effect_id = f"tracker-approval:{issue_id}:{event.event_id}"
        existing_intent = self._outbox.intent(effect_id)
        if existing_intent is not None:
            persisted_event_payload = existing_intent.payload.get("event")
            if not isinstance(persisted_event_payload, dict):
                raise ApprovalRequestConflict(
                    request_id=event.request_id,
                    reason="durable tracker event has malformed content",
                )
            persisted_event = ApprovalHistoryEvent(**persisted_event_payload)
            if not approval_events_semantically_compatible(persisted_event, event):
                raise ApprovalRequestConflict(
                    request_id=event.request_id,
                    reason="tracker event identity has different content",
                )
            event = persisted_event
        try:
            enqueued = self._outbox.enqueue(
                effect_id=effect_id,
                effect_type=TRACKER_APPROVAL_EVENT,
                map_id=issue_id,
                payload={
                    "issue_url": issue_url,
                    "issue_id": issue_id,
                    "event": event.payload(),
                    "completion": completion,
                },
                created_at=self._synchronized_at(),
            )
        except OutboxConflictError as error:
            raise ApprovalRequestConflict(
                request_id=event.request_id,
                reason="tracker event identity has different content",
            ) from error
        self._outbox_dispatcher.dispatch_effect(
            effect_id=effect_id,
            owner_id=self._outbox_owner_id,
            expedite_retry=not enqueued.created,
        )
        status = self._outbox.intent(effect_id)
        if status is None:  # pragma: no cover - durable enqueue contract guard
            raise RuntimeError("Approval Outbox intent disappeared")
        if status.state != "succeeded":
            if status.last_error_type == TrackerEffectPayloadConflict.__name__:
                raise ApprovalRequestConflict(
                    request_id=event.request_id,
                    reason="tracker event identity has different content",
                )
            raise TrackerApprovalConfirmationError(
                status.last_error_message
                or "Tracker did not confirm the approval event in Issue history"
            )
        acknowledgment = status.acknowledgment or {}
        confirmed_event = acknowledgment.get("event")
        if not isinstance(confirmed_event, dict):
            raise TrackerApprovalConfirmationError(
                "Tracker approval acknowledgment has no event payload"
            )
        return TrackerApprovalRecord(
            event=ApprovalHistoryEvent(**confirmed_event),
            tracker_record_id=str(acknowledgment["tracker_record_id"]),
            tracker_record_url=str(acknowledgment["tracker_record_url"]),
        )

    @staticmethod
    def _approval_records_by_event_id(
        records: list[TrackerApprovalRecord],
    ) -> dict[str, TrackerApprovalRecord]:
        by_id: dict[str, TrackerApprovalRecord] = {}
        for record in records:
            existing = by_id.get(record.event.event_id)
            if existing is not None and existing.event != record.event:
                raise ApprovalRequestConflict(
                    request_id=record.event.request_id,
                    reason="tracker event identity has conflicting history",
                )
            by_id.setdefault(record.event.event_id, record)
        return by_id

    def _authorize_chairman_request(
        self,
        *,
        map_id: str,
        action: str,
        actor_identity: GovernanceActorIdentity,
    ) -> None:
        binding = self._storage.ceo_session_binding(map_id)
        if binding is None:
            self._deny_governance_actor(
                map_id=map_id,
                action=action,
                actor_identity=actor_identity,
                reason="canonical_session_missing",
            )
        if actor_identity.role != "chairman":
            self._deny_governance_actor(
                map_id=map_id,
                action=action,
                actor_identity=actor_identity,
                reason="chairman_actor_required",
            )
        if not actor_identity.actor_id:
            self._deny_governance_actor(
                map_id=map_id,
                action=action,
                actor_identity=actor_identity,
                reason="chairman_actor_missing",
            )
        if actor_identity.profile_name != binding["profile_name"]:
            self._deny_governance_actor(
                map_id=map_id,
                action=action,
                actor_identity=actor_identity,
                reason="profile_mismatch",
            )
        if not self._authority_policy.authorizes_chairman_actor(
            actor_identity.actor_id
        ):
            self._deny_governance_actor(
                map_id=map_id,
                action=action,
                actor_identity=actor_identity,
                reason="chairman_actor_not_authorized",
            )

    def _deny_governance_actor(
        self,
        *,
        map_id: str,
        action: str,
        actor_identity: GovernanceActorIdentity,
        reason: str,
    ) -> NoReturn:
        self._storage.save_authorization_denial(
            action=action,
            map_id=map_id,
            profile_name=actor_identity.profile_name,
            session_id=actor_identity.session_id,
            reason=reason,
            denied_at=self._synchronized_at(),
        )
        raise GovernanceAuthorizationError(action=action, map_id=map_id, reason=reason)

    def _deny_approval_enforcement(
        self,
        *,
        map_id: str,
        action: str,
        reason: str,
        actor_identity: GovernanceActorIdentity | None = None,
        profile_name: str = "",
        session_id: str = "",
    ) -> NoReturn:
        if actor_identity is not None:
            profile_name = actor_identity.profile_name
            session_id = actor_identity.session_id
        self._storage.save_authorization_denial(
            action=action,
            map_id=map_id,
            profile_name=profile_name,
            session_id=session_id,
            reason=reason,
            denied_at=self._synchronized_at(),
        )
        raise ApprovalEnforcementError(action=action, map_id=map_id, reason=reason)

    def _current_approval(self, *, request_id: str) -> dict[str, Any] | None:
        approval = self._storage.approval(request_id)
        if approval is None:
            return None
        if approval["status"] == "approved" and approval.get("expires_at"):
            expires_at = datetime.fromisoformat(
                str(approval["expires_at"]).replace("Z", "+00:00")
            )
            if expires_at <= self._current_datetime():
                self._storage.expire_approval(
                    request_id=request_id,
                    expired_at=self._synchronized_at(),
                )
                approval = self._storage.approval(request_id)
        return approval

    def _approval_collection(self, *, map_id: str) -> dict[str, Any]:
        approvals = []
        for row in self._storage.approvals(map_id=map_id):
            current = self._current_approval(request_id=row["request_id"])
            if current is not None:
                approvals.append(self._approval_projection(current))
        return {"count": len(approvals), "items": approvals}

    def _approval_projection(self, row: dict[str, Any]) -> dict[str, Any]:
        decision = None
        if row.get("decided_by"):
            decision = {
                "actor_id": row["decided_by"],
                "actor_profile": row["decided_by_profile"],
                "note": row["decision_note"],
                "decided_at": row["decided_at"],
            }
        consumption = None
        if row.get("consumed_by_mutation_id"):
            consumption = {
                "mutation_id": row["consumed_by_mutation_id"],
                "consumed_at": row["consumed_at"],
            }
        history = []
        for event in self._storage.approval_history(request_id=row["request_id"]):
            history.append(
                {
                    "event_id": event["event_id"],
                    "event_type": event["event_type"],
                    "occurred_at": event["occurred_at"],
                    "actor_id": event["actor_id"],
                    "actor_profile": event["actor_profile"],
                    "note": event["note"],
                    "expires_at": event["expires_at"],
                    "payload_hash": event["payload_hash"],
                    "tracker": (
                        {
                            "id": event["tracker_record_id"],
                            "url": event["tracker_record_url"],
                        }
                        if event["tracker_record_id"]
                        else None
                    ),
                }
            )
        return {
            "request_id": row["request_id"],
            "decision_class": row["decision_class"],
            "proposed_action": row["proposed_action"],
            "alternatives": row["alternatives"],
            "rationale": row["rationale"],
            "cost_risk": row["cost_risk"],
            "evidence": row["evidence"],
            "requested_scope": row["requested_scope"],
            "decision_payload": row["decision_payload"],
            "payload_hash": row["payload_hash"],
            "status": row["status"],
            "requested_at": row["requested_at"],
            "expires_at": row["expires_at"],
            "decision": decision,
            "consumption": consumption,
            "history": history,
            "tracker": {
                "request": {
                    "id": row["tracker_request_id"],
                    "url": row["tracker_request_url"],
                },
                "decision": (
                    {
                        "id": row["tracker_decision_id"],
                        "url": row["tracker_decision_url"],
                    }
                    if row.get("tracker_decision_id")
                    else None
                ),
            },
        }

    @staticmethod
    def _approval_status_reason(status: str) -> str:
        return {
            "pending": "approval_pending",
            "approved": "approval_already_approved",
            "rejected": "approval_rejected",
            "revision": "approval_revision_required",
            "revoked": "approval_revoked",
            "expired": "approval_expired",
            "consumed": "approval_consumed",
        }.get(status, "approval_invalid")

    def _authorize_ceo_request(
        self,
        *,
        map_id: str,
        action: str,
        request_identity: GovernanceRequestIdentity,
    ) -> None:
        binding = self._storage.ceo_session_binding(map_id)
        if binding is None:
            self._deny_governance_request(
                map_id=map_id,
                action=action,
                request_identity=request_identity,
                reason="canonical_session_missing",
            )
        if request_identity.profile_name != binding["profile_name"]:
            self._deny_governance_request(
                map_id=map_id,
                action=action,
                request_identity=request_identity,
                reason="profile_mismatch",
            )
        session_ids = {
            binding.get("root_session_id"),
            binding.get("live_session_id"),
        }
        if request_identity.session_id in session_ids:
            return
        if self._session_runner is not None and binding.get("root_session_id"):
            resolved = self._session_runner.resolve(
                root_session_id=binding["root_session_id"]
            )
            if resolved is not None and request_identity.session_id in {
                resolved.root_session_id,
                resolved.live_session_id,
            }:
                return
        self._deny_governance_request(
            map_id=map_id,
            action=action,
            request_identity=request_identity,
            reason="session_mismatch",
        )

    def enforce_canonical_ceo_toolset(
        self,
        *,
        request_identity: GovernanceRequestIdentity,
        tool_name: str,
        allowed_tool_names: frozenset[str],
    ) -> bool:
        """Block non-governance tools only inside a canonical CEO session."""
        map_id = self._canonical_ceo_map(request_identity=request_identity)
        if map_id is None:
            return False
        if tool_name in allowed_tool_names:
            return True
        self._deny_governance_request(
            map_id=map_id,
            action=f"invoke_tool:{tool_name}",
            request_identity=request_identity,
            reason="tool_outside_ceo_toolset",
        )

    def _canonical_ceo_map(
        self,
        *,
        request_identity: GovernanceRequestIdentity,
    ) -> str | None:
        if not request_identity.session_id:
            return None
        for binding in self._storage.ceo_session_bindings(
            profile_name=request_identity.profile_name
        ):
            if request_identity.session_id in {
                binding.get("root_session_id"),
                binding.get("live_session_id"),
            }:
                return str(binding["map_id"])
            if self._session_runner is None or not binding.get("root_session_id"):
                continue
            resolved = self._session_runner.resolve(
                root_session_id=str(binding["root_session_id"])
            )
            if resolved is not None and request_identity.session_id in {
                resolved.root_session_id,
                resolved.live_session_id,
            }:
                return str(binding["map_id"])
        return None

    def _deny_governance_request(
        self,
        *,
        map_id: str,
        action: str,
        request_identity: GovernanceRequestIdentity,
        reason: str,
    ) -> NoReturn:
        self._storage.save_authorization_denial(
            action=action,
            map_id=map_id,
            profile_name=request_identity.profile_name,
            session_id=request_identity.session_id,
            reason=reason,
            denied_at=self._synchronized_at(),
        )
        raise GovernanceAuthorizationError(
            action=action,
            map_id=map_id,
            reason=reason,
        )

    def authorization_denials(
        self,
        *,
        map_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the durable security audit without exposing it to CEO tools."""
        return self._storage.authorization_denials(map_id=map_id)

    def open_map(self, *, map_id: str) -> dict[str, Any]:
        """Resolve, initialize and return the Map's one canonical CEO session."""
        lock_key = f"{self._storage.database}:{map_id}"
        with _operation_lock("session", lock_key):
            with self._storage.ceo_session_lease(map_id):
                return self._open_map(map_id=map_id)

    def _open_map(self, *, map_id: str) -> dict[str, Any]:
        if self._session_runner is None or not self._profile_name:
            raise MapBindingError(
                "Opening a CEO session requires an explicit Hermes profile"
            )
        context = self._storage.map_session_context(map_id)
        if context is None:
            raise MapBindingError(f"Map is not bound: {map_id}")

        identity = canonical_session_identity(
            profile_name=self._profile_name,
            map_id=map_id,
        )
        title = canonical_session_title(map_id)
        bootstrap = self._ceo_bootstrap(context, canonical_identity=identity)
        bootstrap_hash = hashlib.sha256(bootstrap.encode()).hexdigest()
        idempotency_key = f"{identity}:bootstrap:{bootstrap_hash}"
        existing_binding = self._storage.ceo_session_binding(map_id)

        if existing_binding is not None:
            if existing_binding["profile_name"] != self._profile_name:
                return self._repair_required(
                    map_id=map_id,
                    identity=identity,
                    title=title,
                    reason="canonical_profile_mismatch",
                )
            root_session_id = existing_binding["root_session_id"]
            if root_session_id:
                resolved = self._session_runner.resolve(
                    root_session_id=root_session_id,
                )
                if resolved is not None and resolved.title == title:
                    return self._ready_session(
                        map_id=map_id,
                        identity=identity,
                        title=title,
                        session=resolved,
                        bootstrap_hash=bootstrap_hash,
                    )

            # Repair state is a projection, not a permanent gate. Reconcile on
            # every open so a restored backend or manually repaired ambiguity
            # can recover without rewriting or forking decision history.
            recovered = self._session_runner.find_exact(title=title)
            if len(recovered) > 1:
                return self._repair_required(
                    map_id=map_id,
                    identity=identity,
                    title=title,
                    reason="multiple_exact_canonical_sessions",
                    candidate_count=len(recovered),
                    ambiguity=True,
                )
            if recovered:
                initialized = self._session_runner.initialize(
                    recovered[0],
                    bootstrap=bootstrap,
                    idempotency_key=idempotency_key,
                )
                return self._ready_session(
                    map_id=map_id,
                    identity=identity,
                    title=title,
                    session=initialized,
                    bootstrap_hash=bootstrap_hash,
                )
            return self._repair_required(
                map_id=map_id,
                identity=identity,
                title=title,
                reason="recorded_session_lineage_missing_or_mismatched",
            )

        matches = self._session_runner.find_exact(title=title)
        if len(matches) > 1:
            return self._repair_required(
                map_id=map_id,
                identity=identity,
                title=title,
                reason="multiple_exact_canonical_sessions",
                candidate_count=len(matches),
                ambiguity=True,
            )
        if matches:
            session = self._session_runner.initialize(
                matches[0],
                bootstrap=bootstrap,
                idempotency_key=idempotency_key,
            )
        else:
            try:
                session = self._session_runner.mint(
                    identity=identity,
                    title=title,
                    profile_name=self._profile_name,
                    bootstrap=bootstrap,
                    idempotency_key=idempotency_key,
                )
            except ValueError:
                # A separate process may have won the exact-title race between
                # lookup and mint. Re-read the registry; never suffix or guess.
                converged = self._session_runner.find_exact(title=title)
                if len(converged) != 1:
                    return self._repair_required(
                        map_id=map_id,
                        identity=identity,
                        title=title,
                        reason="concurrent_canonical_session_conflict",
                        candidate_count=len(converged),
                        ambiguity=len(converged) > 1,
                    )
                session = self._session_runner.initialize(
                    converged[0],
                    bootstrap=bootstrap,
                    idempotency_key=idempotency_key,
                )

        exact_after_initialization = self._session_runner.find_exact(title=title)
        if len(exact_after_initialization) != 1:
            return self._repair_required(
                map_id=map_id,
                identity=identity,
                title=title,
                reason="canonical_session_did_not_converge",
                candidate_count=len(exact_after_initialization),
                ambiguity=len(exact_after_initialization) > 1,
            )
        return self._ready_session(
            map_id=map_id,
            identity=identity,
            title=title,
            session=session,
            bootstrap_hash=bootstrap_hash,
        )

    def _ready_session(
        self,
        *,
        map_id: str,
        identity: str,
        title: str,
        session: CanonicalSession,
        bootstrap_hash: str,
    ) -> dict[str, Any]:
        if self._session_runner is None:
            raise RuntimeError("CEO session runner is unavailable")
        skill_content = self._ceo_skill_message()
        skill_hash = hashlib.sha256(skill_content.encode()).hexdigest()
        idempotency_key = f"{identity}:skill:{skill_hash}"
        if self._session_resume_outbox:
            tip_hash = hashlib.sha256(session.live_session_id.encode()).hexdigest()[:16]
            effect_id = f"session-resume:{map_id}:ceo-skill:{skill_hash}:{tip_hash}"
            effect_payload = {
                "root_session_id": session.root_session_id,
                "content": skill_content,
                "idempotency_key": idempotency_key,
                "profile_name": self._profile_name or "",
                "canonical_identity": identity,
                "canonical_title": title,
                "bootstrap_hash": bootstrap_hash,
            }
            existing_intent = self._outbox.intent(effect_id)
            if existing_intent is not None:
                stable_fields = {
                    "root_session_id",
                    "content",
                    "idempotency_key",
                    "profile_name",
                    "canonical_identity",
                    "canonical_title",
                }
                if any(
                    existing_intent.payload.get(name) != effect_payload.get(name)
                    for name in stable_fields
                ):
                    raise CEOSessionRepairRequired(
                        reason="session_resume_identity_conflict"
                    )
                effect_payload = existing_intent.payload
            try:
                enqueued = self._outbox.enqueue(
                    effect_id=effect_id,
                    effect_type=SESSION_RESUME,
                    map_id=map_id,
                    payload=effect_payload,
                    created_at=self._synchronized_at(),
                )
            except OutboxConflictError as error:
                raise CEOSessionRepairRequired(
                    reason="session_resume_identity_conflict"
                ) from error
            self._outbox_dispatcher.dispatch_effect(
                effect_id=effect_id,
                owner_id=self._outbox_owner_id,
                expedite_retry=not enqueued.created,
            )
            status = self._outbox.intent(effect_id)
            if status is None:  # pragma: no cover
                raise RuntimeError("Session resume Outbox intent disappeared")
            if status.state != "succeeded":
                raise RuntimeError(
                    status.last_error_message
                    or status.terminal_reason
                    or "Hermes session resume is pending"
                )
            refreshed = self._session_runner.resolve(
                root_session_id=session.root_session_id
            )
            if refreshed is None:
                raise RuntimeError("Hermes session disappeared after durable resume")
            self._storage.save_ceo_session_ready(
                map_id=map_id,
                profile_name=self._profile_name or "",
                canonical_identity=identity,
                canonical_title=title,
                root_session_id=refreshed.root_session_id,
                live_session_id=refreshed.live_session_id,
                last_activity_at=refreshed.last_activity_at,
                bootstrap_hash=bootstrap_hash,
                updated_at=self._synchronized_at(),
            )
            return {
                "map_id": map_id,
                "ceo_session": {
                    "state": "ready",
                    "root_session_id": refreshed.root_session_id,
                    "live_session_id": refreshed.live_session_id,
                    "last_activity_at": refreshed.last_activity_at,
                },
            }
        session = self._session_runner.load_skill(
            session,
            content=skill_content,
            idempotency_key=idempotency_key,
        )
        self._storage.save_ceo_session_ready(
            map_id=map_id,
            profile_name=self._profile_name or "",
            canonical_identity=identity,
            canonical_title=title,
            root_session_id=session.root_session_id,
            live_session_id=session.live_session_id,
            last_activity_at=session.last_activity_at,
            bootstrap_hash=bootstrap_hash,
            updated_at=self._synchronized_at(),
        )
        return {
            "map_id": map_id,
            "ceo_session": {
                "state": "ready",
                "root_session_id": session.root_session_id,
                "live_session_id": session.live_session_id,
                "last_activity_at": session.last_activity_at,
            },
        }

    def _repair_required(
        self,
        *,
        map_id: str,
        identity: str,
        title: str,
        reason: str,
        candidate_count: int = 0,
        ambiguity: bool = False,
    ):
        self._storage.save_ceo_session_repair_required(
            map_id=map_id,
            profile_name=self._profile_name or "",
            canonical_identity=identity,
            canonical_title=title,
            reason=reason,
            candidate_count=candidate_count,
            updated_at=self._synchronized_at(),
        )
        error_type = CEOSessionAmbiguityError if ambiguity else CEOSessionRepairRequired
        raise error_type(reason=reason, candidate_count=candidate_count)

    def _ceo_bootstrap(
        self,
        context: dict[str, Any],
        *,
        canonical_identity: str,
    ) -> str:
        return "\n".join(
            (
                "Hermes Map Governance canonical CEO-session bootstrap.",
                f"Canonical identity: {canonical_identity}",
                f"CEO profile: {self._profile_name}",
                f"Map Issue: {context['issue_url']}",
                (
                    "Tracker identity: "
                    f"{context['repository']}#{context['issue_number']}"
                ),
                f"Map title at binding: {context['title']}",
                f"Executive stage at binding: {context['stage']}",
                "The initialization sequence loads map-governance:ceo next.",
                (
                    "Authority envelope: govern product and operations only "
                    "within the Map's authorized bounds."
                ),
                (
                    "Escalate chairman-required budget, scope, schedule, "
                    "cancellation, publication, and acceptance actions; never "
                    "self-approve them."
                ),
                (
                    "Treat this user turn as the durable Map context. Re-read "
                    "tracker truth for later board changes."
                ),
            )
        )

    def _ceo_skill_message(self) -> str:
        skill_path = self._plugin_root / "skills" / "ceo" / "SKILL.md"
        try:
            skill = skill_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise RuntimeError("Map Governance CEO Skill is unavailable") from error
        if not skill:
            raise RuntimeError("Map Governance CEO Skill is empty")
        return "\n".join(
            (
                "Loaded Skill: map-governance:ceo",
                "<map-governance-ceo-skill>",
                skill,
                "</map-governance-ceo-skill>",
            )
        )

    def configure_project(self, *, project_url: str) -> dict[str, Any]:
        """Configure one existing GitHub Project as a CEO project."""
        project = self._tracker.get_project(project_url)
        synchronized_at = self._synchronized_at()
        stored = self._stored_project(project)
        self._storage.save_project_binding(stored)
        self._storage.save_project_projection(
            project=stored,
            synchronized_at=synchronized_at,
        )
        return self._configured_project(project, synchronized_at=synchronized_at)

    def bind_map(self, *, project_id: str, issue_url: str) -> dict[str, Any]:
        """Bind an existing tracker Issue as exactly one Map card."""
        project_binding = self._storage.project_binding(project_id)
        if project_binding is None:
            raise MapBindingError(f"CEO project is not configured: {project_id}")
        issue = self._tracker.get_issue(issue_url)
        synchronized_at = self._synchronized_at()
        card = self._stored_card(
            issue,
            project_id=project_binding["project_id"],
            synchronized_at=synchronized_at,
        )
        try:
            self._storage.save_map_binding(
                map_id=issue.id,
                project_id=project_binding["project_id"],
                issue_url=issue.url,
            )
        except ValueError as error:
            raise MapBindingError(str(error)) from error
        self._storage.save_map_projection(card)
        self._rebuild_decision_projection(
            map_id=issue.id,
            issue_url=issue.url,
            confirmed_at=synchronized_at,
        )
        self._rebuild_pm_report_projection(
            map_id=issue.id,
            issue_url=issue.url,
            confirmed_at=synchronized_at,
        )
        return self._card_with_summary(
            card,
            project_url=project_binding["project_url"],
        )

    def refresh(self, *, project_id: str | None = None) -> dict[str, Any]:
        """Rebuild selected board projections from tracker truth and bindings."""
        bindings = self._storage.project_bindings(project_id)
        if project_id is not None and not bindings:
            raise MapBindingError(f"CEO project is not configured: {project_id}")
        for project_binding in bindings:
            project = self._tracker.get_project(project_binding["project_url"])
            if project.id != project_binding["project_id"]:
                raise MapBindingError("Configured GitHub Project identity changed")
            synchronized_at = self._synchronized_at()
            self._storage.save_project_projection(
                project=self._stored_project(project),
                synchronized_at=synchronized_at,
            )
            for map_binding in self._storage.map_bindings(project.id):
                issue = self._tracker.get_issue(map_binding["issue_url"])
                if issue.id != map_binding["map_id"]:
                    raise MapBindingError("Bound GitHub Issue identity changed")
                self._storage.save_map_projection(
                    self._stored_card(
                        issue,
                        project_id=project.id,
                        synchronized_at=synchronized_at,
                    )
                )
                self._rebuild_decision_projection(
                    map_id=issue.id,
                    issue_url=issue.url,
                    confirmed_at=synchronized_at,
                )
                self._rebuild_pm_report_projection(
                    map_id=issue.id,
                    issue_url=issue.url,
                    confirmed_at=synchronized_at,
                )
        return self.board()

    def _rebuild_decision_projection(
        self,
        *,
        map_id: str,
        issue_url: str,
        confirmed_at: str,
    ) -> None:
        records = self._decision_records_by_id(self._tracker.list_decisions(issue_url))
        decisions = [
            self._decision_projection(record, confirmed_at=confirmed_at)
            for record in records.values()
        ]
        self._storage.replace_decision_projections(
            map_id=map_id,
            decisions=decisions,
        )

    def _rebuild_pm_report_projection(
        self,
        *,
        map_id: str,
        issue_url: str,
        confirmed_at: str,
    ) -> None:
        list_reports = getattr(self._tracker, "list_pm_reports", None)
        if list_reports is None:
            return
        records = self._pm_report_records_by_id(list_reports(issue_url))
        reports = []
        for record in records.values():
            if record.report.assignment_map_id != map_id:
                raise MapBindingError(
                    "Tracker PM report belongs to another assigned Map"
                )
            reports.append(
                self._pm_report_projection(record, confirmed_at=confirmed_at)
            )
        self._storage.replace_pm_report_projections(
            map_id=map_id,
            reports=reports,
        )

    def transition_map(
        self,
        *,
        map_id: str,
        expected_stage: str,
        requested_stage: str,
        approval_request_id: str | None = None,
        mutation_id: str | None = None,
        actor_identity: GovernanceActorIdentity | None = None,
    ) -> dict[str, Any]:
        """Commit a governed stage transition to the tracker, then project it."""
        lock_key = f"{self._storage.database}:{map_id}"
        with _operation_lock("transition", lock_key):
            if self._transition_requires_approval(requested_stage=requested_stage):
                with self._storage.approval_lease(map_id):
                    return self._transition_map(
                        map_id=map_id,
                        expected_stage=expected_stage,
                        requested_stage=requested_stage,
                        approval_request_id=approval_request_id,
                        mutation_id=mutation_id,
                        actor_identity=actor_identity,
                    )
            return self._transition_map(
                map_id=map_id,
                expected_stage=expected_stage,
                requested_stage=requested_stage,
                approval_request_id=approval_request_id,
                mutation_id=mutation_id,
                actor_identity=actor_identity,
            )

    def _transition_map(
        self,
        *,
        map_id: str,
        expected_stage: str,
        requested_stage: str,
        approval_request_id: str | None,
        mutation_id: str | None,
        actor_identity: GovernanceActorIdentity | None,
    ) -> dict[str, Any]:
        binding = self._storage.map_binding(map_id)
        if binding is None:
            raise MapBindingError(f"Map is not bound: {map_id}")
        project = self._storage.project_binding(binding["project_id"])
        if project is None:
            raise MapBindingError(
                f"CEO project is not configured: {binding['project_id']}"
            )

        protected = self._transition_requires_approval(requested_stage=requested_stage)
        action = "transition_map"
        scope = {"map_id": map_id}
        payload = {
            "expected_stage": expected_stage,
            "requested_stage": requested_stage,
        }
        payload_hash = normalized_hash(
            {"action": action, "scope": scope, "payload": payload}
        )
        existing_mutation = None
        approval = None
        if protected:
            if not approval_request_id:
                self._deny_approval_enforcement(
                    map_id=map_id,
                    action=action,
                    reason="approval_missing",
                    actor_identity=actor_identity,
                )
            if not mutation_id:
                self._deny_approval_enforcement(
                    map_id=map_id,
                    action=action,
                    reason="mutation_id_missing",
                    actor_identity=actor_identity,
                )
            if len(mutation_id) > 128:
                raise ValueError("mutation_id must not exceed 128 characters")
            existing_mutation = self._storage.protected_mutation(mutation_id)
            if existing_mutation is not None:
                if (
                    existing_mutation["map_id"] != map_id
                    or existing_mutation["request_id"] != approval_request_id
                    or existing_mutation["action"] != action
                    or normalized_json(existing_mutation["scope"])
                    != normalized_json(scope)
                    or normalized_json(existing_mutation["payload"])
                    != normalized_json(payload)
                    or existing_mutation["payload_hash"] != payload_hash
                ):
                    raise ApprovalRequestConflict(
                        request_id=approval_request_id,
                        reason="stable mutation identity belongs to another payload",
                    )
            if existing_mutation is None and self._approval_revocation_unfinished(
                map_id=map_id,
                request_id=approval_request_id,
            ):
                self._deny_approval_enforcement(
                    map_id=map_id,
                    action=action,
                    reason="approval_revocation_pending",
                    actor_identity=actor_identity,
                )
            approval = self._current_approval(request_id=approval_request_id)
            if approval is None or approval["map_id"] != map_id:
                self._deny_approval_enforcement(
                    map_id=map_id,
                    action=action,
                    reason="approval_missing",
                    actor_identity=actor_identity,
                )
            if existing_mutation is None:
                if approval["status"] != "approved":
                    self._deny_approval_enforcement(
                        map_id=map_id,
                        action=action,
                        reason=self._approval_status_reason(approval["status"]),
                        actor_identity=actor_identity,
                    )
                self._validate_approval_action(
                    map_id=map_id,
                    approval=approval,
                    decision_class=self._transition_decision_class(requested_stage),
                    action=action,
                    scope=scope,
                    payload=payload,
                    actor_identity=actor_identity,
                )
            elif approval["status"] != "consumed":
                self._deny_approval_enforcement(
                    map_id=map_id,
                    action=action,
                    reason=self._approval_status_reason(approval["status"]),
                    actor_identity=actor_identity,
                )

        current_issue = self._tracker.get_issue(binding["issue_url"])
        if current_issue.id != map_id:
            raise MapBindingError("Bound GitHub Issue identity changed")
        current_stage = self._executive_stage(current_issue)
        replaying_committed_transition = current_stage == requested_stage and (
            existing_mutation is not None or mutation_id is not None
        )
        if current_stage != expected_stage and not replaying_committed_transition:
            raise MapTransitionConflict(
                current_stage=current_stage,
                requested_stage=requested_stage,
                reason="tracker stage changed; refresh and retry",
            )
        if (
            not replaying_committed_transition
            and requested_stage not in ALLOWED_TRANSITIONS.get(current_stage, ())
        ):
            raise MapTransitionError(
                current_stage=current_stage,
                requested_stage=requested_stage,
                reason=rejection_reason(current_stage, requested_stage),
            )

        mutation_replay = existing_mutation is not None
        effect_payload = {
            "issue_url": binding["issue_url"],
            "issue_id": map_id,
            "project_id": binding["project_id"],
            "expected_stage": expected_stage,
            "requested_stage": requested_stage,
            "protected_mutation_id": mutation_id if protected else None,
        }
        effect_id_base = self._stage_effect_id(
            map_id=map_id,
            expected_stage=expected_stage,
            requested_stage=requested_stage,
            mutation_id=mutation_id,
        )
        intent_created_at = self._synchronized_at()
        try:
            if protected:
                with self._storage.atomic() as connection:
                    mutation_replay, reservation_status = (
                        self._storage.reserve_protected_mutation_in_transaction(
                            connection,
                            mutation_id=mutation_id or "",
                            map_id=map_id,
                            request_id=approval_request_id or "",
                            action=action,
                            scope=scope,
                            payload=payload,
                            payload_hash=payload_hash,
                            reserved_at=intent_created_at,
                        )
                    )
                    if reservation_status not in {"reserved", "confirmed"}:
                        raise RuntimeError(
                            "Approved mutation changed while holding its Map lease"
                        )
                    enqueued = self._outbox.enqueue_in_transaction(
                        connection,
                        effect_id=effect_id_base,
                        effect_type=TRACKER_STAGE_TRANSITION,
                        map_id=map_id,
                        payload=effect_payload,
                        created_at=intent_created_at,
                    )
            elif mutation_id is None:
                enqueued = self._outbox.enqueue_occurrence(
                    effect_id_base=effect_id_base,
                    effect_type=TRACKER_STAGE_TRANSITION,
                    map_id=map_id,
                    payload=effect_payload,
                    created_at=intent_created_at,
                )
            else:
                enqueued = self._outbox.enqueue(
                    effect_id=effect_id_base,
                    effect_type=TRACKER_STAGE_TRANSITION,
                    map_id=map_id,
                    payload=effect_payload,
                    created_at=intent_created_at,
                )
        except OutboxConflictError as error:
            if protected:
                raise ApprovalRequestConflict(
                    request_id=approval_request_id or "",
                    reason="stable mutation identity belongs to another payload",
                ) from error
            raise MapTransitionError(
                current_stage=current_stage,
                requested_stage=requested_stage,
                reason="stable transition identity belongs to another payload",
            ) from error
        except ValueError as error:
            raise ApprovalRequestConflict(
                request_id=approval_request_id or "",
                reason=str(error),
            ) from error
        effect_id = enqueued.intent.effect_id
        self._outbox_dispatcher.dispatch_effect(
            effect_id=effect_id,
            owner_id=self._outbox_owner_id,
            expedite_retry=not enqueued.created,
        )
        status = self._outbox.intent(effect_id)
        if status is None:  # pragma: no cover - durable enqueue contract guard
            raise RuntimeError("Stage-transition Outbox intent disappeared")
        if status.state != "succeeded":
            if status.state == "terminal":
                observed = self._tracker.get_issue(binding["issue_url"])
                raise MapTransitionConflict(
                    current_stage=self._executive_stage(observed),
                    requested_stage=requested_stage,
                    reason=(
                        "tracker stage changed; refresh and retry"
                        if status.last_error_type == TrackerStageEffectConflict.__name__
                        else status.terminal_reason or "tracker rejected transition"
                    ),
                )
            reason = status.last_error_message or (
                f"stage transition is {status.state}"
            )
            raise TrackerError(reason)
        result = next(card for card in self.board()["maps"] if card["id"] == map_id)
        result["external_effect"] = {
            "effect_id": effect_id,
            "state": status.state,
            "attempt_count": status.attempt_count,
            "acknowledged_at": status.acknowledged_at,
        }
        if protected:
            result["protected_mutation"] = {
                "mutation_id": mutation_id,
                "approval_request_id": approval_request_id,
                "idempotent": mutation_replay or not enqueued.created,
            }
        return result

    def _approval_revocation_unfinished(
        self,
        *,
        map_id: str,
        request_id: str,
    ) -> bool:
        for intent in self._outbox.unfinished_intents(
            map_id=map_id,
            effect_type=TRACKER_APPROVAL_EVENT,
        ):
            event = intent.payload.get("event")
            if (
                isinstance(event, dict)
                and event.get("request_id") == request_id
                and event.get("event_type") == "revoked"
            ):
                return True
        return False

    @staticmethod
    def _stage_effect_id(
        *,
        map_id: str,
        expected_stage: str,
        requested_stage: str,
        mutation_id: str | None,
    ) -> str:
        if mutation_id:
            return f"stage-transition:{mutation_id}"
        digest = hashlib.sha256(
            normalized_json(
                {
                    "map_id": map_id,
                    "expected_stage": expected_stage,
                    "requested_stage": requested_stage,
                }
            ).encode()
        ).hexdigest()[:32]
        return f"stage-transition:{digest}"

    def _complete_external_effect(
        self,
        intent: OutboxIntent,
        confirmation: EffectConfirmation,
    ) -> None:
        if intent.effect_type == COORDINATOR_RESUME:
            try:
                self._storage.begin_pm_turn(
                    map_id=intent.map_id,
                    coordinator_id=str(intent.payload["coordinator_id"]),
                    turn_id=str(intent.payload["turn_id"]),
                    started_at=self._synchronized_at(),
                )
            except ValueError as error:
                raise EffectTerminalError(str(error)) from error
            return
        if intent.effect_type == SESSION_RESUME:
            session_payload = confirmation.acknowledgment.get("session")
            if not isinstance(session_payload, dict):
                raise EffectRetryableError(
                    "Hermes resume confirmation has no session payload"
                )
            self._storage.save_ceo_session_ready(
                map_id=intent.map_id,
                profile_name=str(intent.payload["profile_name"]),
                canonical_identity=str(intent.payload["canonical_identity"]),
                canonical_title=str(intent.payload["canonical_title"]),
                root_session_id=str(session_payload["root_session_id"]),
                live_session_id=str(session_payload["live_session_id"]),
                last_activity_at=(
                    str(session_payload["last_activity_at"])
                    if session_payload.get("last_activity_at") is not None
                    else None
                ),
                bootstrap_hash=str(intent.payload["bootstrap_hash"]),
                updated_at=self._synchronized_at(),
            )
            return
        if intent.effect_type == TRACKER_PM_REPORT:
            self._complete_pm_report_effect(intent, confirmation)
            return
        if intent.effect_type == TRACKER_APPROVAL_EVENT:
            self._complete_approval_effect(intent, confirmation)
            return
        if intent.effect_type == TRACKER_DECISION:
            acknowledgment = confirmation.acknowledgment
            decision_payload = acknowledgment.get("decision")
            if not isinstance(decision_payload, dict):
                raise EffectRetryableError(
                    "Tracker decision confirmation has no decision payload"
                )
            record = TrackerDecisionRecord(
                decision=StructuredDecision(**decision_payload),
                tracker_record_id=str(acknowledgment["tracker_record_id"]),
                tracker_record_url=str(acknowledgment["tracker_record_url"]),
            )
            self._storage.save_decision_projection(
                map_id=intent.map_id,
                decision=self._decision_projection(
                    record,
                    confirmed_at=self._synchronized_at(),
                ),
            )
            return
        if intent.effect_type != TRACKER_STAGE_TRANSITION:
            raise EffectRetryableError(
                f"No local completion is configured for {intent.effect_type}"
            )
        issue_payload = confirmation.acknowledgment.get("issue")
        if not isinstance(issue_payload, dict):
            raise EffectRetryableError(
                "Tracker stage confirmation has no authoritative Issue payload"
            )
        issue = TrackerEffectAdapter.issue_from_payload(issue_payload)
        synchronized_at = self._synchronized_at()
        card = self._stored_card(
            issue,
            project_id=str(intent.payload["project_id"]),
            synchronized_at=synchronized_at,
        )
        competing_stage = self._storage.compare_and_save_map_projection(
            card,
            expected_stage=str(intent.payload["expected_stage"]),
        )
        if competing_stage is not None and competing_stage != card["stage"]:
            raise EffectRetryableError(
                "local projection changed after tracker confirmation"
            )
        protected_mutation_id = intent.payload.get("protected_mutation_id")
        if protected_mutation_id:
            self._storage.confirm_protected_mutation(
                mutation_id=str(protected_mutation_id),
                confirmed_at=synchronized_at,
            )
        pm_report_completion = intent.payload.get("pm_report_completion")
        if isinstance(pm_report_completion, dict):
            self._finalize_pm_report(
                map_id=intent.map_id,
                completion=pm_report_completion,
            )

    def _complete_pm_report_effect(
        self,
        intent: OutboxIntent,
        confirmation: EffectConfirmation,
    ) -> None:
        acknowledgment = confirmation.acknowledgment
        report_payload = acknowledgment.get("report")
        if not isinstance(report_payload, dict):
            raise EffectRetryableError("Tracker PM confirmation has no report payload")
        requested_stage = intent.payload.get("requested_stage")
        completion = {
            "report": report_payload,
            "tracker_record_id": str(acknowledgment["tracker_record_id"]),
            "tracker_record_url": str(acknowledgment["tracker_record_url"]),
            "active_turn_id": str(intent.payload["active_turn_id"]),
        }
        if requested_stage is None:
            self._finalize_pm_report(map_id=intent.map_id, completion=completion)
            return
        report = PMReport.from_payload(report_payload)
        effect_id = self._pm_stage_effect_id(
            map_id=intent.map_id,
            record_id=report.content.record_id,
        )
        try:
            self._outbox.enqueue(
                effect_id=effect_id,
                effect_type=TRACKER_STAGE_TRANSITION,
                map_id=intent.map_id,
                payload={
                    "issue_url": str(intent.payload["issue_url"]),
                    "issue_id": intent.map_id,
                    "project_id": str(intent.payload["project_id"]),
                    "expected_stage": str(intent.payload["expected_stage"]),
                    "requested_stage": str(requested_stage),
                    "protected_mutation_id": None,
                    "pm_report_completion": completion,
                },
                created_at=self._synchronized_at(),
            )
        except OutboxConflictError as error:
            raise TrackerEffectPayloadConflict(
                "PM report stage identity belongs to another payload"
            ) from error

    def _finalize_pm_report(
        self,
        *,
        map_id: str,
        completion: dict[str, Any],
    ) -> None:
        report_payload = completion.get("report")
        if not isinstance(report_payload, dict):
            raise EffectRetryableError("PM completion has no report payload")
        report = PMReport.from_payload(report_payload)
        record = TrackerPMReportRecord(
            report=report,
            tracker_record_id=str(completion["tracker_record_id"]),
            tracker_record_url=str(completion["tracker_record_url"]),
        )
        self._storage.save_pm_report_projection(
            map_id=map_id,
            report=self._pm_report_projection(
                record,
                confirmed_at=self._synchronized_at(),
            ),
        )
        try:
            self._storage.finish_pm_turn(
                map_id=map_id,
                turn_id=str(completion["active_turn_id"]),
                outcome="report",
                outcome_id=report.content.record_id,
                finished_at=self._synchronized_at(),
            )
        except ValueError as error:
            raise EffectTerminalError(str(error)) from error

    def _complete_approval_effect(
        self,
        intent: OutboxIntent,
        confirmation: EffectConfirmation,
    ) -> None:
        acknowledgment = confirmation.acknowledgment
        event_payload = acknowledgment.get("event")
        if not isinstance(event_payload, dict):
            raise EffectRetryableError(
                "Tracker approval confirmation has no event payload"
            )
        event = ApprovalHistoryEvent(**event_payload)
        tracker_record_id = str(acknowledgment["tracker_record_id"])
        tracker_record_url = str(acknowledgment["tracker_record_url"])
        completion = intent.payload.get("completion")
        if not isinstance(completion, dict):
            raise EffectRetryableError("Approval effect has no completion metadata")
        operation = completion.get("operation")
        approval = self._storage.approval(event.request_id)
        if operation == "request":
            packet = event.details
            if approval is not None:
                if approval["map_id"] != intent.map_id or approval[
                    "packet_hash"
                ] != packet.get("packet_hash"):
                    raise TrackerEffectPayloadConflict(
                        "approval request projection belongs to another packet"
                    )
                return
            self._storage.save_approval_request(
                map_id=intent.map_id,
                packet=packet,
                packet_hash=str(packet["packet_hash"]),
                requested_by_profile=str(completion["requested_by_profile"]),
                requested_by_session=str(completion["requested_by_session"]),
                requested_at=event.occurred_at,
                tracker_record_id=tracker_record_id,
                tracker_record_url=tracker_record_url,
            )
            return
        if approval is None:
            raise EffectRetryableError(
                "Approval request projection is missing before its outcome"
            )
        details = event.details
        if operation == "decision":
            if approval["status"] == event.event_type:
                if (
                    approval["decided_by"] == details.get("actor_id")
                    and approval["decided_by_profile"] == details.get("actor_profile")
                    and approval["decision_note"] == details.get("note")
                ):
                    return
                raise TrackerEffectPayloadConflict(
                    "approval decision projection has different content"
                )
            if approval["status"] != "pending":
                raise TrackerEffectPayloadConflict(
                    "approval projection is no longer pending"
                )
            self._storage.apply_approval_decision(
                request_id=event.request_id,
                decision=event.event_type,
                actor_id=str(details["actor_id"]),
                actor_profile=str(details["actor_profile"]),
                note=str(details["note"]),
                decided_at=event.occurred_at,
                expires_at=(
                    str(details["expires_at"]) if details.get("expires_at") else None
                ),
                tracker_record_id=tracker_record_id,
                tracker_record_url=tracker_record_url,
            )
            return
        if operation == "revocation":
            if approval["status"] == "revoked":
                if (
                    approval["decided_by"] == details.get("actor_id")
                    and approval["decided_by_profile"] == details.get("actor_profile")
                    and approval["decision_note"] == details.get("note")
                ):
                    return
                raise TrackerEffectPayloadConflict(
                    "approval revocation projection has different content"
                )
            if approval["status"] != "approved":
                raise TrackerEffectPayloadConflict(
                    "approval projection is no longer revocable"
                )
            self._storage.revoke_approval(
                request_id=event.request_id,
                actor_id=str(details["actor_id"]),
                actor_profile=str(details["actor_profile"]),
                note=str(details["note"]),
                revoked_at=event.occurred_at,
                tracker_record_id=tracker_record_id,
                tracker_record_url=tracker_record_url,
            )
            return
        raise EffectTerminalError(
            f"Unsupported approval completion operation: {operation}"
        )

    def _validate_approval_action(
        self,
        *,
        map_id: str,
        approval: dict[str, Any],
        decision_class: str,
        action: str,
        scope: dict[str, Any],
        payload: dict[str, Any],
        actor_identity: GovernanceActorIdentity | None,
    ) -> None:
        reason = None
        if approval["proposed_action"] != action:
            reason = "approval_action_mismatch"
        elif normalized_json(approval["requested_scope"]) != normalized_json(scope):
            reason = "approval_scope_mismatch"
        elif normalized_json(approval["decision_payload"]) != normalized_json(payload):
            reason = "approval_payload_mismatch"
        elif approval["decision_class"] != decision_class:
            reason = "approval_decision_class_mismatch"
        elif approval["payload_hash"] != normalized_hash(
            {"action": action, "scope": scope, "payload": payload}
        ):
            reason = "approval_payload_mismatch"
        if reason is not None:
            self._deny_approval_enforcement(
                map_id=map_id,
                action=action,
                reason=reason,
                actor_identity=actor_identity,
            )

    def _transition_requires_approval(self, *, requested_stage: str) -> bool:
        return (
            self._authority_policy.classify(
                self._transition_decision_class(requested_stage),
                decision_payload={"requested_stage": requested_stage},
            )
            != "ceo"
        )

    @staticmethod
    def _transition_decision_class(requested_stage: str) -> str:
        return (
            "delivery_authorization"
            if requested_stage == "authorized"
            else "operational"
        )

    def _synchronized_at(self) -> str:
        return self._format_datetime(self._current_datetime())

    def _current_datetime(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _format_datetime(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _stored_project(project: TrackerProject) -> dict[str, Any]:
        return {
            "id": project.id,
            "url": project.url,
            "owner": project.owner,
            "owner_type": project.owner_type,
            "number": project.number,
            "title": project.title,
        }

    def _stored_card(
        self,
        issue: TrackerIssue,
        *,
        project_id: str,
        synchronized_at: str,
    ) -> dict[str, Any]:
        return {
            "id": issue.id,
            "project_id": project_id,
            "repository": issue.repository,
            "issue_number": issue.number,
            "issue_url": issue.url,
            "title": issue.title,
            "stage": self._executive_stage(issue),
            "ceo_session_state": "unbound",
            "synchronized_at": synchronized_at,
        }

    @staticmethod
    def _executive_stage(issue: TrackerIssue) -> str:
        try:
            return executive_stage(
                issue_state=issue.state,
                state_reason=issue.state_reason,
                labels=issue.labels,
            )
        except ValueError as error:
            raise MapBindingError(str(error)) from error

    @staticmethod
    def _configured_project(
        project: TrackerProject,
        *,
        synchronized_at: str,
    ) -> dict[str, Any]:
        return {
            "id": project.id,
            "tracker": {
                "provider": "github",
                "id": project.id,
                "owner": project.owner,
                "owner_type": project.owner_type,
                "number": project.number,
                "url": project.url,
            },
            "title": project.title,
            "last_synchronized_at": synchronized_at,
            "maps": [],
        }

    @classmethod
    def _project_projection(cls, row: dict[str, Any]) -> dict[str, Any]:
        project = TrackerProject(
            id=row["project_id"],
            owner=row["owner_login"],
            owner_type=row["owner_type"],
            number=row["project_number"],
            title=row["title"],
            url=row["project_url"],
        )
        return cls._configured_project(
            project,
            synchronized_at=row["synchronized_at"],
        )

    @staticmethod
    def _card_projection(
        row: dict[str, Any],
        *,
        project_url: str,
    ) -> dict[str, Any]:
        map_id = row.get("id", row.get("map_id"))
        if map_id is None:
            raise RuntimeError("Map projection has no tracker id")
        return {
            "id": map_id,
            "project": {
                "id": row["project_id"],
                "url": project_url,
            },
            "tracker": {
                "provider": "github",
                "id": map_id,
                "identity": f"{row['repository']}#{row['issue_number']}",
                "url": row["issue_url"],
            },
            "title": row["title"],
            "stage": row["stage"],
            "available_transitions": list(available_transitions(row["stage"])),
            "ceo_session": MapGovernanceApplication._session_projection(row),
            "last_synchronized_at": row["synchronized_at"],
        }

    def _card_with_summary(
        self,
        row: dict[str, Any],
        *,
        project_url: str,
    ) -> dict[str, Any]:
        card = self._card_projection(row, project_url=project_url)
        card["decision_summary"] = self._storage.decision_summary(map_id=card["id"])
        approvals = self._approval_collection(map_id=card["id"])
        card["approval_summary"] = {
            "count": approvals["count"],
            "pending_count": sum(
                item["status"] == "pending" for item in approvals["items"]
            ),
            "latest": approvals["items"][0] if approvals["items"] else None,
        }
        card["delivery_summary"] = self._storage.pm_delivery_summary(map_id=card["id"])
        card["external_effects"] = self._outbox.map_summary(card["id"])
        return card

    @staticmethod
    def _session_projection(row: dict[str, Any]) -> dict[str, Any]:
        state = row["ceo_session_state"]
        if state == "ready":
            return {
                "state": "ready",
                "last_activity_at": row.get("ceo_session_last_activity_at"),
            }
        if state == "repair_required":
            return {
                "state": "repair_required",
                "repair": {
                    "reason": row.get("ceo_session_repair_reason"),
                    "candidate_count": int(row.get("ceo_session_candidate_count") or 0),
                },
            }
        return {"state": "unbound"}
