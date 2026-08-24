"""The single application interface shared by every plugin adapter."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Callable, Iterator, Mapping, NoReturn, cast

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
    TRACKER_PUBLICATION_RECORD,
    TRACKER_ISSUE_CLOSE,
    TRACKER_STAGE_TRANSITION,
    PUBLISHER_EXECUTE,
    CoordinatorResumeBoundary,
    CoordinatorResumeEffectAdapter,
    SessionResumeBoundary,
    SessionResumeEffectAdapter,
    TrackerEffectAdapter,
    TrackerEffectPayloadConflict,
    TrackerStageEffectConflict,
    PublisherEffectAdapter,
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
from .events import BoardEventJournal, BoardEventSettings
from .sessions import (
    CEOSessionRunner,
    CanonicalSession,
    canonical_session_identity,
    canonical_session_title,
)
from .reports import (
    PMDecisionResponse,
    PMReport,
    PMReportDraft,
    TrackerPMReportRecord,
)
from .publication import (
    AcceptanceEvidence,
    ApprovedPublicationAction,
    PublicationAction,
    PublicationHandoff,
    PublicationRecord,
    PublisherBoundary,
    RemotePublicationEvidence,
)
from .coordinator import (
    DELIVERY_TRANSPORT_RECOVERY_LIMITATION,
    CommissioningAuthorizationError,
    CommissioningContext,
    CommissioningPrerequisiteError,
    CommissioningPrerequisiteResolver,
    CoordinatorRuntimeError,
    CoordinatorRuntimeBoundary,
    DeliveryLaneRegistry,
    DeliveryLaneSpec,
    DeliveryRuntimeRequest,
    RootRuntimeRequest,
    delivery_confirmed_dispatch_id,
    delivery_dispatch_id,
    validate_delivery_lane_registry,
)
from .tracker import (
    GitHubTrackerAdapter,
    TrackerAdapter,
    TrackerApprovalRecord,
    StructuredDecision,
    TrackerDecisionRecord,
    TrackerError,
    TrackerIssue,
    TrackerPublicationRecord,
    TrackerProject,
)


class MapBindingError(ValueError):
    """Raised when a requested binding violates governance identity rules."""


class StaleProjectionError(PermissionError):
    """A governance write was blocked until authority is revalidated."""

    def __init__(
        self,
        *,
        project_id: str,
        source: str,
        last_success_at: str,
        reason: str,
    ) -> None:
        self.project_id = project_id
        self.source = source
        self.last_success_at = last_success_at
        self.reason = reason
        super().__init__(
            f"{source} authority is stale for {project_id}: {reason}; reconnect "
            "authority and complete authoritative reconcile before retrying"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "stale_projection",
            "project_id": self.project_id,
            "source": self.source,
            "last_success_at": self.last_success_at,
            "reason": self.reason,
            "recovery": (
                "Reconnect tracker authority and complete authoritative reconcile."
            ),
            "retryable": True,
        }


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


class PublicationRepairRequired(RuntimeError):
    """A remote publication may have changed state and needs governed repair."""

    def __init__(self, *, map_id: str, action_id: str, reason: str) -> None:
        self.map_id = map_id
        self.action_id = action_id
        self.reason = reason
        super().__init__(f"Publication repair required: {reason}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "publication_repair_required",
            "map_id": self.map_id,
            "action_id": self.action_id,
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


class DecisionResumePendingError(RuntimeError):
    """A committed decision is waiting for retry-safe delivery to its PM."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "decision_resume_pending",
            "reason": self.reason,
            "retryable": True,
        }


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
        event_settings: BoardEventSettings | None = None,
        commissioning_prerequisites: CommissioningPrerequisiteResolver | None = None,
        coordinator_runtime: CoordinatorRuntimeBoundary | None = None,
        publisher: PublisherBoundary | None = None,
        publication_handoff: PublicationHandoff | None = None,
        publication_authority_ref: str | None = None,
        storage_group_id: int | None = None,
    ) -> None:
        self._plugin_root = plugin_root.resolve()
        self._storage = PluginStorage(storage_root, shared_gid=storage_group_id)
        self._tracker = tracker or GitHubTrackerAdapter()
        self._session_runner = session_runner
        self._profile_name = profile_name
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._authority_policy = (
            authority_policy or AuthorityEnvelopePolicy.from_settings(None)
        )
        self._outbox = OutboxRepository(storage_root, shared_gid=storage_group_id)
        self._event_settings = event_settings or BoardEventSettings()
        self._board_events = BoardEventJournal(
            self._storage.database,
            settings=self._event_settings,
            clock=self._clock,
        )
        self._outbox_settings = outbox_settings or OutboxSettings()
        self._outbox_owner_id = outbox_owner_id or (
            f"map-governance:{os.getpid()}:{uuid.uuid4()}"
        )
        self._outbox_runtime: OutboxRuntime | None = None
        tracker_effects = TrackerEffectAdapter(
            self._tracker,
            publication_fence=self._publication_tracker_fence,
        )
        effect_adapters: dict[str, Any] = {
            TRACKER_STAGE_TRANSITION: tracker_effects,
            TRACKER_DECISION: tracker_effects,
            TRACKER_APPROVAL_EVENT: tracker_effects,
            TRACKER_PM_REPORT: tracker_effects,
            TRACKER_PUBLICATION_RECORD: tracker_effects,
            TRACKER_ISSUE_CLOSE: tracker_effects,
        }
        self._publisher = publisher
        self._publication_handoff = publication_handoff
        self._publication_authority_ref = publication_authority_ref or (
            publisher.authority_ref if publisher is not None else None
        )
        if publisher is not None:
            effect_adapters[PUBLISHER_EXECUTE] = PublisherEffectAdapter(
                publisher,
                execution_fence=self._publisher_execution_fence,
                before_execute=self._mark_publisher_external_call,
            )
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
        self._commissioning_prerequisites = commissioning_prerequisites
        self._coordinator_runtime = coordinator_runtime
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
            interlock=self._enforce_external_effect_interlock,
            failure=self._external_effect_failed,
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

    def recover_restart(self, *, outbox_limit: int = 100) -> dict[str, Any]:
        """Rebuild durable projections and reconnect recorded runtime identities.

        Recovery intentionally differs from ``open_map``: it never mints a CEO
        session.  Only a previously recorded lineage that still resolves to its
        exact canonical title is reconnected automatically.
        """
        rebuilt = self.refresh()
        session_results: list[dict[str, Any]] = []
        if self._session_runner is not None and self._profile_name:
            for binding in self._storage.ceo_session_registry():
                map_id = str(binding["map_id"])
                if binding["profile_name"] != self._profile_name:
                    self._record_ceo_repair_required(
                        map_id=map_id,
                        reason="canonical_profile_mismatch",
                        candidate_count=0,
                        preserve_existing_identity=True,
                    )
                    session_results.append(
                        {
                            "map_id": map_id,
                            "state": "repair_required",
                            "reason": "canonical_profile_mismatch",
                            "evidence": {
                                "recorded_profile_name": binding["profile_name"],
                                "requested_profile_name": self._profile_name,
                            },
                        }
                    )
                    continue
                try:
                    result = self._recover_ceo_session(binding)
                except CEOSessionRepairRequired as error:
                    self._record_ceo_repair_required(
                        map_id=map_id,
                        reason=error.reason,
                        candidate_count=error.candidate_count,
                    )
                    result = {
                        "map_id": map_id,
                        "state": "repair_required",
                        "reason": error.reason,
                    }
                except (OSError, RuntimeError):
                    result = {
                        "map_id": map_id,
                        "state": "pending",
                        "reason": "session_reconnect_pending",
                    }
                session_results.append(result)

        runtime_results = self._recover_pm_runtimes()
        outbox = self.recover_outbox(limit=outbox_limit)
        outbox["unfinished"] = [
            {
                "effect_id": intent.effect_id,
                "effect_type": intent.effect_type,
                "state": intent.state,
                "attempt_count": intent.attempt_count,
            }
            for intent in self._outbox.all_unfinished_intents()
        ]
        projection_stale = any(
            project.get("authority", {}).get("state") != "healthy"
            for project in rebuilt["projects"]
        )
        repair_required = any(
            item["state"] == "repair_required"
            for item in [*session_results, *runtime_results]
        ) or any(item["state"] == "terminal" for item in outbox["unfinished"])
        pending = any(
            item["state"] == "pending" for item in [*session_results, *runtime_results]
        ) or bool(outbox["unfinished"])
        return {
            "state": (
                "repair_required"
                if repair_required
                else "pending"
                if pending
                else "stale"
                if projection_stale
                else "recovered"
            ),
            "projections": {
                "project_count": len(rebuilt["projects"]),
                "map_count": len(rebuilt["maps"]),
                "state": "stale" if projection_stale else "rebuilt",
            },
            "ceo_sessions": session_results,
            "pm_runtimes": runtime_results,
            "outbox": outbox,
        }

    def _recover_ceo_session(self, binding: Mapping[str, Any]) -> dict[str, Any]:
        session_runner = self._session_runner
        profile_name = self._profile_name
        if session_runner is None or not profile_name:  # pragma: no cover
            raise RuntimeError("CEO session recovery is not configured")
        map_id = str(binding["map_id"])
        expected_title = canonical_session_title(map_id)
        if binding["state"] == "repair_required":
            exact_candidates = session_runner.find_exact(title=expected_title)
            reason = str(
                binding.get("repair_reason") or "recorded_session_lineage_missing"
            )
            if len(exact_candidates) > 1:
                reason = "multiple_exact_canonical_sessions"
            recorded_root = str(binding.get("root_session_id") or "")
            if (
                len(exact_candidates) == 1
                and recorded_root
                and exact_candidates[0].root_session_id == recorded_root
            ):
                session = exact_candidates[0]
            else:
                self._record_ceo_repair_required(
                    map_id=map_id,
                    reason=reason,
                    candidate_count=len(exact_candidates),
                )
                return {
                    "map_id": map_id,
                    "state": "repair_required",
                    "reason": reason,
                }
        else:
            root_session_id = str(binding["root_session_id"])
            session = session_runner.resolve(root_session_id=root_session_id)
            exact_candidates = session_runner.find_exact(title=expected_title)
            if (
                len(exact_candidates) != 1
                or session is None
                or session.title != expected_title
                or exact_candidates[0].root_session_id != root_session_id
            ):
                reason = (
                    "multiple_exact_canonical_sessions"
                    if len(exact_candidates) > 1
                    else (
                        "recorded_session_lineage_missing"
                        if session is None
                        else (
                            "recorded_session_lineage_conflicting"
                            if session.title != expected_title
                            else "canonical_session_inventory_conflicting"
                        )
                    )
                )
                self._record_ceo_repair_required(
                    map_id=map_id,
                    reason=reason,
                    candidate_count=len(exact_candidates),
                )
                return {
                    "map_id": map_id,
                    "state": "repair_required",
                    "reason": reason,
                }
        context = self._storage.map_session_context(map_id)
        if context is None:
            self._record_ceo_repair_required(
                map_id=map_id,
                reason="map_binding_missing",
                candidate_count=0,
            )
            return {
                "map_id": map_id,
                "state": "repair_required",
                "reason": "map_binding_missing",
            }
        identity = canonical_session_identity(
            profile_name=profile_name,
            map_id=map_id,
        )
        bootstrap_hash = str(binding.get("bootstrap_hash") or "")
        if not bootstrap_hash:
            bootstrap_hash = hashlib.sha256(
                self._ceo_bootstrap(
                    context,
                    canonical_identity=identity,
                ).encode()
            ).hexdigest()
        ready = self._ready_session(
            map_id=map_id,
            identity=identity,
            title=expected_title,
            session=session,
            bootstrap_hash=bootstrap_hash,
        )["ceo_session"]
        return {
            "map_id": map_id,
            "state": "reconnected",
            "root_session_id": ready["root_session_id"],
            "live_session_id": ready["live_session_id"],
        }

    def _record_ceo_repair_required(
        self,
        *,
        map_id: str,
        reason: str,
        candidate_count: int,
        preserve_existing_identity: bool = False,
    ) -> None:
        profile_name = self._profile_name or ""
        self._storage.save_ceo_session_repair_required(
            map_id=map_id,
            profile_name=profile_name,
            canonical_identity=canonical_session_identity(
                profile_name=profile_name,
                map_id=map_id,
            ),
            canonical_title=canonical_session_title(map_id),
            reason=reason,
            candidate_count=candidate_count,
            updated_at=self._synchronized_at(),
            preserve_existing_identity=preserve_existing_identity,
        )

    def _recover_pm_runtimes(self) -> list[dict[str, Any]]:
        if (
            self._coordinator_runtime is None
            or self._commissioning_prerequisites is None
        ):
            return []
        results: list[dict[str, Any]] = []
        for recorded in self._storage.pm_runtimes():
            map_id = str(recorded["map_id"])
            if recorded["state"] == "repair_required":
                results.append(
                    {
                        "map_id": map_id,
                        "state": "repair_required",
                        "reason": str(
                            (recorded.get("failure") or {}).get(
                                "reason", "runtime_repair_required"
                            )
                        ),
                    }
                )
                continue
            try:
                issue, context = self._verified_runtime_map_binding(recorded)
                rediscovered = self._coordinator_runtime.ensure_root(
                    RootRuntimeRequest(
                        map_id=map_id,
                        map_url=issue.url,
                        context=context,
                    )
                )
                stable_fields = (
                    "map_id",
                    "project_id",
                    "project_url",
                    "repository",
                    "repository_path",
                    "pm_profile",
                    "routing_policy",
                    "herdr_executable",
                    "session_namespace",
                    "workspace_label",
                    "agent_id",
                    "ownership_marker",
                    "lifecycle_id",
                )
                if rediscovered is None or any(
                    str(rediscovered.get(name) or "") != str(recorded.get(name) or "")
                    for name in stable_fields
                ):
                    raise ValueError("runtime_rediscovery_identity_conflict")
            except CoordinatorRuntimeError as error:
                if error.repair_required:
                    self._coordinator_runtime.record_failure(
                        map_id=map_id,
                        reason=error.reason,
                        retryable=error.retryable,
                        repair_required=True,
                    )
                    results.append(
                        {
                            "map_id": map_id,
                            "state": "repair_required",
                            "reason": error.reason,
                        }
                    )
                else:
                    results.append(
                        {
                            "map_id": map_id,
                            "state": "pending",
                            "reason": error.reason,
                        }
                    )
                continue
            except (CommissioningPrerequisiteError, TrackerError) as error:
                results.append(
                    {
                        "map_id": map_id,
                        "state": "pending",
                        "reason": type(error).__name__,
                    }
                )
                continue
            except ValueError as error:
                reason = str(error)
                self._coordinator_runtime.record_failure(
                    map_id=map_id,
                    reason=reason,
                    retryable=False,
                    repair_required=True,
                )
                results.append(
                    {
                        "map_id": map_id,
                        "state": "repair_required",
                        "reason": reason,
                    }
                )
                continue
            results.append(
                {
                    "map_id": map_id,
                    "state": "rediscovered",
                    "runtime_state": str(rediscovered["state"]),
                }
            )
        return results

    def preview_repairs(self) -> dict[str, Any]:
        """Preview only identity changes that current evidence proves safe."""
        actions: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        if self._session_runner is not None and self._profile_name:
            for binding in self._storage.ceo_session_registry():
                if binding["state"] != "repair_required":
                    continue
                map_id = str(binding["map_id"])
                if binding["profile_name"] != self._profile_name:
                    blocked.append(
                        {
                            "resource": "ceo_session",
                            "map_id": map_id,
                            "reason": "canonical_profile_mismatch",
                            "evidence": {
                                "recorded_profile_name": binding["profile_name"],
                                "requested_profile_name": self._profile_name,
                            },
                        }
                    )
                    continue
                title = str(binding["canonical_title"])
                candidates = self._session_runner.find_exact(title=title)
                evidence = {
                    "profile_name": self._profile_name,
                    "canonical_title": title,
                    "exact_candidate_count": len(candidates),
                }
                if len(candidates) != 1:
                    blocked.append(
                        {
                            "resource": "ceo_session",
                            "map_id": map_id,
                            "reason": (
                                "canonical_session_missing"
                                if not candidates
                                else "canonical_session_ambiguous"
                            ),
                            "evidence": evidence,
                        }
                    )
                    continue
                candidate = candidates[0]
                bootstrap_verified = self._ceo_candidate_bootstrap_verified(
                    binding=binding,
                    candidate=candidate,
                )
                if not bootstrap_verified:
                    blocked.append(
                        {
                            "resource": "ceo_session",
                            "map_id": map_id,
                            "reason": "canonical_session_bootstrap_unverified",
                            "evidence": {
                                **evidence,
                                "bootstrap_marker_verified": False,
                            },
                        }
                    )
                    continue
                owner = self._storage.ceo_session_root_owner(candidate.root_session_id)
                if owner not in {None, map_id} or (
                    candidate.root_session_id == binding["root_session_id"]
                    and candidate.live_session_id == binding["live_session_id"]
                ):
                    blocked.append(
                        {
                            "resource": "ceo_session",
                            "map_id": map_id,
                            "reason": (
                                "canonical_session_bound_to_another_map"
                                if owner not in {None, map_id}
                                else "canonical_lineage_resolution_conflict"
                            ),
                            "evidence": evidence,
                        }
                    )
                    continue
                before = {
                    "root_session_id": binding["root_session_id"],
                    "live_session_id": binding["live_session_id"],
                    "state": binding["state"],
                    "repair_reason": binding["repair_reason"],
                }
                after = {
                    "root_session_id": candidate.root_session_id,
                    "live_session_id": candidate.live_session_id,
                    "state": "ready",
                }
                actions.append(
                    {
                        "id": (
                            f"ceo-session:{map_id}:rebind:{candidate.root_session_id}"
                        ),
                        "kind": "ceo_session.rebind",
                        "map_id": map_id,
                        "safe": True,
                        "before": before,
                        "after": after,
                        "evidence": {
                            **evidence,
                            "bootstrap_marker_verified": True,
                            "candidate_last_activity_at": (candidate.last_activity_at),
                        },
                    }
                )
        if self._coordinator_runtime is not None:
            preview_runtime = getattr(
                self._coordinator_runtime,
                "preview_repair",
                None,
            )
            for recorded in self._storage.pm_runtimes():
                if recorded["state"] != "repair_required":
                    continue
                map_id = str(recorded["map_id"])
                if not callable(preview_runtime):
                    blocked.append(
                        {
                            "resource": "pm_runtime",
                            "map_id": map_id,
                            "reason": "runtime_repair_preview_unavailable",
                            "evidence": {"map_binding_verified": False},
                        }
                    )
                    continue
                if not self._runtime_map_binding_verified(recorded):
                    blocked.append(
                        {
                            "resource": "pm_runtime",
                            "map_id": map_id,
                            "reason": "runtime_map_binding_conflict",
                            "evidence": {"map_binding_verified": False},
                        }
                    )
                    continue
                try:
                    candidate = preview_runtime(map_id=map_id)
                except CoordinatorRuntimeError as error:
                    blocked.append(
                        {
                            "resource": "pm_runtime",
                            "map_id": map_id,
                            "reason": error.reason,
                            "evidence": {
                                **error.evidence(),
                                "map_binding_verified": True,
                            },
                        }
                    )
                    continue
                if not isinstance(candidate, Mapping):
                    blocked.append(
                        {
                            "resource": "pm_runtime",
                            "map_id": map_id,
                            "reason": "runtime_repair_evidence_malformed",
                            "evidence": {"map_binding_verified": True},
                        }
                    )
                    continue
                before = candidate.get("before")
                after = candidate.get("after")
                evidence = candidate.get("evidence")
                if not all(
                    isinstance(value, Mapping) for value in (before, after, evidence)
                ):
                    blocked.append(
                        {
                            "resource": "pm_runtime",
                            "map_id": map_id,
                            "reason": "runtime_repair_evidence_malformed",
                            "evidence": {"map_binding_verified": True},
                        }
                    )
                    continue
                action_digest = hashlib.sha256(
                    normalized_json(after).encode()
                ).hexdigest()[:16]
                actions.append(
                    {
                        "id": f"pm-runtime:{map_id}:rebind:{action_digest}",
                        "kind": "pm_runtime.rebind_coordinates",
                        "map_id": map_id,
                        "safe": True,
                        "before": dict(before),
                        "after": dict(after),
                        "evidence": {
                            **dict(evidence),
                            "map_binding_verified": True,
                        },
                    }
                )
        return self._identity_repair_plan(actions=actions, blocked=blocked)

    def _ceo_candidate_bootstrap_verified(
        self,
        *,
        binding: Mapping[str, Any],
        candidate: CanonicalSession,
    ) -> bool:
        session_runner = self._session_runner
        bootstrap_hash = str(binding.get("bootstrap_hash") or "")
        canonical_identity = str(binding.get("canonical_identity") or "")
        verifier = getattr(session_runner, "has_bootstrap_marker", None)
        if not bootstrap_hash or not canonical_identity or not callable(verifier):
            return False
        return bool(
            verifier(
                root_session_id=candidate.root_session_id,
                idempotency_key=(f"{canonical_identity}:bootstrap:{bootstrap_hash}"),
            )
        )

    def _runtime_map_binding_verified(self, recorded: Mapping[str, Any]) -> bool:
        try:
            self._verified_runtime_map_binding(recorded)
        except (CommissioningPrerequisiteError, TrackerError, ValueError):
            return False
        return True

    def _verified_runtime_map_binding(
        self,
        recorded: Mapping[str, Any],
    ) -> tuple[TrackerIssue, CommissioningContext]:
        prerequisites = self._commissioning_prerequisites
        if prerequisites is None:
            raise ValueError("runtime_prerequisites_missing")
        map_id = str(recorded.get("map_id") or "")
        binding = self._storage.map_binding(map_id)
        if binding is None:
            raise ValueError("runtime_map_binding_missing")
        project = self._storage.project_binding(str(binding["project_id"]))
        if project is None:
            raise ValueError("runtime_map_binding_missing")
        issue = self._tracker.get_issue(str(binding["issue_url"]))
        context = prerequisites.commissioning_context(
            project_id=str(binding["project_id"]),
            repository=issue.repository,
        )
        expected = {
            "map_id": map_id,
            "project_id": str(binding["project_id"]),
            "project_url": str(project["project_url"]),
            "repository": issue.repository,
            "repository_path": context.repository_path,
            "pm_profile": context.pm_profile,
            "routing_policy": context.routing_policy,
            "herdr_executable": context.herdr_executable,
        }
        if (
            issue.id != map_id
            or issue.url != binding["issue_url"]
            or any(
                str(recorded.get(name) or "") != value
                for name, value in expected.items()
            )
        ):
            raise ValueError("runtime_map_binding_conflict")
        return issue, context

    def apply_repairs(
        self,
        *,
        plan: Mapping[str, Any],
        selected_action_ids: list[str],
        authorizer: str,
    ) -> dict[str, Any]:
        """Apply selected, still-proven identity repairs and audit authority."""
        authorizer = str(authorizer).strip()
        if not authorizer or len(authorizer) > 256:
            raise ValueError("Repair authorizer is required")
        if not selected_action_ids or any(
            not isinstance(item, str) or not item.strip()
            for item in selected_action_ids
        ):
            raise ValueError("At least one repair action must be selected")
        if len(set(selected_action_ids)) != len(selected_action_ids):
            raise ValueError("Repair action selection contains duplicates")
        if not isinstance(plan, Mapping):
            raise ValueError("Repair plan must be an object")
        supplied_actions = plan.get("actions")
        supplied_blocked = plan.get("blocked")
        if not isinstance(supplied_actions, list) or not isinstance(
            supplied_blocked, list
        ):
            raise ValueError("Repair plan is malformed")
        expected = self._identity_repair_plan(
            actions=supplied_actions,
            blocked=supplied_blocked,
        )
        if plan.get("plan_id") != expected["plan_id"]:
            raise ValueError("Repair plan identity does not match its content")
        supplied_by_id = {
            str(action.get("id")): action
            for action in supplied_actions
            if isinstance(action, Mapping)
        }
        if any(action_id not in supplied_by_id for action_id in selected_action_ids):
            raise ValueError("Selected repair action is not in the preview")
        idempotent_action_ids: list[str] = []
        pending_action_ids: list[str] = []
        for action_id in selected_action_ids:
            supplied = supplied_by_id[action_id]
            repair_id = f"{plan['plan_id']}:{action_id}"
            prior = next(
                (
                    item
                    for item in self._storage.identity_repair_history(
                        map_id=str(supplied["map_id"])
                    )
                    if item["repair_id"] == repair_id
                ),
                None,
            )
            if prior is None:
                pending_action_ids.append(action_id)
                continue
            resource_type = (
                "ceo_session"
                if supplied.get("kind") == "ceo_session.rebind"
                else "pm_runtime"
            )
            if any(
                (
                    prior["plan_id"] != plan["plan_id"],
                    prior["action_id"] != action_id,
                    prior["resource_type"] != resource_type,
                    prior["authorizer"] != authorizer,
                    normalized_json(prior["before"])
                    != normalized_json(supplied["before"]),
                    normalized_json(prior["after"])
                    != normalized_json(supplied["after"]),
                    normalized_json(prior["evidence"])
                    != normalized_json(supplied["evidence"]),
                )
            ):
                raise ValueError("Repair identity was reused with different content")
            idempotent_action_ids.append(action_id)
        current_by_id = {
            action["id"]: action for action in self.preview_repairs()["actions"]
        }
        for action_id in pending_action_ids:
            supplied = supplied_by_id[action_id]
            current = current_by_id.get(action_id)
            if current is None or normalized_json(current) != normalized_json(supplied):
                raise ValueError("Repair preview is stale; generate a new preview")

        applied_action_ids: list[str] = []
        for action_id in pending_action_ids:
            action = dict(supplied_by_id[action_id])
            if action.get("safe") is not True:
                raise ValueError("Selected identity repair is not proven safe")
            repair_id = f"{plan['plan_id']}:{action_id}"
            if action.get("kind") == "ceo_session.rebind":
                self._storage.apply_ceo_session_binding_repair(
                    repair_id=repair_id,
                    plan_id=str(plan["plan_id"]),
                    action_id=action_id,
                    map_id=str(action["map_id"]),
                    authorizer=authorizer,
                    before=dict(action["before"]),
                    after=dict(action["after"]),
                    evidence=dict(action["evidence"]),
                    last_activity_at=action["evidence"].get(
                        "candidate_last_activity_at"
                    ),
                    applied_at=self._synchronized_at(),
                )
            elif action.get("kind") == "pm_runtime.rebind_coordinates":
                self._storage.apply_pm_runtime_binding_repair(
                    repair_id=repair_id,
                    plan_id=str(plan["plan_id"]),
                    action_id=action_id,
                    map_id=str(action["map_id"]),
                    authorizer=authorizer,
                    before=dict(action["before"]),
                    after=dict(action["after"]),
                    evidence=dict(action["evidence"]),
                    applied_at=self._synchronized_at(),
                )
            else:
                raise ValueError("Selected identity repair is not proven safe")
            applied_action_ids.append(action_id)
        return {
            "state": "applied",
            "plan_id": plan["plan_id"],
            "authorizer": authorizer,
            "applied_action_ids": applied_action_ids,
            "idempotent_action_ids": idempotent_action_ids,
        }

    def identity_repair_history(self, *, map_id: str) -> list[dict[str, Any]]:
        """Return durable identity-binding repair authority and evidence."""
        return self._storage.identity_repair_history(map_id=map_id)

    def _identity_repair_plan(
        self,
        *,
        actions: list[dict[str, Any]],
        blocked: list[dict[str, Any]],
    ) -> dict[str, Any]:
        content = {
            "schema_version": 1,
            "profile_name": self._profile_name,
            "actions": actions,
            "blocked": blocked,
        }
        plan_id = (
            "identity-repair:"
            + hashlib.sha256(normalized_json(content).encode()).hexdigest()[:24]
        )
        return {
            **content,
            "plan_id": plan_id,
            "generated_at": self._synchronized_at(),
        }

    def _ensure_project_writable(self, *, project_id: str) -> None:
        for source in self._storage.project_reachability(project_id):
            if source["state"] == "healthy":
                continue
            raise StaleProjectionError(
                project_id=project_id,
                source=str(source["source"]),
                last_success_at=str(source["last_success_at"]),
                reason=str(source["reason"] or "authority reconcile is incomplete"),
            )

    def _ensure_map_writable(self, *, map_id: str) -> dict[str, Any]:
        binding = self._storage.map_binding(map_id)
        if binding is None:
            raise MapBindingError(f"Map is not bound: {map_id}")
        self._ensure_project_writable(project_id=str(binding["project_id"]))
        return binding

    def _enforce_external_effect_interlock(self, intent: OutboxIntent) -> None:
        binding = self._storage.map_binding(intent.map_id)
        if binding is None:
            return
        try:
            self._ensure_project_writable(project_id=str(binding["project_id"]))
        except StaleProjectionError as error:
            raise EffectRetryableError(str(error)) from error

    @contextmanager
    def _publication_tracker_fence(self, intent: OutboxIntent) -> Iterator[None]:
        """Fence publication evidence and closeout against acceptance writes."""
        with self._storage.publication_lease(intent.map_id):
            yield

    @contextmanager
    def _publisher_execution_fence(self, intent: OutboxIntent) -> Iterator[None]:
        """Hold the cross-process acceptance fence through the remote call."""
        action = ApprovedPublicationAction.from_payload(dict(intent.payload["action"]))
        binding = self._ensure_map_writable(map_id=action.map_id)
        expected_report_id = str(intent.payload["acceptance_report_id"])
        with self._storage.publication_lease(action.map_id):
            approval = self._storage.approval(
                str(intent.payload["approval_request_id"])
            )
            expires_at = approval.get("expires_at") if approval is not None else None
            approval_active = (
                approval is not None
                and approval["status"] == "consumed"
                and approval.get("consumed_by_mutation_id") == action.action_id
                and self._publisher is not None
                and intent.payload.get("publisher_authority_ref")
                == self._publisher.authority_ref
                and isinstance(expires_at, str)
                and self._current_datetime()
                < datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            )
            issue = self._tracker_read(
                project_id=str(binding["project_id"]),
                operation=lambda: self._tracker.get_issue(binding["issue_url"]),
            )
            reports = self._tracker_read(
                project_id=str(binding["project_id"]),
                operation=lambda: self._tracker.list_pm_reports(binding["issue_url"]),
            )
            acceptance_reports = [
                item
                for item in reports
                if item.report.content.report_type == "acceptance"
                and item.report.content.acceptance is not None
            ]
            latest = acceptance_reports[-1] if acceptance_reports else None
            acceptance = (
                latest.report.content.acceptance if latest is not None else None
            )
            requested = (
                acceptance.requested_publication_action
                if acceptance is not None
                else None
            )
            if (
                not approval_active
                or issue.id != action.map_id
                or self._executive_stage(issue) != "acceptance"
                or latest is None
                or latest.report.content.record_id != expected_report_id
                or acceptance is None
                or acceptance.revision != action.revision
                or requested is None
                or requested.action != action.action
                or normalized_json(requested.target) != normalized_json(action.target)
            ):
                raise EffectTerminalError(
                    "Approved publication became stale before privileged execution"
                )
            yield

    def _mark_publisher_external_call(self, intent: OutboxIntent) -> None:
        """Persist the uncertainty boundary after final preflight, before mutation."""
        action = ApprovedPublicationAction.from_payload(dict(intent.payload["action"]))
        binding = self._ensure_map_writable(map_id=action.map_id)
        issue = self._tracker_read(
            project_id=str(binding["project_id"]),
            operation=lambda: self._tracker.get_issue(str(binding["issue_url"])),
        )
        reports = self._tracker_read(
            project_id=str(binding["project_id"]),
            operation=lambda: self._tracker.list_pm_reports(str(binding["issue_url"])),
        )
        acceptance_reports = [
            item
            for item in reports
            if item.report.content.report_type == "acceptance"
            and item.report.content.acceptance is not None
        ]
        latest = acceptance_reports[-1] if acceptance_reports else None
        acceptance = latest.report.content.acceptance if latest is not None else None
        requested = (
            acceptance.requested_publication_action if acceptance is not None else None
        )
        attempted_at = self._synchronized_at()
        attempted_datetime = datetime.fromisoformat(attempted_at.replace("Z", "+00:00"))
        approval = self._storage.approval(str(intent.payload["approval_request_id"]))
        expires_at = approval.get("expires_at") if approval is not None else None
        decided_at = approval.get("decided_at") if approval is not None else None
        authorization_active = (
            approval is not None
            and approval["status"] == "consumed"
            and approval.get("consumed_by_mutation_id") == action.action_id
            and self._publisher is not None
            and intent.payload.get("publisher_authority_ref")
            == self._publisher.authority_ref
            and isinstance(decided_at, str)
            and isinstance(expires_at, str)
            and datetime.fromisoformat(decided_at.replace("Z", "+00:00"))
            <= attempted_datetime
            < datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        )
        if (
            not authorization_active
            or issue.id != action.map_id
            or self._executive_stage(issue) != "acceptance"
            or latest is None
            or latest.report.content.record_id
            != str(intent.payload["acceptance_report_id"])
            or acceptance is None
            or acceptance.revision != action.revision
            or requested is None
            or requested.action != action.action
            or normalized_json(requested.target) != normalized_json(action.target)
        ):
            raise EffectTerminalError(
                "Approved publication became stale before remote-call marker"
            )
        self._outbox.mark_external_call_started(
            effect_id=intent.effect_id,
            owner_id=self._outbox_owner_id,
            now=attempted_at,
        )

    def _external_effect_failed(self, intent: OutboxIntent, error: Exception) -> None:
        if intent.effect_type == PUBLISHER_EXECUTE:
            attempted_at = self._outbox.external_call_started_at(intent.effect_id)
            if attempted_at is None:
                return
            action = ApprovedPublicationAction.from_payload(
                dict(intent.payload["action"])
            )
            record = PublicationRecord(
                record_id=f"publication:{action.action_id}:repair-required",
                action_id=action.action_id,
                map_id=action.map_id,
                approval_request_id=str(intent.payload["approval_request_id"]),
                status="repair_required",
                revision=action.revision,
                action=action.action,
                target=action.target,
                occurred_at=attempted_at,
                reason=self._publication_incident_reason(str(error)),
            )
            try:
                self._outbox.enqueue(
                    effect_id=f"tracker-publication:{action.action_id}:repair-required",
                    effect_type=TRACKER_PUBLICATION_RECORD,
                    map_id=intent.map_id,
                    payload={
                        "issue_url": intent.payload["issue_url"],
                        "issue_id": intent.map_id,
                        "project_id": intent.payload["project_id"],
                        "record": record.payload(),
                        "acceptance_report_id": intent.payload["acceptance_report_id"],
                    },
                    created_at=self._synchronized_at(),
                )
            except OutboxConflictError:
                pass
            return
        if not intent.effect_type.startswith("tracker."):
            return
        if getattr(error, "retryable", True) is False:
            return
        binding = self._storage.map_binding(intent.map_id)
        if binding is None:
            return
        project_id = str(binding["project_id"])
        if any(
            source["source"] == "tracker" and source["state"] != "healthy"
            for source in self._storage.project_reachability(project_id)
        ):
            return
        self._mark_tracker_stale(
            project_id=project_id,
            reason=str(error),
        )

    def _mark_tracker_stale(self, *, project_id: str, reason: str) -> None:
        reason = self._executive_safe_tracker_reason(reason)
        self._storage.set_project_reachability(
            project_id=project_id,
            source="tracker",
            state="stale",
            reason=reason,
            changed_at=self._synchronized_at(),
        )

    @staticmethod
    def _executive_safe_tracker_reason(reason: str) -> str:
        normalized = " ".join((reason or "").split()).lower()
        if "unreachable" in normalized or "connection" in normalized:
            return "Tracker authority is unreachable"
        if "timed out" in normalized or "timeout" in normalized:
            return "Tracker authority timed out"
        if any(marker in normalized for marker in ("401", "403", "auth", "credential")):
            return "Tracker authority authentication failed"
        if "rate limit" in normalized:
            return "Tracker authority rate limit is unavailable"
        if "identity changed" in normalized:
            return "Tracker authority identity no longer matches the binding"
        if "conflict" in normalized or "belongs to another" in normalized:
            return "Tracker authority history conflicts with the governance binding"
        return "Tracker authority request failed"

    def _tracker_read(
        self,
        *,
        project_id: str,
        operation: Callable[[], Any],
    ) -> Any:
        """Mark only the affected project stale when authority cannot be read."""
        try:
            return operation()
        except TrackerError as error:
            self._mark_tracker_stale(project_id=project_id, reason=str(error))
            raise

    def board_events(self, *, cursor: int, limit: int = 200) -> dict[str, Any]:
        """Read committed board changes after one durable cursor."""
        return self._board_events.read(cursor=cursor, limit=limit)

    def board_snapshot(self) -> dict[str, Any]:
        """Return one full projection plus a safe cursor for catch-up."""
        cursor = self._board_events.latest_cursor()
        return {**self.board(), "cursor": cursor}

    @property
    def board_stream_settings(self) -> BoardEventSettings:
        """Expose bounded stream settings to the thin transport adapter."""
        return self._event_settings

    def map_detail(self, *, map_id: str) -> dict[str, Any]:
        """Return one Map projection without session-inventory disclosure."""
        for card in self.board()["maps"]:
            if card["id"] == map_id:
                card["recent_decisions"] = self._storage.recent_decisions(map_id=map_id)
                card["approvals"] = self._approval_collection(map_id=map_id)
                card["pm_reports"] = self._storage.recent_pm_reports(map_id=map_id)
                acceptance_report = next(
                    (
                        report
                        for report in card["pm_reports"]
                        if report["type"] == "acceptance"
                        and isinstance(report.get("acceptance"), dict)
                    ),
                    None,
                )
                changes_requested = next(
                    (
                        approval
                        for approval in card["approvals"]["items"]
                        if approval["proposed_action"] == "publish_map"
                        and approval["status"] in {"rejected", "revision"}
                        and acceptance_report is not None
                        and approval["decision_payload"].get("acceptance_report_id")
                        == acceptance_report["record_id"]
                    ),
                    None,
                )
                if changes_requested is not None:
                    card["acceptance_outcome"] = {
                        "state": "changes_requested",
                        "request_id": changes_requested["request_id"],
                        "decision": changes_requested["status"],
                        "requested_changes": changes_requested["decision"]["note"],
                    }
                card["decision_acknowledgments"] = (
                    self._storage.pm_decision_acknowledgments(map_id=map_id)
                )
                card["external_effects"] = self._outbox.map_summary(map_id)
                card["publication"] = self._storage.publication_summary(map_id=map_id)
                if acceptance_report is not None:
                    acceptance = dict(acceptance_report["acceptance"])
                    card["acceptance"] = {
                        **acceptance,
                        "acceptance_evidence_hash": normalized_hash(acceptance),
                        "report": {
                            "id": acceptance_report["record_id"],
                            "tracker_url": acceptance_report["tracker"]["url"],
                        },
                    }
                    approval_binding = self._publication_approval_binding(
                        map_id=map_id,
                        acceptance=acceptance,
                        acceptance_report_id=acceptance_report["record_id"],
                    )
                    if approval_binding is not None:
                        card["publication_approval"] = approval_binding
                return card
        raise MapBindingError(f"Map is not bound: {map_id}")

    def outbox_status(self, *, effect_id: str) -> dict[str, Any]:
        """Return one operator-visible durable execution record."""
        return self._outbox.operator_status(effect_id)

    def cancel_map(
        self,
        *,
        map_id: str,
        actor_identity: GovernanceActorIdentity,
        approval_request_id: str,
        mutation_id: str,
    ) -> dict[str, Any]:
        """Close one exactly approved Map as not planned, never as delivered."""
        binding = self._ensure_map_writable(map_id=map_id)
        self._authorize_chairman_request(
            map_id=map_id,
            action="cancel_map",
            actor_identity=actor_identity,
        )
        if not mutation_id or len(mutation_id) > 128:
            raise ValueError("cancellation mutation_id must be 1 to 128 characters")
        action = "cancel_map"
        scope = {"map_id": map_id}
        payload = {"state_reason": "not_planned"}
        payload_hash = normalized_hash(
            {"action": action, "scope": scope, "payload": payload}
        )
        lock_key = f"{self._storage.database}:{map_id}"
        with _operation_lock("cancellation", lock_key):
            with self._storage.approval_lease(map_id):
                approval = self._current_approval(request_id=approval_request_id)
                existing = self._storage.protected_mutation(mutation_id)
                if approval is None or approval["map_id"] != map_id:
                    self._deny_approval_enforcement(
                        map_id=map_id,
                        action=action,
                        reason="approval_missing",
                        actor_identity=actor_identity,
                    )
                expected_status = "consumed" if existing is not None else "approved"
                if approval["status"] != expected_status:
                    self._deny_approval_enforcement(
                        map_id=map_id,
                        action=action,
                        reason=self._approval_status_reason(approval["status"]),
                        actor_identity=actor_identity,
                    )
                self._validate_approval_action(
                    map_id=map_id,
                    approval=approval,
                    decision_class="cancellation",
                    action=action,
                    scope=scope,
                    payload=payload,
                    actor_identity=actor_identity,
                )
                if existing is not None and (
                    existing["map_id"] != map_id
                    or existing["request_id"] != approval_request_id
                    or existing["action"] != action
                    or normalized_json(existing["scope"]) != normalized_json(scope)
                    or normalized_json(existing["payload"]) != normalized_json(payload)
                    or existing["payload_hash"] != payload_hash
                ):
                    raise ApprovalRequestConflict(
                        request_id=approval_request_id,
                        reason="stable cancellation identity belongs to another payload",
                    )
                close_effect_id = f"tracker-close:{mutation_id}:not-planned"
                existing_intent = self._outbox.intent(close_effect_id)
                current_issue = self._tracker_read(
                    project_id=str(binding["project_id"]),
                    operation=lambda: self._tracker.get_issue(binding["issue_url"]),
                )
                current_stage = self._executive_stage(current_issue)
                if current_stage in {"done", "cancelled"} and existing is None:
                    raise MapTransitionError(
                        current_stage=current_stage,
                        requested_stage="cancelled",
                        reason="terminal Map stages cannot be cancelled again",
                    )
                expected_stage = (
                    str(existing_intent.payload["expected_stage"])
                    if existing_intent is not None
                    else current_stage
                )
                created_at = (
                    str(existing["reserved_at"])
                    if existing is not None
                    else self._synchronized_at()
                )
                close_payload = {
                    "issue_url": binding["issue_url"],
                    "issue_id": map_id,
                    "project_id": binding["project_id"],
                    "state_reason": "not_planned",
                    "expected_stage": expected_stage,
                    "protected_mutation_id": mutation_id,
                    "approval_request_id": approval_request_id,
                    "consumption_event_id": (
                        f"approval:{approval_request_id}:consumed:{mutation_id}"
                    ),
                    "approval_payload_hash": str(approval["payload_hash"]),
                }
                consumption_event = ApprovalHistoryEvent(
                    event_id=(f"approval:{approval_request_id}:consumed:{mutation_id}"),
                    request_id=approval_request_id,
                    event_type="consumed",
                    occurred_at=created_at,
                    payload_hash=str(approval["payload_hash"]),
                    details={
                        "mutation_id": mutation_id,
                        "action": action,
                        "actor_id": actor_identity.actor_id,
                        "actor_profile": actor_identity.profile_name,
                        "note": "Approved cancellation action consumed.",
                    },
                )
                consumption_effect_id = f"tracker-approval:{consumption_event.event_id}"
                try:
                    with self._storage.atomic() as connection:
                        _replay, status = (
                            self._storage.reserve_protected_mutation_in_transaction(
                                connection,
                                mutation_id=mutation_id,
                                map_id=map_id,
                                request_id=approval_request_id,
                                action=action,
                                scope=scope,
                                payload=payload,
                                payload_hash=payload_hash,
                                reserved_at=created_at,
                            )
                        )
                        if status not in {"reserved", "confirmed"}:
                            raise ApprovalEnforcementError(
                                action=action,
                                map_id=map_id,
                                reason=self._approval_status_reason(status),
                            )
                        enqueued = self._outbox.enqueue_in_transaction(
                            connection,
                            effect_id=consumption_effect_id,
                            effect_type=TRACKER_APPROVAL_EVENT,
                            map_id=map_id,
                            payload={
                                "issue_url": binding["issue_url"],
                                "issue_id": map_id,
                                "project_id": binding["project_id"],
                                "event": consumption_event.payload(),
                                "completion": {
                                    "operation": "consumption",
                                    "mutation_id": mutation_id,
                                    "action": action,
                                    "close_effect_id": close_effect_id,
                                    "close_payload": close_payload,
                                },
                            },
                            created_at=created_at,
                        )
                except OutboxConflictError as error:
                    raise ApprovalRequestConflict(
                        request_id=approval_request_id,
                        reason="stable cancellation identity belongs to another payload",
                    ) from error

        self._outbox_dispatcher.dispatch_effect(
            effect_id=consumption_effect_id,
            owner_id=self._outbox_owner_id,
            expedite_retry=not enqueued.created,
        )
        self._require_effect_success(
            consumption_effect_id,
            "Map cancellation authority consumption is not confirmed",
        )
        self._dispatch_if_present(close_effect_id)
        self._require_effect_success(
            close_effect_id,
            "Map cancellation is not confirmed",
        )
        return next(card for card in self.board()["maps"] if card["id"] == map_id)

    def publish_map(
        self,
        *,
        map_id: str,
        approval_request_id: str,
        mutation_id: str,
    ) -> dict[str, Any]:
        """Execute one exact approved action, record evidence, then close done."""
        binding = self._ensure_map_writable(map_id=map_id)
        if self._publisher is None:
            self._storage.save_authorization_denial(
                action="publish_map",
                map_id=map_id,
                profile_name="unavailable-publisher",
                session_id="unavailable-publisher",
                reason="publisher_capability_unavailable",
                denied_at=self._synchronized_at(),
            )
            raise GovernanceAuthorizationError(
                action="publish_map",
                map_id=map_id,
                reason="publisher_capability_unavailable",
            )
        publisher_profile = self._publisher.profile_name
        publisher_authority = self._publisher.authority_ref
        try:
            self._publisher.validate_authority()
        except Exception as error:
            self._storage.save_authorization_denial(
                action="publish_map",
                map_id=map_id,
                profile_name=publisher_profile,
                session_id=publisher_authority,
                reason="publisher_authority_unavailable",
                denied_at=self._synchronized_at(),
            )
            raise GovernanceAuthorizationError(
                action="publish_map",
                map_id=map_id,
                reason="publisher_authority_unavailable",
            ) from error
        if not mutation_id or len(mutation_id) > 128:
            raise ValueError("publication mutation_id must be 1 to 128 characters")
        lock_key = f"{self._storage.database}:{map_id}"
        with _operation_lock("publication", lock_key):
            with self._storage.approval_lease(map_id):
                approval = self._current_approval(request_id=approval_request_id)
                existing = self._storage.protected_mutation(mutation_id)
                if approval is None or approval["map_id"] != map_id:
                    self._deny_approval_enforcement(
                        map_id=map_id,
                        action="publish_map",
                        reason="approval_missing",
                        profile_name=publisher_profile,
                        session_id=publisher_authority,
                    )
                if existing is None and approval["status"] != "approved":
                    self._deny_approval_enforcement(
                        map_id=map_id,
                        action="publish_map",
                        reason=self._approval_status_reason(approval["status"]),
                        profile_name=publisher_profile,
                        session_id=publisher_authority,
                    )
                if existing is not None and approval["status"] != "consumed":
                    self._deny_approval_enforcement(
                        map_id=map_id,
                        action="publish_map",
                        reason=self._approval_status_reason(approval["status"]),
                        profile_name=publisher_profile,
                        session_id=publisher_authority,
                    )
                issue = self._tracker_read(
                    project_id=str(binding["project_id"]),
                    operation=lambda: self._tracker.get_issue(binding["issue_url"]),
                )
                if issue.id != map_id or self._executive_stage(issue) != "acceptance":
                    self._deny_approval_enforcement(
                        map_id=map_id,
                        action="publish_map",
                        reason="publication_acceptance_stage_mismatch",
                        profile_name=publisher_profile,
                        session_id=publisher_authority,
                    )
                incident = self._unresolved_publication_incident(binding=binding)
                if incident is not None:
                    raise PublicationRepairRequired(
                        map_id=map_id,
                        action_id=incident.action_id,
                        reason=incident.reason
                        or "Remote publication requires evidence reconciliation",
                    )
                action = self._approved_publication_action(
                    map_id=map_id,
                    mutation_id=mutation_id,
                    approval=approval,
                )
                try:
                    self._publisher.validate_action(action)
                except Exception:
                    self._deny_approval_enforcement(
                        map_id=map_id,
                        action="publish_map",
                        reason="publication_target_preflight_failed",
                        profile_name=publisher_profile,
                        session_id=publisher_authority,
                    )
                scope = {
                    "map_id": map_id,
                    "publication_target": action.target,
                    "publisher_authority_ref": publisher_authority,
                }
                payload = {
                    "revision": action.revision,
                    "publication_action": action.action,
                    "publication_target": action.target,
                    "publisher_authority_ref": publisher_authority,
                    "acceptance_evidence_hash": approval["decision_payload"].get(
                        "acceptance_evidence_hash"
                    ),
                    "acceptance_report_id": approval["decision_payload"].get(
                        "acceptance_report_id"
                    ),
                }
                payload_hash = normalized_hash(
                    {"action": "publish_map", "scope": scope, "payload": payload}
                )
                if existing is not None and (
                    existing["map_id"] != map_id
                    or existing["request_id"] != approval_request_id
                    or existing["action"] != "publish_map"
                    or normalized_json(existing["scope"]) != normalized_json(scope)
                    or normalized_json(existing["payload"]) != normalized_json(payload)
                    or existing["payload_hash"] != payload_hash
                ):
                    raise ApprovalRequestConflict(
                        request_id=approval_request_id,
                        reason="stable mutation identity belongs to another payload",
                    )
                effect_id = f"publisher:{mutation_id}"
                effect_payload = {
                    "action": action.payload(),
                    "approval_request_id": approval_request_id,
                    "issue_url": binding["issue_url"],
                    "issue_id": map_id,
                    "project_id": binding["project_id"],
                    "acceptance_report_id": approval["decision_payload"][
                        "acceptance_report_id"
                    ],
                    "publisher_authority_ref": publisher_authority,
                }
                created_at = self._synchronized_at()
                try:
                    with self._storage.atomic() as connection:
                        _replay, status = (
                            self._storage.reserve_protected_mutation_in_transaction(
                                connection,
                                mutation_id=mutation_id,
                                map_id=map_id,
                                request_id=approval_request_id,
                                action="publish_map",
                                scope=scope,
                                payload=payload,
                                payload_hash=payload_hash,
                                reserved_at=created_at,
                            )
                        )
                        if status not in {"reserved", "confirmed"}:
                            raise ApprovalEnforcementError(
                                action="publish_map",
                                map_id=map_id,
                                reason=self._approval_status_reason(status),
                            )
                        enqueued = self._outbox.enqueue_in_transaction(
                            connection,
                            effect_id=effect_id,
                            effect_type=PUBLISHER_EXECUTE,
                            map_id=map_id,
                            payload=effect_payload,
                            created_at=created_at,
                        )
                except OutboxConflictError as error:
                    raise ApprovalRequestConflict(
                        request_id=approval_request_id,
                        reason="stable publication identity belongs to another payload",
                    ) from error

        self._outbox_dispatcher.dispatch_effect(
            effect_id=effect_id,
            owner_id=self._outbox_owner_id,
            expedite_retry=not enqueued.created,
        )
        publisher_intent = self._outbox.intent(effect_id)
        if publisher_intent is not None and publisher_intent.state == "terminal":
            if not self._outbox.external_call_started(effect_id):
                raise ApprovalEnforcementError(
                    action="publish_map",
                    map_id=map_id,
                    reason=(
                        publisher_intent.last_error_message
                        or "publication_preflight_failed"
                    ),
                )
            incident_effect_id = f"tracker-publication:{mutation_id}:repair-required"
            self._dispatch_if_present(incident_effect_id)
            self._require_effect_success(
                incident_effect_id,
                "Publication repair incident is not tracker-confirmed",
            )
            raise PublicationRepairRequired(
                map_id=map_id,
                action_id=mutation_id,
                reason=self._publication_incident_reason(
                    publisher_intent.last_error_message
                    or "Remote publication could not be safely confirmed"
                ),
            )
        if (
            publisher_intent is not None
            and publisher_intent.state == "succeeded"
            and isinstance(publisher_intent.acknowledgment, dict)
            and publisher_intent.acknowledgment.get("outcome") == "confirmed_absent"
        ):
            raise ApprovalEnforcementError(
                action="publish_map",
                map_id=map_id,
                reason="publication_aborted_grant_consumed",
            )
        self._require_effect_success(effect_id, "Remote publication is not confirmed")
        self._dispatch_if_present(f"tracker-publication:{mutation_id}:succeeded")
        self._dispatch_if_present(f"tracker-close:{mutation_id}:completed")
        self._require_effect_success(
            f"tracker-publication:{mutation_id}:succeeded",
            "Remote publication evidence is not tracker-confirmed",
        )
        self._require_effect_success(
            f"tracker-close:{mutation_id}:completed",
            "Map closeout is not tracker-confirmed",
        )
        return next(card for card in self.board()["maps"] if card["id"] == map_id)

    def reconcile_publication(
        self,
        *,
        map_id: str,
        action_id: str,
    ) -> dict[str, Any]:
        """Resolve an uncertain publication exclusively through provider readback."""
        if self._publisher is None:
            raise GovernanceAuthorizationError(
                action="reconcile_publication",
                map_id=map_id,
                reason="publisher_capability_unavailable",
            )
        self._ensure_map_writable(map_id=map_id)
        effect_id = f"publisher:{action_id}"
        intent = self._outbox.intent(effect_id)
        if (
            intent is None
            or intent.map_id != map_id
            or intent.effect_type != PUBLISHER_EXECUTE
        ):
            raise ValueError(
                "Publication action is not durably registered for this Map"
            )
        if intent.state == "succeeded":
            outcome = intent.acknowledgment or {}
            self._confirm_publication_incident_if_present(action_id=action_id)
            if outcome.get("outcome") == "confirmed_absent":
                aborted_effect_id = f"tracker-publication:{action_id}:aborted"
                self._dispatch_if_present(aborted_effect_id)
                self._require_effect_success(
                    aborted_effect_id,
                    "Confirmed-absent publication record is not tracker-confirmed",
                )
                return self.map_detail(map_id=map_id)
            self._dispatch_if_present(f"tracker-publication:{action_id}:succeeded")
            self._dispatch_if_present(f"tracker-close:{action_id}:completed")
            return self.map_detail(map_id=map_id)
        if intent.state != "terminal":
            raise ValueError(
                "Publication action is not terminal and cannot use evidence repair"
            )
        if not self._outbox.external_call_started(effect_id):
            raise ValueError(
                "Publication never crossed the remote-call boundary; use explicit "
                "Outbox repair after correcting the failed preflight"
            )
        incident_effect_id = f"tracker-publication:{action_id}:repair-required"
        self._dispatch_if_present(incident_effect_id)
        self._require_effect_success(
            incident_effect_id,
            "Publication repair incident must be tracker-confirmed before resolution",
        )
        action = ApprovedPublicationAction.from_payload(dict(intent.payload["action"]))
        try:
            evidence = self._publisher.readback(action)
        except Exception as error:
            raise PublicationRepairRequired(
                map_id=map_id,
                action_id=action_id,
                reason=self._publication_incident_reason(str(error)),
            ) from error
        resolved_at = self._synchronized_at()
        if evidence is None:
            try:
                confirmed_absent = self._publisher.confirms_absence(action)
            except Exception as error:
                raise PublicationRepairRequired(
                    map_id=map_id,
                    action_id=action_id,
                    reason=self._publication_incident_reason(str(error)),
                ) from error
            if not confirmed_absent:
                raise PublicationRepairRequired(
                    map_id=map_id,
                    action_id=action_id,
                    reason=(
                        "Provider state conflicts with or cannot prove absence of "
                        "the approved remote action"
                    ),
                )
            attempted_at = self._outbox.external_call_started_at(effect_id)
            if attempted_at is None:  # pragma: no cover - marker checked above
                raise RuntimeError("Publisher attempt timestamp disappeared")
            record = PublicationRecord(
                record_id=f"publication:{action.action_id}:aborted",
                action_id=action.action_id,
                map_id=action.map_id,
                approval_request_id=str(intent.payload["approval_request_id"]),
                status="aborted",
                revision=action.revision,
                action=action.action,
                target=action.target,
                occurred_at=attempted_at,
                reason=(
                    "Provider readback confirmed the approved remote action is "
                    "absent; the consumed grant will not be replayed."
                ),
            )
            acknowledgment = {
                "outcome": "confirmed_absent",
                "action": action.payload(),
            }
            resolution_id = f"publication-absence:{action_id}"
            note = (
                "Privileged provider readback proved the exact approved action absent."
            )
        else:
            record = self._successful_publication_record(intent, evidence)
            acknowledgment = {"evidence": evidence.payload()}
            resolution_id = f"publication-evidence:{action_id}"
            note = "Privileged provider readback proved the exact approved action."
        successor = self._publication_record_successor(intent, record)
        self._outbox.acknowledge_terminal_readback(
            effect_id=effect_id,
            resolution_id=resolution_id,
            note=note,
            acknowledgment=acknowledgment,
            resolved_at=resolved_at,
            successor_effect_id=str(successor["effect_id"]),
            successor_effect_type=TRACKER_PUBLICATION_RECORD,
            successor_map_id=intent.map_id,
            successor_payload=dict(successor["payload"]),
        )
        publication_effect_id = str(successor["effect_id"])
        self._dispatch_if_present(publication_effect_id)
        self._require_effect_success(
            publication_effect_id,
            "Reconciled publication evidence is not tracker-confirmed",
        )
        if record.status == "aborted":
            return self.map_detail(map_id=map_id)
        close_effect_id = f"tracker-close:{action_id}:completed"
        self._dispatch_if_present(close_effect_id)
        close = self._outbox.intent(close_effect_id)
        if close is not None and close.state == "succeeded":
            return next(card for card in self.board()["maps"] if card["id"] == map_id)
        return self.map_detail(map_id=map_id)

    def _unresolved_publication_incident(
        self, *, binding: Mapping[str, Any]
    ) -> PublicationRecord | None:
        records = self._tracker_read(
            project_id=str(binding["project_id"]),
            operation=lambda: self._tracker.list_publication_records(
                str(binding["issue_url"])
            ),
        )
        for index, item in enumerate(records):
            if item.record.status != "repair_required":
                continue
            resolved_later = any(
                later.record.action_id == item.record.action_id
                and later.record.status in {"succeeded", "aborted"}
                for later in records[index + 1 :]
            )
            if not resolved_later:
                return item.record
        return None

    def _confirm_publication_incident_if_present(self, *, action_id: str) -> None:
        incident_effect_id = f"tracker-publication:{action_id}:repair-required"
        if self._outbox.intent(incident_effect_id) is None:
            return
        self._dispatch_if_present(incident_effect_id)
        self._require_effect_success(
            incident_effect_id,
            "Publication repair incident must be tracker-confirmed before resolution",
        )

    def _approved_publication_action(
        self,
        *,
        map_id: str,
        mutation_id: str,
        approval: Mapping[str, Any],
    ) -> ApprovedPublicationAction:
        acceptance_report = next(
            (
                report
                for report in self._storage.recent_pm_reports(map_id=map_id)
                if report["type"] == "acceptance"
                and isinstance(report.get("acceptance"), dict)
            ),
            None,
        )
        acceptance = (
            dict(acceptance_report["acceptance"])
            if acceptance_report is not None
            else None
        )
        requested = (
            acceptance.get("requested_publication_action")
            if isinstance(acceptance, dict)
            else None
        )
        decision_payload = approval["decision_payload"]
        if (
            approval["decision_class"] != "remote_publication"
            or approval["proposed_action"] != "publish_map"
            or acceptance is None
            or not isinstance(requested, dict)
            or decision_payload.get("acceptance_evidence_hash")
            != normalized_hash(acceptance)
            or decision_payload.get("acceptance_report_id")
            != (
                acceptance_report["record_id"]
                if acceptance_report is not None
                else None
            )
            or decision_payload.get("revision") != acceptance.get("revision")
            or decision_payload.get("publication_action") != requested.get("action")
            or self._publisher is None
            or self._publication_authority_ref != self._publisher.authority_ref
            or decision_payload.get("publisher_authority_ref")
            != self._publisher.authority_ref
            or normalized_json(decision_payload.get("publication_target"))
            != normalized_json(requested.get("target"))
        ):
            self._deny_approval_enforcement(
                map_id=map_id,
                action="publish_map",
                reason="publication_acceptance_mismatch",
                profile_name=(
                    self._publisher.profile_name if self._publisher else "unavailable"
                ),
                session_id=(
                    self._publisher.authority_ref if self._publisher else "unavailable"
                ),
            )
        return ApprovedPublicationAction(
            action_id=mutation_id,
            map_id=map_id,
            revision=str(decision_payload["revision"]),
            action=str(decision_payload["publication_action"]),
            target=dict(decision_payload["publication_target"]),
        )

    @staticmethod
    def _publication_incident_reason(reason: str) -> str:
        normalized = " ".join(reason.split())[:1000]
        if re.search(
            r"(?i)(credential|secret|password|access[_ -]?token|github_pat_|ghp_)",
            normalized,
        ):
            return (
                "Privileged publisher reported a partial remote failure; inspect "
                "the isolated publisher audit boundary."
            )
        return normalized or "Remote publication could not be safely confirmed"

    def _dispatch_if_present(self, effect_id: str) -> None:
        intent = self._outbox.intent(effect_id)
        if intent is None or intent.state == "succeeded":
            return
        self._outbox_dispatcher.dispatch_effect(
            effect_id=effect_id,
            owner_id=self._outbox_owner_id,
            expedite_retry=True,
        )

    def _require_effect_success(self, effect_id: str, message: str) -> None:
        intent = self._outbox.intent(effect_id)
        if intent is None or intent.state != "succeeded":
            raise TrackerError(
                (intent.last_error_message if intent is not None else None) or message
            )

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
        acceptance_decisions = self._recover_publication_acceptance_decisions()
        return {
            "processed_count": len(outcomes),
            "outcomes": outcomes,
            "acceptance_decisions": acceptance_decisions,
        }

    def _recover_publication_acceptance_decisions(self) -> list[dict[str, Any]]:
        """Derive the rejection/revision delivery handoff from durable authority."""
        _projects, maps = self._storage.board_rows()
        recovered: list[dict[str, Any]] = []
        for card in maps:
            map_id = str(card["map_id"])
            for row in reversed(self._storage.approvals(map_id=map_id)):
                if row.get("proposed_action") != "publish_map" or row.get(
                    "status"
                ) not in {"rejected", "revision"}:
                    continue
                result = self._complete_publication_acceptance_decision(
                    result={
                        "map_id": map_id,
                        "approval": self._approval_projection(row),
                        "idempotent": True,
                    },
                    row=row,
                )
                recovered.append(
                    {
                        "map_id": map_id,
                        "request_id": row["request_id"],
                        "decision": row["status"],
                        "resumed": "resume" in result,
                    }
                )
        return recovered

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
        intent = self._outbox.intent(effect_id)
        if (
            intent is not None
            and intent.effect_type == PUBLISHER_EXECUTE
            and self._outbox.external_call_started(effect_id)
        ):
            raise ValueError(
                "Publisher effects cannot be re-executed by generic Outbox repair; "
                "use evidence-only publication reconciliation"
            )
        if intent is not None and self._storage.map_binding(intent.map_id) is not None:
            self._ensure_map_writable(map_id=intent.map_id)
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

    def commission_map(
        self,
        *,
        map_id: str,
        request_identity: GovernanceRequestIdentity,
    ) -> dict[str, Any]:
        """Commission or resume the Map's one plugin-owned Hermes PM root."""
        self._authorize_ceo_request(
            map_id=map_id,
            action="commission",
            request_identity=request_identity,
        )
        binding = self._ensure_map_writable(map_id=map_id)
        if (
            self._commissioning_prerequisites is None
            or self._coordinator_runtime is None
        ):
            raise RuntimeError("PM commissioning runtime is not configured")
        lock_key = f"{self._storage.database}:{map_id}"
        with _operation_lock("commission", lock_key):
            with self._storage.commission_lease(map_id):
                return self._commission_map(
                    map_id=map_id,
                    binding=binding,
                )

    def _commission_map(
        self,
        *,
        map_id: str,
        binding: dict[str, Any],
    ) -> dict[str, Any]:
        prerequisites = self._commissioning_prerequisites
        coordinator_runtime = self._coordinator_runtime
        if prerequisites is None or coordinator_runtime is None:
            raise RuntimeError("PM commissioning runtime is not configured")
        issue = self._tracker_read(
            project_id=str(binding["project_id"]),
            operation=lambda: self._tracker.get_issue(str(binding["issue_url"])),
        )
        if issue.id != map_id:
            raise MapBindingError("Bound GitHub Issue identity changed")
        authorization = self._commissioning_authorization(map_id=map_id)
        current_stage = self._executive_stage(issue)
        current_runtime = coordinator_runtime.status(map_id=map_id)
        if current_stage == "delivery" and current_runtime.get("ready_record_id"):
            context = prerequisites.commissioning_context(
                project_id=str(binding["project_id"]),
                repository=issue.repository,
            )
            runtime = coordinator_runtime.ensure_root(
                RootRuntimeRequest(
                    map_id=map_id,
                    map_url=issue.url,
                    context=context,
                )
            )
            try:
                self._bind_pm_runtime_identity(
                    map_id=map_id,
                    runtime=runtime,
                    context=context,
                )
            except (
                GovernanceAuthorizationError,
                OSError,
                RuntimeError,
                ValueError,
                sqlite3.Error,
            ):
                return self._pm_handoff_failure(
                    coordinator_runtime=coordinator_runtime,
                    map_id=map_id,
                )
            record_id = str(runtime.get("ready_record_id") or "")
            if not record_id:
                runtime = coordinator_runtime.record_failure(
                    map_id=map_id,
                    reason="delivery_runtime_checkpoint_missing",
                    retryable=False,
                    repair_required=True,
                )
                return {
                    **self._runtime_projection(runtime),
                    "checkpoint": {"state": "runtime_verification_failed"},
                    "idempotent": False,
                }
            if runtime.get("state") != "active":
                runtime = coordinator_runtime.activate(
                    map_id=map_id,
                    record_id=record_id,
                )
            return {
                **self._runtime_projection(runtime),
                "checkpoint": {
                    "state": "tracker_confirmed",
                    "record_id": record_id,
                },
                "idempotent": True,
            }
        if current_stage == "delivery":
            runtime = (
                coordinator_runtime.record_failure(
                    map_id=map_id,
                    reason="delivery_runtime_inconsistent",
                    retryable=False,
                    repair_required=True,
                )
                if current_runtime.get("state") != "not_commissioned"
                else {
                    "map_id": map_id,
                    "state": "repair_required",
                    "failure": {
                        "reason": "delivery_runtime_inconsistent",
                        "retryable": False,
                        "repair_required": True,
                        "resource_disposition": (
                            "no_cleanup_without_verified_ownership"
                        ),
                    },
                }
            )
            return {
                **self._runtime_projection(runtime),
                "checkpoint": {"state": "runtime_verification_failed"},
                "idempotent": False,
            }
        if current_stage != "authorized":
            raise CommissioningAuthorizationError(
                map_id=map_id,
                reason="map_not_authorized",
            )
        context = prerequisites.commissioning_context(
            project_id=str(binding["project_id"]),
            repository=issue.repository,
        )
        project = self._storage.project_binding(str(binding["project_id"]))
        if project is None:
            raise MapBindingError(
                f"CEO project is not configured: {binding['project_id']}"
            )
        if (
            context.project_id != binding["project_id"]
            or context.project_url != str(project["project_url"])
            or context.repository.casefold() != issue.repository.casefold()
        ):
            raise CommissioningAuthorizationError(
                map_id=map_id,
                reason="commissioning_coordinate_mismatch",
            )
        runtime = coordinator_runtime.ensure_root(
            RootRuntimeRequest(
                map_id=map_id,
                map_url=issue.url,
                context=context,
            )
        )
        try:
            pm_identity, coordinator_id = self._bind_pm_runtime_identity(
                map_id=map_id,
                runtime=runtime,
                context=context,
            )
        except (
            GovernanceAuthorizationError,
            OSError,
            RuntimeError,
            ValueError,
            sqlite3.Error,
        ):
            return self._pm_handoff_failure(
                coordinator_runtime=coordinator_runtime,
                map_id=map_id,
            )
        ready_draft = self._commissioning_ready_draft(
            map_id=map_id,
            timestamp=str(runtime["commissioned_at"]),
        )
        try:
            ready_record = self._tracker_ready_record(
                binding=binding,
                report=ready_draft,
            )
        except TrackerError:
            return self._commissioning_tracker_failure(
                coordinator_runtime=coordinator_runtime,
                map_id=map_id,
                record_id=ready_draft.record_id,
            )
        if ready_record is None:
            try:
                with self._storage.pm_turn_lease(map_id):
                    self._reserve_pm_turn(
                        map_id=map_id,
                        request_identity=pm_identity,
                        coordinator_id=coordinator_id,
                        turn_id=f"commission-ready:{map_id}",
                    )
            except (
                GovernanceAuthorizationError,
                RuntimeError,
                ValueError,
                sqlite3.Error,
            ):
                runtime = coordinator_runtime.record_failure(
                    map_id=map_id,
                    reason="pm_ready_turn_conflict",
                    retryable=False,
                    repair_required=True,
                )
                return {
                    **self._runtime_projection(runtime),
                    "checkpoint": {"state": "runtime_verification_failed"},
                    "idempotent": False,
                }
            runtime = coordinator_runtime.prompt_ready(
                map_id=map_id,
                payload=self._commissioning_payload(
                    issue=issue,
                    context=context,
                    report=ready_draft,
                    authorization=authorization,
                ),
            )
            if runtime.get("state") == "repair_required":
                return {
                    **self._runtime_projection(runtime),
                    "checkpoint": {
                        "state": "runtime_verification_failed",
                        "record_id": ready_draft.record_id,
                    },
                    "idempotent": False,
                }
            try:
                ready_record = self._tracker_ready_record(
                    binding=binding,
                    report=ready_draft,
                )
            except TrackerError:
                return self._commissioning_tracker_failure(
                    coordinator_runtime=coordinator_runtime,
                    map_id=map_id,
                    record_id=ready_draft.record_id,
                )
        if ready_record is None:
            return {
                **self._runtime_projection(runtime),
                "checkpoint": {
                    "state": "tracker_unconfirmed",
                    "record_id": ready_draft.record_id,
                },
                "idempotent": False,
            }
        self._storage.save_pm_report_projection(
            map_id=map_id,
            report=self._pm_report_projection(
                ready_record,
                confirmed_at=self._synchronized_at(),
            ),
        )
        try:
            with self._storage.pm_turn_lease(map_id):
                self._storage.finish_pm_turn(
                    map_id=map_id,
                    turn_id=f"commission-ready:{map_id}",
                    outcome="report",
                    outcome_id=ready_draft.record_id,
                    finished_at=self._synchronized_at(),
                )
        except (ValueError, sqlite3.Error):
            runtime = coordinator_runtime.record_failure(
                map_id=map_id,
                reason="pm_ready_turn_completion_conflict",
                retryable=False,
                repair_required=True,
            )
            return {
                **self._runtime_projection(runtime),
                "checkpoint": {
                    "state": "runtime_verification_failed",
                    "record_id": ready_draft.record_id,
                },
                "idempotent": False,
            }
        runtime = coordinator_runtime.confirm_ready(
            map_id=map_id,
            record_id=ready_draft.record_id,
        )
        try:
            self._transition_commissioned_delivery(
                map_id=map_id,
                ready_record_id=ready_draft.record_id,
                mutation_id=self._commissioning_transition_id(map_id),
            )
        except (MapTransitionError, TrackerError):
            runtime = coordinator_runtime.record_failure(
                map_id=map_id,
                reason="tracker_delivery_transition_pending",
                retryable=True,
            )
            return {
                **self._runtime_projection(runtime),
                "checkpoint": {
                    "state": "tracker_confirmed",
                    "record_id": ready_draft.record_id,
                },
                "idempotent": False,
            }
        runtime = coordinator_runtime.activate(
            map_id=map_id,
            record_id=ready_draft.record_id,
        )
        return {
            **self._runtime_projection(runtime),
            "checkpoint": {
                "state": "tracker_confirmed",
                "record_id": ready_draft.record_id,
            },
            "idempotent": False,
        }

    def runtime_status(
        self,
        *,
        map_id: str,
        request_identity: GovernanceRequestIdentity,
    ) -> dict[str, Any]:
        """Return executive-safe commission/resume and repair evidence."""
        self._authorize_ceo_request(
            map_id=map_id,
            action="runtime_status",
            request_identity=request_identity,
        )
        if self._coordinator_runtime is None:
            return {"map_id": map_id, "state": "not_commissioned"}
        return self._runtime_projection(self._coordinator_runtime.status(map_id=map_id))

    def accepts_pm_control_binding(
        self,
        *,
        request_identity: GovernanceRequestIdentity,
        map_id: str,
        coordinator_id: str,
    ) -> bool:
        """Confirm a PM-profile handoff against the authoritative CEO registry."""
        assignment = self._storage.pm_assignment_for_request(
            profile_name=request_identity.profile_name,
            session_id=request_identity.session_id,
        )
        return bool(
            assignment is not None
            and assignment["map_id"] == map_id
            and assignment["coordinator_id"] == coordinator_id
        )

    def _register_pm_control_plane(
        self,
        *,
        map_id: str,
        request_identity: GovernanceRequestIdentity,
        coordinator_id: str,
        context: Any,
    ) -> None:
        control_profile = str(context.ceo_profile or "")
        pm_storage_root = str(context.pm_storage_root or "")
        if (
            not self._profile_name
            or control_profile != self._profile_name
            or not pm_storage_root
            or not Path(pm_storage_root).is_absolute()
        ):
            raise ValueError("PM profile handoff coordinates are unavailable")
        PluginStorage(Path(pm_storage_root)).save_pm_control_plane_binding(
            profile_name=request_identity.profile_name,
            session_id=request_identity.session_id,
            map_id=map_id,
            control_profile=control_profile,
            coordinator_id=coordinator_id,
            registered_at=self._synchronized_at(),
        )

    def _bind_pm_runtime_identity(
        self,
        *,
        map_id: str,
        runtime: dict[str, Any],
        context: Any,
    ) -> tuple[GovernanceRequestIdentity, str]:
        identity = GovernanceRequestIdentity(
            profile_name=context.pm_profile,
            session_id=str(runtime["agent_session_id"]),
        )
        coordinator_id = str(runtime["lifecycle_id"])
        self.assign_pm(
            map_id=map_id,
            request_identity=identity,
            coordinator_id=coordinator_id,
        )
        self._register_pm_control_plane(
            map_id=map_id,
            request_identity=identity,
            coordinator_id=coordinator_id,
            context=context,
        )
        return identity, coordinator_id

    def _pm_handoff_failure(
        self,
        *,
        coordinator_runtime: CoordinatorRuntimeBoundary,
        map_id: str,
    ) -> dict[str, Any]:
        runtime = coordinator_runtime.record_failure(
            map_id=map_id,
            reason="pm_profile_handoff_unavailable",
            retryable=False,
            repair_required=True,
        )
        return {
            **self._runtime_projection(runtime),
            "checkpoint": {"state": "runtime_verification_failed"},
            "idempotent": False,
        }

    def _commissioning_authorization(self, *, map_id: str) -> dict[str, Any]:
        expected_scope = normalized_json({"map_id": map_id})
        expected_payload = normalized_json(
            {
                "expected_stage": "awaiting-approval",
                "requested_stage": "authorized",
            }
        )
        for approval in reversed(self._storage.approvals(map_id=map_id)):
            mutation_id = approval.get("consumed_by_mutation_id")
            if (
                approval["status"] != "consumed"
                or approval["decision_class"] != "delivery_authorization"
                or approval["proposed_action"] != "transition_map"
                or normalized_json(approval["requested_scope"]) != expected_scope
                or normalized_json(approval["decision_payload"]) != expected_payload
                or not mutation_id
            ):
                continue
            mutation = self._storage.protected_mutation(str(mutation_id))
            if mutation is not None and mutation["status"] == "confirmed":
                if self._approval_revocation_unfinished(
                    map_id=map_id,
                    request_id=str(approval["request_id"]),
                ):
                    break
                return {
                    "request_id": str(approval["request_id"]),
                    "mutation_id": str(mutation_id),
                    "decision_class": "delivery_authorization",
                }
        raise CommissioningAuthorizationError(
            map_id=map_id,
            reason="delivery_authorization_missing",
        )

    @staticmethod
    def _commissioning_ready_draft(*, map_id: str, timestamp: str) -> PMReportDraft:
        record_id = f"commission-ready-{map_id}"
        if len(record_id) > 128:
            record_id = (
                "commission-ready-" + hashlib.sha256(map_id.encode()).hexdigest()
            )
        return PMReportDraft(
            record_id=record_id,
            report_type="checkpoint",
            summary="Hermes PM runtime is ready for governed delivery.",
            timestamp=timestamp,
        )

    def _tracker_ready_record(
        self,
        *,
        binding: dict[str, Any],
        report: PMReportDraft,
    ) -> TrackerPMReportRecord | None:
        list_reports = getattr(self._tracker, "list_pm_reports", None)
        if not callable(list_reports):
            return None
        records = self._tracker_read(
            project_id=str(binding["project_id"]),
            operation=lambda: list_reports(str(binding["issue_url"])),
        )
        expected = report.assign_to(str(binding["map_id"]))
        return next((record for record in records if record.report == expected), None)

    @staticmethod
    def _commissioning_tracker_failure(
        *,
        coordinator_runtime: CoordinatorRuntimeBoundary,
        map_id: str,
        record_id: str,
    ) -> dict[str, Any]:
        runtime = coordinator_runtime.record_failure(
            map_id=map_id,
            reason="tracker_ready_confirmation_unavailable",
            retryable=True,
        )
        return {
            **MapGovernanceApplication._runtime_projection(runtime),
            "checkpoint": {
                "state": "tracker_confirmation_unavailable",
                "record_id": record_id,
            },
            "idempotent": False,
        }

    @staticmethod
    def _commissioning_payload(
        *,
        issue: TrackerIssue,
        context: Any,
        report: PMReportDraft,
        authorization: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "protocol": "map-governance/pm-commission-v1",
            "action": "submit_ready_checkpoint_and_wait",
            "map": {"id": issue.id, "url": issue.url},
            "project": {"id": context.project_id, "url": context.project_url},
            "repository": {
                "coordinate": context.repository,
                "path": context.repository_path,
            },
            "profile": context.pm_profile,
            "skills": list(context.skills),
            "routing_policy": context.routing_policy,
            "governance": {
                "stage": "authorized",
                "authority": {
                    "decision_class": authorization["decision_class"],
                    "request_id": authorization["request_id"],
                },
                "constraints": [
                    "Do not dispatch implementation work yet.",
                    "Do not publish remote changes.",
                    "Submit exactly the ready checkpoint, then wait.",
                ],
            },
            "ready_checkpoint": report.payload(),
        }

    @staticmethod
    def _commissioning_transition_id(map_id: str) -> str:
        return "commission-delivery:" + hashlib.sha256(map_id.encode()).hexdigest()[:32]

    @staticmethod
    def _runtime_projection(record: dict[str, Any]) -> dict[str, Any]:
        projection = {
            "map_id": record.get("map_id"),
            "state": record.get("state", "not_commissioned"),
            "profile": record.get("pm_profile"),
            "repository": record.get("repository"),
            "session_id": record.get("session_namespace"),
            "workspace_id": record.get("workspace_id"),
            "window_id": record.get("window_id"),
            "pane_id": record.get("pane_id"),
            "agent_id": record.get("agent_id"),
            "lifecycle_id": record.get("lifecycle_id"),
            "ready_record_id": record.get("ready_record_id"),
            "failure": record.get("failure"),
        }
        return {key: value for key, value in projection.items() if value is not None}

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
        self._ensure_map_writable(map_id=map_id)
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
        self._ensure_map_writable(map_id=map_id)
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
        assignment, normalized_turn_id = self._validate_pm_turn_start(
            map_id=map_id,
            request_identity=request_identity,
            coordinator_id=coordinator_id,
            turn_id=turn_id,
        )

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
                        "content": (
                            "Resume the assigned PM turn. Re-read authoritative Map "
                            "state before continuing."
                        ),
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
            turn_id=normalized_turn_id,
            started_at=self._synchronized_at(),
        )
        assignment = self._storage.pm_assignment(map_id)
        return {
            "coordinator": self._pm_assignment_projection(assignment),
            "idempotent": idempotent,
        }

    def _validate_pm_turn_start(
        self,
        *,
        map_id: str,
        request_identity: GovernanceRequestIdentity,
        coordinator_id: str,
        turn_id: str,
    ) -> tuple[dict[str, Any], str]:
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
        return assignment, normalized_turn_id

    def _reserve_pm_turn(
        self,
        *,
        map_id: str,
        request_identity: GovernanceRequestIdentity,
        coordinator_id: str,
        turn_id: str,
    ) -> bool:
        _, normalized_turn_id = self._validate_pm_turn_start(
            map_id=map_id,
            request_identity=request_identity,
            coordinator_id=coordinator_id,
            turn_id=turn_id,
        )
        idempotent = self._storage.begin_pm_turn(
            map_id=map_id,
            coordinator_id=coordinator_id,
            turn_id=normalized_turn_id,
            started_at=self._synchronized_at(),
        )
        return idempotent

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
        self._ensure_map_writable(map_id=map_id)
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

    def dispatch_pm_delivery_lane(
        self,
        *,
        request_identity: GovernanceRequestIdentity,
        lane: DeliveryLaneSpec,
    ) -> dict[str, Any]:
        """Hand one delivery-pipeline lane to the bounded Herdr runtime."""
        if not isinstance(lane, DeliveryLaneSpec) or not lane.ticket_url:
            raise TypeError("A delivery lane with a ticket URL is required")
        lock_key = f"{self._storage.database}:{lane.ticket_url}"
        with _operation_lock("delivery-lane", lock_key):
            with self._storage.delivery_lane_lease(lane.ticket_url):
                return self._dispatch_pm_delivery_lane(
                    request_identity=request_identity,
                    lane=lane,
                )

    def _dispatch_pm_delivery_lane(
        self,
        *,
        request_identity: GovernanceRequestIdentity,
        lane: DeliveryLaneSpec,
    ) -> dict[str, Any]:
        """Dispatch after winning the ticket-scoped authoritative reread lease."""
        assignment = self._storage.pm_assignment_for_request(
            profile_name=request_identity.profile_name,
            session_id=request_identity.session_id,
        )
        if assignment is None:
            self._deny_governance_request(
                map_id="unassigned",
                action="pm:dispatch_lane",
                request_identity=request_identity,
                reason="pm_assignment_missing",
            )
        map_id = str(assignment["map_id"])
        self._ensure_map_writable(map_id=map_id)
        if assignment["state"] != "active" or not assignment.get("active_turn_id"):
            raise CoordinatorRuntimeError(
                reason="delivery_lane_ownership_conflict",
                retryable=True,
            )
        if (
            self._commissioning_prerequisites is None
            or self._coordinator_runtime is None
        ):
            raise RuntimeError("PM delivery runtime is not configured")
        binding = self._storage.map_binding(map_id)
        if binding is None:  # pragma: no cover - assignment references a bound Map
            raise MapBindingError(f"Map is not bound: {map_id}")
        issue = self._tracker_read(
            project_id=str(binding["project_id"]),
            operation=lambda: self._tracker.get_issue(str(binding["issue_url"])),
        )
        if issue.id != map_id or self._executive_stage(issue) != "delivery":
            raise ValueError("PM delivery dispatch requires a Map in delivery")
        try:
            context = self._commissioning_prerequisites.commissioning_context(
                project_id=str(binding["project_id"]),
                repository=issue.repository,
            )
            ticket = self._delivery_ticket(
                project_id=str(binding["project_id"]),
                lane=lane,
                map_issue=issue,
            )
            existing_registry = self._latest_delivery_lane_registry(
                project_id=str(binding["project_id"]),
                ticket_url=lane.ticket_url,
                lane=lane,
            )
            routing = self._delivery_worker_routing(
                ticket=ticket,
                lane=lane,
                context=context,
                existing_registry=existing_registry,
            )
            integration_expected_head, integration_predecessor_commits = (
                self._delivery_integration_frontier(
                    project_id=str(binding["project_id"]),
                    lane=lane,
                    repository=issue.repository,
                    require_all=False,
                )
            )
            runtime_request = DeliveryRuntimeRequest(
                map_id=map_id,
                map_url=issue.url,
                context=context,
                lane=lane,
                registry_timestamp=str(assignment["updated_at"]),
                integration_expected_head=integration_expected_head,
                integration_predecessor_commits=integration_predecessor_commits,
            )
            try:
                if existing_registry is None:
                    prepared = self._coordinator_runtime.prepare_lane(runtime_request)
                    self._validate_delivery_prepare_outcome(
                        outcome=prepared,
                        request=runtime_request,
                    )
                    existing_registry = DeliveryLaneRegistry.from_payload(
                        prepared.get("registry", {})
                    )
                    if existing_registry.dispatch_id != delivery_dispatch_id(
                        runtime_request
                    ):
                        raise RuntimeError(
                            "Delivery lane registry conflicts with the dispatch identity"
                        )
                    self._validate_application_lane_registry(
                        lane=lane,
                        registry=existing_registry,
                        allowed_states={"created"},
                    )
                    self._confirm_delivery_lane_registry(
                        project_id=str(binding["project_id"]),
                        ticket=ticket,
                        lane=lane,
                        registry=existing_registry,
                    )
                else:
                    self._validate_application_lane_registry(
                        lane=lane,
                        registry=existing_registry,
                        allowed_states={"created", "running", "blocked"},
                    )
                runtime_request = replace(runtime_request, registry=existing_registry)
                outcome = self._coordinator_runtime.dispatch_lane(runtime_request)
            except CoordinatorRuntimeError as error:
                if error.reason in {
                    "delivery_base_mismatch",
                    "overlapping_integration_ownership",
                }:
                    refreshed_frontier = self._delivery_integration_frontier(
                        project_id=str(binding["project_id"]),
                        lane=lane,
                        repository=issue.repository,
                        require_all=False,
                    )
                    if refreshed_frontier != (
                        integration_expected_head,
                        integration_predecessor_commits,
                    ):
                        raise CoordinatorRuntimeError(
                            reason="integration_frontier_stale",
                            retryable=True,
                        ) from error
                raise
            self._validate_delivery_dispatch_outcome(
                outcome=outcome,
                request=runtime_request,
            )
            registry = DeliveryLaneRegistry.from_payload(outcome.get("registry", {}))
            if registry.dispatch_id not in {
                None,
                delivery_confirmed_dispatch_id(runtime_request),
            }:
                raise RuntimeError(
                    "Delivery lane registry conflicts with the dispatch identity"
                )
            self._validate_application_lane_registry(
                lane=lane,
                registry=registry,
                allowed_states={"running"},
            )
            self._confirm_delivery_lane_registry(
                project_id=str(binding["project_id"]),
                ticket=ticket,
                lane=lane,
                registry=registry,
            )
        except CommissioningPrerequisiteError as error:
            record_id = (
                "delivery-blocked-"
                + hashlib.sha256(
                    (
                        f"{lane.lane_id}:{error.reason}:{assignment['active_turn_id']}"
                    ).encode()
                ).hexdigest()[:32]
            )
            missing_integration = error.reason == "supported_worker_integration_missing"
            routing_conflict = error.reason == "supported_worker_routing_missing"
            selected_worker = next(
                (
                    check.removeprefix("herdr.integration.").removeprefix(
                        "routing.worker."
                    )
                    for check in error.failed_checks
                    if check.startswith(("herdr.integration.", "routing.worker."))
                ),
                lane.worker_kind,
            )
            selected_worker_label = selected_worker.capitalize()
            if missing_integration:
                summary = (
                    f"Delivery is blocked because the selected "
                    f"{selected_worker_label} Herdr integration is not ready."
                )
                continuation = (
                    f"Restore the {selected_worker_label} integration or change "
                    "the configured future-dispatch fallback policy."
                )
            elif routing_conflict:
                summary = (
                    f"Delivery is blocked because the selected "
                    f"{selected_worker_label} worker conflicts with the configured "
                    "routing policy."
                )
                continuation = (
                    "Align the future-dispatch ticket/repository route with the "
                    "configured routing policy."
                )
            else:
                summary = "Delivery is blocked by an unmet commissioning prerequisite."
                continuation = "Repair the failed commissioning prerequisite and rerun verification."
            report_result = self.report_pm(
                request_identity=request_identity,
                report=PMReportDraft(
                    record_id=record_id,
                    report_type="blocker",
                    summary=summary,
                    timestamp=str(assignment["updated_at"]),
                    blocking=True,
                    continuation_requirement=continuation,
                ),
            )
            return {
                "state": "blocked",
                "blocker": error.as_dict(),
                **report_result,
            }
        completion = self.complete_pm_dispatch(
            map_id=map_id,
            request_identity=request_identity,
            coordinator_id=str(assignment["coordinator_id"]),
            turn_id=str(assignment["active_turn_id"]),
            dispatch_id=str(outcome["dispatch_id"]),
        )
        return {**outcome, "routing": routing, **completion}

    def collect_pm_delivery_lane(
        self,
        *,
        request_identity: GovernanceRequestIdentity,
        lane: DeliveryLaneSpec,
    ) -> dict[str, Any]:
        """Integrate one terminal lane and submit executive acceptance evidence."""
        assignment = self._storage.pm_assignment_for_request(
            profile_name=request_identity.profile_name,
            session_id=request_identity.session_id,
        )
        if assignment is None:
            self._deny_governance_request(
                map_id="unassigned",
                action="pm:collect_lane",
                request_identity=request_identity,
                reason="pm_assignment_missing",
            )
        map_id = str(assignment["map_id"])
        lock_key = f"{self._storage.database}:{map_id}"
        with _operation_lock("delivery-integration", lock_key):
            with self._storage.delivery_integration_lease(map_id):
                return self._collect_pm_delivery_lane(
                    request_identity=request_identity,
                    lane=lane,
                )

    def _collect_pm_delivery_lane(
        self,
        *,
        request_identity: GovernanceRequestIdentity,
        lane: DeliveryLaneSpec,
    ) -> dict[str, Any]:
        """Collect after winning the Map-scoped integration writer lease."""
        assignment = self._storage.pm_assignment_for_request(
            profile_name=request_identity.profile_name,
            session_id=request_identity.session_id,
        )
        if assignment is None:
            self._deny_governance_request(
                map_id="unassigned",
                action="pm:collect_lane",
                request_identity=request_identity,
                reason="pm_assignment_missing",
            )
        map_id = str(assignment["map_id"])
        self._ensure_map_writable(map_id=map_id)
        if assignment["state"] != "active" or not assignment.get("active_turn_id"):
            raise ValueError(
                "PM delivery collection requires an active coordinator turn"
            )
        if (
            self._commissioning_prerequisites is None
            or self._coordinator_runtime is None
        ):
            raise RuntimeError("PM delivery runtime is not configured")
        binding = self._storage.map_binding(map_id)
        if binding is None:  # pragma: no cover - assignment references a bound Map
            raise MapBindingError(f"Map is not bound: {map_id}")
        issue = self._tracker_read(
            project_id=str(binding["project_id"]),
            operation=lambda: self._tracker.get_issue(str(binding["issue_url"])),
        )
        if issue.id != map_id or self._executive_stage(issue) != "delivery":
            raise ValueError("PM delivery collection requires a Map in delivery")
        context = self._commissioning_prerequisites.commissioning_context(
            project_id=str(binding["project_id"]),
            repository=issue.repository,
        )
        runtime_request = DeliveryRuntimeRequest(
            map_id=map_id,
            map_url=issue.url,
            context=context,
            lane=lane,
            registry_timestamp=str(assignment["updated_at"]),
        )
        registry = self._latest_delivery_lane_registry(
            project_id=str(binding["project_id"]),
            ticket_url=lane.ticket_url,
            lane=lane,
        )
        if registry is None:
            raise RuntimeError("Delivery lane registry readback is missing")
        self._validate_application_lane_registry(
            lane=lane,
            registry=registry,
            allowed_states={"running", "blocked", "terminal", "integrated"},
        )
        runtime_request = replace(runtime_request, registry=registry)
        expected_dispatch_id = delivery_confirmed_dispatch_id(runtime_request)
        confirmed_dispatch_id = registry.dispatch_id or assignment.get(
            "last_outcome_id"
        )
        if expected_dispatch_id != confirmed_dispatch_id:
            raise ValueError(
                "Collected delivery lane does not match the dispatch handoff"
            )
        ticket = self._delivery_ticket(
            project_id=str(binding["project_id"]),
            lane=lane,
            map_issue=issue,
        )
        integration_expected_head, integration_predecessor_commits = (
            self._delivery_integration_frontier(
                project_id=str(binding["project_id"]),
                lane=lane,
                repository=issue.repository,
                require_all=True,
            )
        )
        runtime_request = replace(
            runtime_request,
            integration_expected_head=integration_expected_head,
            integration_predecessor_commits=integration_predecessor_commits,
        )
        outcome = self._coordinator_runtime.collect_lane(runtime_request)
        if outcome.get("state") == "blocked":
            if outcome.get("dispatch_id") != expected_dispatch_id:
                raise ValueError(
                    "Collected delivery lane does not match the dispatch handoff"
                )
            blocked_registry = DeliveryLaneRegistry.from_payload(
                outcome.get("blocked_registry", {})
            )
            self._validate_application_lane_registry(
                lane=lane,
                registry=blocked_registry,
                allowed_states={"blocked"},
            )
            self._validate_blocked_delivery_outcome(
                outcome=outcome,
                request=runtime_request,
                blocked_registry=blocked_registry,
            )
            report_summary, continuation_requirement = (
                self._delivery_blocker_report_text()
            )
            self._confirm_delivery_lane_registry(
                project_id=str(binding["project_id"]),
                ticket=ticket,
                lane=lane,
                registry=blocked_registry,
            )
            report_result = self.report_pm(
                request_identity=request_identity,
                report=PMReportDraft(
                    record_id=(
                        "delivery-worker-blocked-"
                        + hashlib.sha256(
                            (
                                f"{outcome['dispatch_id']}:"
                                f"{assignment['active_turn_id']}"
                            ).encode()
                        ).hexdigest()[:32]
                    ),
                    report_type="blocker",
                    summary=report_summary,
                    timestamp=str(assignment["updated_at"]),
                    blocking=True,
                    continuation_requirement=continuation_requirement,
                ),
            )
            return {**outcome, **report_result}
        if outcome.get("state") != "locally_validated":
            raise RuntimeError("Delivery lane did not reach local validation")
        if outcome.get("dispatch_id") != expected_dispatch_id:
            raise ValueError(
                "Collected delivery lane does not match the dispatch handoff"
            )
        terminal_registry = DeliveryLaneRegistry.from_payload(
            outcome.get("terminal_registry", {})
        )
        integrated_registry = DeliveryLaneRegistry.from_payload(
            outcome.get("integrated_registry", {})
        )
        self._validate_application_lane_registry(
            lane=lane,
            registry=terminal_registry,
            allowed_states={"terminal"},
        )
        self._validate_application_lane_registry(
            lane=lane,
            registry=integrated_registry,
            allowed_states={"integrated"},
        )
        self._validate_local_delivery_outcome(
            outcome=outcome,
            request=runtime_request,
            source_registry=registry,
            terminal_registry=terminal_registry,
            integrated_registry=integrated_registry,
        )
        if registry.state != "integrated":
            if registry.state == "running":
                self._confirm_delivery_lane_registry(
                    project_id=str(binding["project_id"]),
                    ticket=ticket,
                    lane=lane,
                    registry=terminal_registry,
                )
            self._confirm_delivery_lane_registry(
                project_id=str(binding["project_id"]),
                ticket=ticket,
                lane=lane,
                registry=integrated_registry,
            )
        dispatch_id = str(outcome["dispatch_id"])
        final_lane = lane.integration_order == lane.integration_total
        record_id = (
            "delivery-acceptance-" if final_lane else "delivery-integrated-"
        ) + hashlib.sha256(dispatch_id.encode()).hexdigest()[:32]
        evidence = (
            self._delivery_acceptance_evidence(outcome=outcome, lane=lane)
            if final_lane
            else ()
        )
        acceptance = (
            self._delivery_acceptance_packet(
                outcome=outcome,
                lane=lane,
                repository=issue.repository,
                executive_evidence=evidence,
            )
            if final_lane
            else None
        )
        report_result = self.report_pm(
            request_identity=request_identity,
            report=PMReportDraft(
                record_id=record_id,
                report_type="acceptance" if final_lane else "checkpoint",
                summary=(
                    "Local delivery is validated and ready for acceptance review."
                    if final_lane
                    else "One ordered local delivery outcome is integrated and validated."
                ),
                timestamp=str(assignment["updated_at"]),
                evidence=evidence,
                acceptance=acceptance,
            ),
        )
        return {
            **outcome,
            "integration_idempotent": bool(outcome.get("idempotent")),
            **report_result,
        }

    def _delivery_ticket(
        self,
        *,
        project_id: str,
        lane: DeliveryLaneSpec,
        map_issue: TrackerIssue,
    ) -> TrackerIssue:
        ticket = self._tracker_read(
            project_id=project_id,
            operation=lambda: self._tracker.get_issue(lane.ticket_url),
        )
        if (
            ticket.id != lane.ticket_id
            or ticket.repository != map_issue.repository
            or ticket.url != lane.ticket_url
            or ticket.title != lane.ticket_title
            or ticket.state != "open"
            or "implementation" not in ticket.labels
            or lane.lane_id != f"implementation-{ticket.number}"
            or lane.integration_branch != f"feature/map-{map_issue.number}"
            or lane.execution_branch != f"{lane.worker_kind}/issue-{ticket.number}"
        ):
            raise ValueError("Delivery ticket does not match tracker authority")
        parent_spec = self._tracker_read(
            project_id=project_id,
            operation=lambda: self._tracker.get_issue(lane.parent_spec_url),
        )
        if (
            parent_spec.repository != map_issue.repository
            or parent_spec.url != lane.parent_spec_url
            or parent_spec.url in {map_issue.url, ticket.url}
            or "spec" not in parent_spec.labels
        ):
            raise ValueError("Delivery parent Spec does not match tracker authority")
        parent_declaration = self._delivery_ticket_field(ticket.body, "Parent")
        if (
            re.fullmatch(
                rf"(?:[-*]\s*)?{re.escape(lane.parent_spec_url)}",
                parent_declaration,
            )
            is None
        ):
            raise ValueError("Delivery ticket does not link its declared parent Spec")
        blocked_by = self._delivery_ticket_field(ticket.body, "Blocked by")
        no_dependencies = re.fullmatch(
            r"(?is)(?:[-*]\s*)?none(?:\s*-\s*can start immediately)?[.\s]*",
            blocked_by,
        )
        if no_dependencies is None:
            dependency_urls = self._delivery_dependency_urls(
                blocked_by,
                repository=map_issue.repository,
            )
            if not dependency_urls:
                raise ValueError("Delivery ticket dependency declaration is invalid")
            for dependency_url in dependency_urls:
                dependency = self._tracker_read(
                    project_id=project_id,
                    operation=lambda url=dependency_url: self._tracker.get_issue(url),
                )
                if (
                    dependency.repository != map_issue.repository
                    or dependency.url != dependency_url
                    or dependency.state != "closed"
                ):
                    raise ValueError("Delivery ticket is not independently grabbable")
        integration_fields_present = (
            re.search(
                r"(?im)^##\s+Integration (?:order|total|after)\s*$",
                ticket.body,
            )
            is not None
        )
        if (
            integration_fields_present
            or lane.integration_order != 1
            or lane.integration_total != 1
            or lane.integration_predecessor_ticket_urls
        ):
            order = self._delivery_ticket_field(ticket.body, "Integration order")
            total = self._delivery_ticket_field(ticket.body, "Integration total")
            after = self._delivery_ticket_field(ticket.body, "Integration after")
            if not order.isdigit() or not total.isdigit():
                raise ValueError("Delivery ticket integration order is invalid")
            predecessor_urls = (
                ()
                if re.fullmatch(r"(?is)(?:[-*]\s*)?none[.\s]*", after)
                else self._delivery_dependency_urls(
                    after,
                    repository=map_issue.repository,
                )
            )
            if (
                int(order) != lane.integration_order
                or int(total) != lane.integration_total
                or predecessor_urls != lane.integration_predecessor_ticket_urls
            ):
                raise ValueError(
                    "Delivery ticket integration order does not match tracker authority"
                )
        return ticket

    def _delivery_integration_frontier(
        self,
        *,
        project_id: str,
        lane: DeliveryLaneSpec,
        repository: str,
        require_all: bool,
    ) -> tuple[str, tuple[str, ...]]:
        """Read one authoritative ordered predecessor chain and its Git frontier."""
        if lane.integration_order == 1:
            return lane.base_commit, ()
        commits: list[str] = []
        incomplete = False
        for index, ticket_url in enumerate(
            lane.integration_predecessor_ticket_urls,
            start=1,
        ):
            ticket = self._tracker_read(
                project_id=project_id,
                operation=lambda url=ticket_url: self._tracker.get_issue(url),
            )
            parent = self._delivery_ticket_field(ticket.body, "Parent")
            order = self._delivery_ticket_field(ticket.body, "Integration order")
            total = self._delivery_ticket_field(ticket.body, "Integration total")
            after = self._delivery_ticket_field(ticket.body, "Integration after")
            predecessor_urls = (
                ()
                if re.fullmatch(r"(?is)(?:[-*]\s*)?none[.\s]*", after)
                else self._delivery_dependency_urls(after, repository=repository)
            )
            expected_predecessors = lane.integration_predecessor_ticket_urls[
                : index - 1
            ]
            if (
                ticket.repository != repository
                or ticket.url != ticket_url
                or ticket.state != "open"
                or "implementation" not in ticket.labels
                or re.fullmatch(
                    rf"(?:[-*]\s*)?{re.escape(lane.parent_spec_url)}",
                    parent,
                )
                is None
                or not order.isdigit()
                or int(order) != index
                or not total.isdigit()
                or int(total) != lane.integration_total
                or predecessor_urls != expected_predecessors
            ):
                raise ValueError(
                    "Delivery integration predecessor does not match tracker authority"
                )
            records = self._tracker_read(
                project_id=project_id,
                operation=lambda url=ticket_url: (
                    self._tracker.list_delivery_lane_registries(url)
                ),
            )
            registry = records[-1].registry if records else None
            if registry is None or registry.state != "integrated":
                incomplete = True
                if require_all:
                    raise CoordinatorRuntimeError(
                        reason="integration_order_not_ready",
                        retryable=True,
                    )
                continue
            if incomplete:
                raise ValueError("Delivery integration predecessor chain is not linear")
            if (
                registry.work_item != ticket_url
                or registry.role != "implementation"
                or registry.lane_id != f"implementation-{ticket.number}"
                or registry.base_commit != lane.base_commit
                or registry.runtime not in {"herdr-codex-pane", "herdr-claude-pane"}
                or not isinstance(registry.integrated_commit, str)
                or re.fullmatch(r"[0-9a-f]{40}", registry.integrated_commit) is None
            ):
                raise ValueError(
                    "Delivery integration predecessor registry conflicts with tracker truth"
                )
            commits.append(registry.integrated_commit)
        if require_all and len(commits) != lane.integration_order - 1:
            raise CoordinatorRuntimeError(
                reason="integration_order_not_ready",
                retryable=True,
            )
        if not commits:
            if require_all:
                raise CoordinatorRuntimeError(
                    reason="integration_order_not_ready",
                    retryable=True,
                )
            return lane.base_commit, ()
        return commits[-1], tuple(commits)

    @staticmethod
    def _delivery_worker_routing(
        *,
        ticket: TrackerIssue,
        lane: DeliveryLaneSpec,
        context: CommissioningContext,
        existing_registry: DeliveryLaneRegistry | None,
    ) -> dict[str, Any]:
        """Resolve one explicit, repository-scoped worker route before mutation."""
        if existing_registry is not None:
            worker_kind = existing_registry.runtime.removeprefix("herdr-").removesuffix(
                "-pane"
            )
            if lane.worker_kind != worker_kind:
                raise ValueError(
                    "Delivery worker kind conflicts with the active lane registry"
                )
            return {
                "worker_kind": worker_kind,
                "source": "active_lane_registry",
                "ticket_attributes": [],
                "integration_ready": worker_kind in context.supported_worker_kinds,
                "fallback": False,
            }

        allowed_workers = {
            "codex": ("codex",),
            "claude": ("claude",),
            "mixed": ("codex", "claude"),
        }.get(context.routing_policy)
        if allowed_workers is None:
            raise CommissioningPrerequisiteError(
                reason="supported_worker_routing_missing",
                failed_checks=("routing.policy",),
            )
        labels = tuple(
            dict.fromkeys(
                str(label).strip().lower()
                for label in ticket.labels
                if str(label).strip()
            )
        )
        explicit_workers = tuple(
            worker for worker in ("codex", "claude") if f"worker/{worker}" in labels
        )
        if len(explicit_workers) > 1:
            raise ValueError("Delivery ticket declares conflicting worker attributes")

        attribute_workers = dict(context.routing_attribute_workers)
        matched_attributes = tuple(
            attribute for attribute in labels if attribute in attribute_workers
        )
        matched_workers = tuple(
            dict.fromkeys(
                attribute_workers[attribute] for attribute in matched_attributes
            )
        )
        if len(matched_workers) > 1:
            raise ValueError("Delivery ticket attributes map to conflicting workers")
        if explicit_workers:
            selected = explicit_workers[0]
            source = "ticket_worker_attribute"
            evidence_attributes = [f"worker/{selected}"]
        elif matched_workers:
            selected = matched_workers[0]
            source = "repository_attribute_policy"
            evidence_attributes = list(matched_attributes)
        else:
            selected = context.routing_default_worker
            source = "configured_default"
            evidence_attributes = []
        if selected not in allowed_workers:
            raise CommissioningPrerequisiteError(
                reason="supported_worker_routing_missing",
                failed_checks=(f"routing.worker.{selected}",),
            )

        ready_workers = tuple(
            worker
            for worker in allowed_workers
            if worker in context.supported_worker_kinds
        )
        fallback = False
        if selected not in ready_workers:
            if context.routing_unavailable_behavior == "fallback":
                candidates = tuple(
                    worker
                    for worker in (
                        context.routing_default_worker,
                        "codex",
                        "claude",
                    )
                    if worker in ready_workers
                )
                if candidates:
                    selected = candidates[0]
                    source = "integration_readiness_fallback"
                    fallback = True
            if selected not in ready_workers:
                raise CommissioningPrerequisiteError(
                    reason="supported_worker_integration_missing",
                    failed_checks=(f"herdr.integration.{selected}",),
                )
        if lane.worker_kind != selected:
            raise ValueError("Delivery worker kind conflicts with routing policy")
        return {
            "worker_kind": selected,
            "source": source,
            "ticket_attributes": evidence_attributes,
            "integration_ready": True,
            "fallback": fallback,
        }

    @staticmethod
    def _delivery_ticket_field(body: str, field: str) -> str:
        declarations = [
            match.group(1).strip()
            for match in re.finditer(
                rf"(?ims)^##\s+{re.escape(field)}\s*$\s*(.*?)(?=^##\s+|\Z)",
                body,
            )
            if match.group(1).strip()
        ]
        declarations.extend(
            match.group(1).strip()
            for match in re.finditer(
                rf"(?im)^\*\*{re.escape(field)}:\*\*\s*(.+?)\s*$",
                body,
            )
            if match.group(1).strip()
        )
        if len(declarations) == 1:
            return declarations[0]
        if len(declarations) > 1:
            raise ValueError(f"Delivery ticket has multiple {field} declarations")
        raise ValueError(f"Delivery ticket is missing its {field} declaration")

    @staticmethod
    def _validate_delivery_prepare_outcome(
        *,
        outcome: Mapping[str, Any],
        request: DeliveryRuntimeRequest,
    ) -> None:
        if (
            not isinstance(outcome, Mapping)
            or outcome.get("state") != "prepared"
            or outcome.get("map_id") != request.map_id
            or outcome.get("dispatch_id") != delivery_confirmed_dispatch_id(request)
            or outcome.get("worker_kind") != request.lane.worker_kind
            or outcome.get("remote_actions") != "forbidden"
        ):
            raise RuntimeError("Herdr did not confirm lane preparation")

    @staticmethod
    def _validate_delivery_dispatch_outcome(
        *,
        outcome: Mapping[str, Any],
        request: DeliveryRuntimeRequest,
    ) -> None:
        lane = request.lane
        if (
            not isinstance(outcome, Mapping)
            or outcome.get("state") != "dispatched"
            or outcome.get("map_id") != request.map_id
            or outcome.get("dispatch_id") != delivery_confirmed_dispatch_id(request)
            or outcome.get("worker_kind") != lane.worker_kind
            or outcome.get("completion_contract") != lane.completion_contract
            or outcome.get("remote_actions") != "forbidden"
        ):
            raise RuntimeError("Herdr did not confirm the delivery dispatch boundary")

    @staticmethod
    def _validate_blocked_delivery_outcome(
        *,
        outcome: Mapping[str, Any],
        request: DeliveryRuntimeRequest,
        blocked_registry: DeliveryLaneRegistry,
    ) -> None:
        expected_blocker = {
            "reason": "worker_reported_blocker",
            "retryable": True,
            "summary": blocked_registry.blocker_summary,
        }
        if (
            outcome.get("state") != "blocked"
            or outcome.get("map_id") != request.map_id
            or outcome.get("dispatch_id") != delivery_confirmed_dispatch_id(request)
            or outcome.get("worker_kind") != request.lane.worker_kind
            or outcome.get("remote_actions") != "forbidden"
            or outcome.get("blocker") != expected_blocker
        ):
            raise RuntimeError("Worker blocker evidence does not match the registry")

    @staticmethod
    def _validate_local_delivery_outcome(
        *,
        outcome: Mapping[str, Any],
        request: DeliveryRuntimeRequest,
        source_registry: DeliveryLaneRegistry,
        terminal_registry: DeliveryLaneRegistry,
        integrated_registry: DeliveryLaneRegistry,
    ) -> None:
        lane = request.lane
        evidence = outcome.get("evidence")
        final_report = (
            evidence.get("final_report") if isinstance(evidence, Mapping) else None
        )
        execution_commit = (
            evidence.get("execution_commit") if isinstance(evidence, Mapping) else None
        )
        integration_commit = (
            evidence.get("integration_commit")
            if isinstance(evidence, Mapping)
            else None
        )
        allowed_report_statuses = {
            "confirmed",
            "recovered_from_git",
            "recovered_from_terminal_registry",
        }
        report_status = (
            final_report.get("status") if isinstance(final_report, Mapping) else None
        )
        report_digest = (
            final_report.get("digest") if isinstance(final_report, Mapping) else None
        )
        expected_integrated = replace(
            terminal_registry,
            state="integrated",
            integrated_commit=integrated_registry.integrated_commit,
        )
        registry_evidence_matches = (
            report_status in {"confirmed", "recovered_from_terminal_registry"}
            and terminal_registry.evidence_source == "herdr-final-report"
            and report_digest == terminal_registry.final_report_digest
        ) or (
            report_status == "recovered_from_git"
            and terminal_registry.evidence_source == "registry-git-recovery"
            and report_digest is None
            and terminal_registry.final_report_digest is None
        )
        if (
            outcome.get("map_id") != request.map_id
            or outcome.get("dispatch_id") != delivery_confirmed_dispatch_id(request)
            or outcome.get("worker_kind") != lane.worker_kind
            or outcome.get("remote_actions") != "forbidden"
            or outcome.get("acceptance_recommendation") != "accept"
            or not isinstance(evidence, Mapping)
            or not isinstance(execution_commit, str)
            or not re.fullmatch(r"[0-9a-f]{40}", execution_commit)
            or execution_commit != terminal_registry.head_commit
            or execution_commit != integrated_registry.head_commit
            or not isinstance(integration_commit, str)
            or not re.fullmatch(r"[0-9a-f]{40}", integration_commit)
            or integration_commit != integrated_registry.integrated_commit
            or evidence.get("validation") != "passed"
            or evidence.get("completion_contract") != "satisfied"
            or not isinstance(final_report, Mapping)
            or report_status not in allowed_report_statuses
            or integrated_registry != expected_integrated
            or not registry_evidence_matches
            or (
                source_registry.state == "terminal"
                and terminal_registry != source_registry
            )
            or (
                source_registry.state == "integrated"
                and integrated_registry != source_registry
            )
        ):
            raise RuntimeError(
                "Herdr delivery evidence did not confirm local acceptance readiness"
            )

    @staticmethod
    def _delivery_dependency_urls(value: str, *, repository: str) -> tuple[str, ...]:
        issue_prefix = f"https://github.com/{repository}/issues/"
        absolute_pattern = re.compile(
            r"https://github\.com/([^/\s]+)/([^/\s]+)/issues/([1-9][0-9]*)\b"
        )
        absolute = absolute_pattern.findall(value)
        if any(f"{owner}/{name}" != repository for owner, name, _number in absolute):
            raise ValueError("Delivery ticket dependency is outside the Map repository")
        numbers = [number for _owner, _name, number in absolute]
        remainder = absolute_pattern.sub("", value)
        shorthand = re.findall(r"(?<![\w/])#([1-9][0-9]*)\b", remainder)
        numbers.extend(shorthand)
        remainder = re.sub(r"(?<![\w/])#[1-9][0-9]*\b", "", remainder)
        if not numbers and re.fullmatch(r"[\s,;*\-0-9]+", remainder):
            bare = re.findall(r"\b([1-9][0-9]*)\b", remainder)
            numbers.extend(bare)
            remainder = re.sub(r"\b[1-9][0-9]*\b", "", remainder)
        if re.sub(r"[\s,;*\-]+", "", remainder):
            raise ValueError("Delivery ticket dependency declaration is invalid")
        return tuple(dict.fromkeys(f"{issue_prefix}{number}" for number in numbers))

    def _latest_delivery_lane_registry(
        self,
        *,
        project_id: str,
        ticket_url: str,
        lane: DeliveryLaneSpec,
    ) -> DeliveryLaneRegistry | None:
        records = self._tracker_read(
            project_id=project_id,
            operation=lambda: self._tracker.list_delivery_lane_registries(ticket_url),
        )
        conflicting = [
            record.registry
            for record in records
            if record.registry.lane_id != lane.lane_id
        ]
        if conflicting:
            raise RuntimeError(
                "Another active delivery lane already owns this implementation ticket"
            )
        matching = [
            record.registry
            for record in records
            if record.registry.lane_id == lane.lane_id
        ]
        if not matching:
            return None
        immutable_fields = (
            "work_item",
            "role",
            "lane_id",
            "runtime",
            "workspace_id",
            "tab_id",
            "pane_id",
            "herdr_session_name",
            "herdr_session_owned",
            "bootstrap_authority",
            "agent_permission_mode",
            "worktree",
            "branch",
            "base_commit",
            "dispatch_id",
        )
        allowed_transitions = {
            ("created", "running"),
            ("blocked", "running"),
            ("running", "blocked"),
            ("running", "terminal"),
            ("terminal", "integrated"),
        }
        try:
            for registry in matching:
                validate_delivery_lane_registry(
                    lane=lane,
                    registry=registry,
                    allowed_states={
                        "created",
                        "running",
                        "blocked",
                        "terminal",
                        "integrated",
                    },
                )
        except ValueError as error:
            raise RuntimeError(
                "Delivery lane registry history conflicts with tracker truth"
            ) from error
        if matching[0].state != "created":
            raise RuntimeError(
                "Delivery lane registry history conflicts with tracker truth"
            )
        for index in range(1, len(matching)):
            previous = matching[index - 1]
            registry = matching[index]
            if registry == previous:
                continue
            if (
                any(
                    getattr(previous, field) != getattr(registry, field)
                    for field in immutable_fields
                )
                or (previous.state, registry.state) not in allowed_transitions
            ):
                raise RuntimeError(
                    "Delivery lane registry history conflicts with tracker truth"
                )
            if previous.state == "terminal" and (
                registry.head_commit != previous.head_commit
                or registry.evidence_source != previous.evidence_source
                or registry.final_report_digest != previous.final_report_digest
            ):
                raise RuntimeError(
                    "Delivery lane registry history conflicts with tracker truth"
                )
        return matching[-1]

    @staticmethod
    def _validate_application_lane_registry(
        *,
        lane: DeliveryLaneSpec,
        registry: DeliveryLaneRegistry,
        allowed_states: set[str],
    ) -> None:
        try:
            validate_delivery_lane_registry(
                lane=lane,
                registry=registry,
                allowed_states=allowed_states,
            )
        except ValueError as error:
            raise RuntimeError(
                "Delivery lane registry conflicts with the lane contract"
            ) from error

    def _confirm_delivery_lane_registry(
        self,
        *,
        project_id: str,
        ticket: TrackerIssue,
        lane: DeliveryLaneSpec,
        registry: DeliveryLaneRegistry,
    ) -> None:
        latest = self._latest_delivery_lane_registry(
            project_id=project_id,
            ticket_url=ticket.url,
            lane=lane,
        )
        if latest == registry:
            return
        immutable_fields = (
            "work_item",
            "role",
            "lane_id",
            "runtime",
            "workspace_id",
            "tab_id",
            "pane_id",
            "herdr_session_name",
            "herdr_session_owned",
            "bootstrap_authority",
            "agent_permission_mode",
            "worktree",
            "branch",
            "base_commit",
            "dispatch_id",
        )
        if latest is not None and any(
            getattr(latest, field) != getattr(registry, field)
            for field in immutable_fields
        ):
            raise RuntimeError(
                "Delivery lane registry transition conflicts with tracker truth"
            )
        allowed_transition = (latest is None and registry.state == "created") or (
            latest is not None
            and (latest.state, registry.state)
            in {
                ("created", "running"),
                ("blocked", "running"),
                ("running", "blocked"),
                ("running", "terminal"),
                ("terminal", "integrated"),
            }
        )
        if not allowed_transition:
            raise RuntimeError(
                "Delivery lane registry transition conflicts with tracker truth"
            )
        self._tracker_read(
            project_id=project_id,
            operation=lambda: self._tracker.append_delivery_lane_registry(
                ticket.url,
                issue_id=ticket.id,
                registry=registry,
            ),
        )
        readback = self._latest_delivery_lane_registry(
            project_id=project_id,
            ticket_url=ticket.url,
            lane=lane,
        )
        if readback != registry:
            raise TrackerError(
                "Delivery lane registry readback did not match the write"
            )

    @staticmethod
    def _delivery_blocker_report_text() -> tuple[str, str]:
        return (
            "Delivery is blocked; implementation details remain in the delivery ticket.",
            "Resolve the recorded implementation blocker in the delivery ticket before requesting acceptance.",
        )

    @staticmethod
    def _delivery_acceptance_evidence(
        *,
        outcome: Mapping[str, Any],
        lane: DeliveryLaneSpec,
    ) -> tuple[str, ...]:
        evidence = [
            "One independently owned implementation outcome is integrated in the Map-local workspace.",
            "The declared focused validation and completion contract passed.",
        ]
        forbidden = {
            lane.lane_id,
            lane.ticket_id,
            lane.ticket_title,
            lane.ticket_url,
            lane.integration_worktree,
            lane.integration_branch,
            lane.execution_worktree,
            lane.execution_branch,
            lane.base_commit,
        }
        final_report = outcome.get("evidence", {}).get("final_report", {})
        limitations = [*lane.known_limitations]
        if final_report.get("status") == "recovered_from_git":
            limitations.append(DELIVERY_TRANSPORT_RECOVERY_LIMITATION)
        limitations.append(
            "Push, PR, merge, release, and Issue closure were not performed."
        )
        for item in dict.fromkeys(limitations):
            normalized = " ".join(item.split())[:500]
            if (
                not normalized
                or any(value and value in normalized for value in forbidden)
                or re.search(r"\b[0-9a-f]{40}\b", normalized)
                or "https://" in normalized
            ):
                normalized = (
                    "Additional implementation detail remains in the delivery ticket."
                )
            evidence.append(f"Known limitation: {normalized}")
        recommendation = outcome.get("acceptance_recommendation")
        if recommendation not in {"accept", "hold"}:
            raise RuntimeError("Delivery acceptance recommendation is invalid")
        evidence.append(
            f"Acceptance recommendation: {recommendation} the locally validated outcome."
        )
        return tuple(dict.fromkeys(evidence))

    @staticmethod
    def _delivery_acceptance_packet(
        *,
        outcome: Mapping[str, Any],
        lane: DeliveryLaneSpec,
        repository: str,
        executive_evidence: tuple[str, ...],
    ) -> AcceptanceEvidence:
        raw_evidence = outcome.get("evidence")
        if not isinstance(raw_evidence, Mapping):
            raise RuntimeError("Delivery acceptance evidence is unavailable")
        revision = raw_evidence.get("integration_commit")
        limitations = [
            item.removeprefix("Known limitation: ")
            for item in executive_evidence
            if item.startswith("Known limitation: ")
        ]
        branch = lane.integration_branch
        target_ref = branch if branch.startswith("refs/") else f"refs/heads/{branch}"
        return AcceptanceEvidence(
            revision=revision,
            delivered_scope=(
                "The declared Map delivery scope is locally integrated and validated.",
            ),
            validations=(
                "The declared focused validation passed.",
                "The local integration completion contract is satisfied.",
            ),
            known_limitations=tuple(limitations),
            rollback_considerations=(
                "Restore the publication target to its previous immutable revision.",
            ),
            requested_publication_action=PublicationAction(
                action="push",
                target={"repository": repository, "ref": target_ref},
            ),
        )

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
        self._ensure_map_writable(map_id=map_id)
        bound_report = report.assign_to(map_id)
        if report.report_type == "acceptance" and report.acceptance is None:
            raise ValueError(
                "PM acceptance report must include structured acceptance evidence"
            )
        if (
            report.report_type == "question"
            and report.scope is not None
            and report.scope.get("map_id") != map_id
        ):
            raise ValueError("PM question scope must match the assigned Map")
        binding = self._storage.map_binding(map_id)
        if binding is None:
            raise MapBindingError(f"Map is not bound: {map_id}")
        lock_key = f"{self._storage.database}:{map_id}"
        with _operation_lock("pm-turn", lock_key):
            with (
                self._storage.pm_turn_lease(map_id),
                self._storage.publication_lease(map_id),
            ):
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
                acceptance = bound_report.content.acceptance
                if acceptance is not None and self._publication_handoff is not None:
                    self._publication_handoff.prepare(acceptance)
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
                    issue = self._tracker_read(
                        project_id=str(binding["project_id"]),
                        operation=lambda: self._tracker.get_issue(binding["issue_url"]),
                    )
                    if issue.id != map_id:
                        raise MapBindingError("Bound GitHub Issue identity changed")
                    if bound_report.content.report_type == "question":
                        authoritative_reports = self._tracker_read(
                            project_id=str(binding["project_id"]),
                            operation=lambda: self._tracker.list_pm_reports(
                                str(binding["issue_url"])
                            ),
                        )
                        if any(
                            record.report.content.correlation_id
                            == bound_report.content.correlation_id
                            and record.report.content.record_id
                            != bound_report.content.record_id
                            for record in authoritative_reports
                        ):
                            raise PMReportConflict(
                                record_id=bound_report.content.record_id
                            )
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
            "decision_acknowledgments": self._storage.pm_decision_acknowledgments(
                map_id=map_id
            ),
        }

    def acknowledge_pm_decision(
        self,
        *,
        request_identity: GovernanceRequestIdentity,
        correlation_id: str,
    ) -> dict[str, Any]:
        """Re-read one committed answer, acknowledge it, then continue or park."""
        if not isinstance(correlation_id, str) or not correlation_id.strip():
            raise ValueError("PM decision acknowledgment requires a correlation id")
        correlation_id = correlation_id.strip()
        assignment = self._storage.pm_assignment_for_request(
            profile_name=request_identity.profile_name,
            session_id=request_identity.session_id,
        )
        if assignment is None:
            self._deny_governance_request(
                map_id="unassigned",
                action="pm:acknowledge_decision",
                request_identity=request_identity,
                reason="pm_assignment_missing",
            )
        map_id = str(assignment["map_id"])
        existing = next(
            (
                item
                for item in self._storage.pm_decision_acknowledgments(map_id=map_id)
                if item["correlation_id"] == correlation_id
            ),
            None,
        )
        binding = self._ensure_map_writable(map_id=map_id)
        question_record = self._authoritative_pm_question(
            map_id=map_id,
            binding=binding,
            correlation_id=correlation_id,
        )
        answer = self._authoritative_pm_answer(
            map_id=map_id,
            binding=binding,
            correlation_id=correlation_id,
        )
        if existing is None:
            if assignment["state"] != "active" or not assignment.get("active_turn_id"):
                raise ValueError("PM decision acknowledgment requires its resumed turn")
            matching_resumes = [
                intent
                for intent in self._outbox.effect_intents(
                    map_id=map_id,
                    effect_type=COORDINATOR_RESUME,
                )
                if intent.state == "succeeded"
                and intent.payload.get("correlation_id") == correlation_id
                and intent.payload.get("turn_id") == assignment["active_turn_id"]
            ]
            if len(matching_resumes) != 1:
                raise ValueError(
                    "PM decision correlation does not match the active resumed turn"
                )
            turn_id = str(assignment["active_turn_id"])
            acknowledged_at = self._synchronized_at()
            idempotent = self._storage.save_pm_decision_acknowledgment(
                map_id=map_id,
                correlation_id=correlation_id,
                turn_id=turn_id,
                outcome=str(answer["outcome"]),
                tracker_record_id=str(answer["tracker"]["id"]),
                tracker_record_url=str(answer["tracker"]["url"]),
                acknowledged_at=acknowledged_at,
            )
        else:
            if (
                existing["outcome"] != answer["outcome"]
                or existing["tracker"] != answer["tracker"]
            ):
                raise ValueError(
                    "Committed PM acknowledgment conflicts with authoritative answer"
                )
            turn_id = str(existing["turn_id"])
            acknowledged_at = str(existing["acknowledged_at"])
            idempotent = True
        if (
            assignment["state"] == "active"
            and assignment.get("active_turn_id") != turn_id
        ):
            raise ValueError("Another PM turn is active during decision acknowledgment")
        if answer["outcome"] == "continue" and question_record.report.content.blocking:
            issue = self._tracker_read(
                project_id=str(binding["project_id"]),
                operation=lambda: self._tracker.get_issue(str(binding["issue_url"])),
            )
            stage = self._executive_stage(issue)
            if stage == "decision":
                self.transition_map(
                    map_id=map_id,
                    expected_stage="decision",
                    requested_stage="delivery",
                )
            elif stage != "delivery":
                raise ValueError(
                    "PM decision continuation conflicts with authoritative Map stage"
                )
        elif answer["outcome"] == "blocked":
            self._storage.finish_pm_turn(
                map_id=map_id,
                turn_id=turn_id,
                outcome="decision_acknowledgment",
                outcome_id=correlation_id,
                finished_at=acknowledged_at,
            )
        return {
            "map_id": map_id,
            "correlation_id": correlation_id,
            "continuation": answer["outcome"],
            "tracker": answer["tracker"],
            "acknowledged_at": acknowledged_at,
            "idempotent": idempotent,
        }

    def _authoritative_pm_answer(
        self,
        *,
        map_id: str,
        binding: dict[str, Any],
        correlation_id: str,
    ) -> dict[str, Any]:
        approval = self._current_approval(request_id=correlation_id)
        if approval is not None:
            if approval["map_id"] != map_id:
                raise ValueError("PM decision approval ledger belongs to another Map")
            approval_records = self._tracker_read(
                project_id=str(binding["project_id"]),
                operation=lambda: self._tracker.list_approval_events(
                    str(binding["issue_url"])
                ),
            )
            decision_events = [
                record
                for record in approval_records
                if record.event.request_id == correlation_id
                and record.event.event_type in APPROVAL_DECISIONS
            ]
            if len(decision_events) != 1:
                raise ValueError(
                    "PM decision correlation has no single authoritative answer"
                )
            outcome = (
                approval["decision_payload"].get("outcome")
                if approval["status"] == "approved"
                else "blocked"
            )
            if outcome not in {"continue", "blocked"}:
                raise ValueError(
                    "Committed chairman decision has no correlation outcome"
                )
            record = decision_events[0]
            return {
                "outcome": outcome,
                "tracker": {
                    "id": record.tracker_record_id,
                    "url": record.tracker_record_url,
                },
            }
        decisions = self._tracker_read(
            project_id=str(binding["project_id"]),
            operation=lambda: self._tracker.list_decisions(str(binding["issue_url"])),
        )
        matching_decisions = [
            record
            for record in decisions
            if record.decision.decision_id == correlation_id
        ]
        if len(matching_decisions) != 1:
            raise ValueError(
                "PM decision correlation has no single authoritative answer"
            )
        record = matching_decisions[0]
        context = record.decision.authority_context or {}
        payload = context.get("decision_payload")
        outcome = payload.get("outcome") if isinstance(payload, dict) else None
        if outcome not in {"continue", "blocked"}:
            raise ValueError("Committed CEO decision has no correlation outcome")
        return {
            "outcome": outcome,
            "tracker": {
                "id": record.tracker_record_id,
                "url": record.tracker_record_url,
            },
        }

    def answer_pm_question(
        self,
        *,
        map_id: str,
        request_identity: GovernanceRequestIdentity,
        response: PMDecisionResponse,
    ) -> dict[str, Any]:
        """Route one correlation-bound CEO answer through policy and durable truth."""
        self._authorize_ceo_request(
            map_id=map_id,
            action="answer_question",
            request_identity=request_identity,
        )
        binding = self._ensure_map_writable(map_id=map_id)
        question_record = self._authoritative_pm_question(
            map_id=map_id,
            binding=binding,
            correlation_id=response.correlation_id,
        )
        question = question_record.report.content
        if response.recommendation not in question.options:
            raise ValueError("CEO recommendation must select one recorded PM option")
        authority = self._authority_policy.classify(
            str(question.decision_class),
            decision_payload=response.decision_payload,
            requested_scope=question.scope,
        )
        if authority == "unconfigured":
            self._deny_governance_request(
                map_id=map_id,
                action="answer_question",
                request_identity=request_identity,
                reason="decision_class_not_configured",
            )
        if authority == "chairman":
            requested_scope = dict(question.scope or {})
            requested_scope.update(
                map_id=map_id,
                correlation_id=response.correlation_id,
                blocking=bool(question.blocking),
            )
            decision_payload = {
                **response.decision_payload,
                "correlation_id": response.correlation_id,
                "outcome": response.outcome,
            }
            approval = self.request_approval(
                map_id=map_id,
                request_identity=request_identity,
                packet=ApprovalPacket(
                    request_id=response.correlation_id,
                    decision_class=str(question.decision_class),
                    proposed_action=response.recommendation,
                    alternatives=question.options,
                    rationale=response.rationale,
                    cost_risk=response.cost_risk,
                    evidence=question.evidence,
                    requested_scope=requested_scope,
                    decision_payload=decision_payload,
                ),
            )
            return {
                **approval,
                "route": "chairman_approval",
                "correlation_id": response.correlation_id,
            }

        affected_stage = (
            "decision"
            if question.blocking is True and response.outcome == "blocked"
            else "delivery"
        )
        decision = StructuredDecision(
            decision_id=response.correlation_id,
            type=str(question.decision_class),
            rationale=response.rationale,
            authority="ceo",
            affected_stage=affected_stage,
            timestamp=response.timestamp,
            authority_context={
                "decision_payload": {
                    **response.decision_payload,
                    "recommendation": response.recommendation,
                    "outcome": response.outcome,
                    "correlation_id": response.correlation_id,
                },
                "requested_scope": dict(question.scope or {}),
            },
        )
        lock_key = f"{self._storage.database}:{map_id}"
        with _operation_lock("decision", lock_key):
            with self._storage.decision_lease(map_id):
                decision_result = self._record_decision(
                    map_id=map_id,
                    decision=decision,
                    pm_decision_resume={
                        "correlation_id": response.correlation_id,
                        "blocking": bool(question.blocking),
                        "outcome": response.outcome,
                        "record_kind": "CEO decision",
                    },
                )
        resume = self._resume_pm_after_committed_answer(
            map_id=map_id,
            correlation_id=response.correlation_id,
            blocking=bool(question.blocking),
            outcome=response.outcome,
            record_kind="CEO decision",
            tracker=decision_result["decision"]["tracker"],
        )
        return {
            **decision_result,
            "route": "ceo_decision",
            "correlation_id": response.correlation_id,
            "resume": resume,
            "idempotent": bool(decision_result["idempotent"] and resume["idempotent"]),
        }

    def _authoritative_pm_question(
        self,
        *,
        map_id: str,
        binding: dict[str, Any],
        correlation_id: str,
    ) -> TrackerPMReportRecord:
        records = self._tracker_read(
            project_id=str(binding["project_id"]),
            operation=lambda: self._tracker.list_pm_reports(str(binding["issue_url"])),
        )
        matches = [
            record
            for record in records
            if record.report.assignment_map_id == map_id
            and record.report.content.report_type == "question"
            and record.report.content.correlation_id == correlation_id
        ]
        if len(matches) != 1:
            raise ValueError(
                "PM question correlation must resolve to exactly one authoritative record"
            )
        return matches[0]

    def _resume_pm_after_committed_answer(
        self,
        *,
        map_id: str,
        correlation_id: str,
        blocking: bool,
        outcome: str,
        record_kind: str,
        tracker: Mapping[str, Any],
    ) -> dict[str, Any]:
        effect_id = self._pm_decision_resume_effect_id(
            map_id=map_id,
            correlation_id=correlation_id,
            outcome=outcome,
        )
        assignment = self._storage.pm_assignment(map_id)
        existing = self._outbox.intent(effect_id)
        if assignment is None or existing is None:
            raise DecisionResumePendingError(
                "PM decision resume intent is not durably linked"
            )
        expected_payload = self._pm_decision_resume_payload(
            assignment=assignment,
            correlation_id=correlation_id,
            blocking=blocking,
            outcome=outcome,
            record_kind=record_kind,
            tracker=tracker,
        )
        if existing.payload != expected_payload:
            raise EffectTerminalError(
                "PM decision resume identity belongs to another committed answer"
            )
        if assignment["state"] != "idle" and existing.state != "succeeded":
            raise DecisionResumePendingError(
                "PM assignment is not idle for decision resume"
            )
        dispatched = self._outbox_dispatcher.dispatch_effect(
            effect_id=effect_id,
            owner_id=self._outbox_owner_id,
            expedite_retry=existing.state == "retry_scheduled",
        )
        status = self._outbox.intent(effect_id)
        if status is None:  # pragma: no cover
            raise DecisionResumePendingError("PM decision resume disappeared")
        if status.state != "succeeded":
            raise DecisionResumePendingError(
                status.last_error_message
                or status.terminal_reason
                or "PM decision resume is pending"
            )
        return {
            "effect_id": effect_id,
            "state": status.state,
            "turn_id": str(existing.payload["turn_id"]),
            "idempotent": (
                existing.state == "succeeded"
                or (dispatched is not None and dispatched.reconciled_by_readback)
            ),
        }

    def _enqueue_pm_decision_resume(
        self,
        *,
        map_id: str,
        correlation_id: str,
        blocking: bool,
        outcome: str,
        record_kind: str,
        tracker: Mapping[str, Any],
    ) -> str:
        assignment = self._storage.pm_assignment(map_id)
        if assignment is None:
            raise EffectRetryableError(
                "PM assignment is unavailable for decision resume"
            )
        effect_id = self._pm_decision_resume_effect_id(
            map_id=map_id,
            correlation_id=correlation_id,
            outcome=outcome,
        )
        try:
            self._outbox.enqueue(
                effect_id=effect_id,
                effect_type=COORDINATOR_RESUME,
                map_id=map_id,
                payload=self._pm_decision_resume_payload(
                    assignment=assignment,
                    correlation_id=correlation_id,
                    blocking=blocking,
                    outcome=outcome,
                    record_kind=record_kind,
                    tracker=tracker,
                ),
                created_at=self._synchronized_at(),
            )
        except OutboxConflictError as error:
            raise EffectTerminalError(
                "PM decision resume identity belongs to another committed answer"
            ) from error
        return effect_id

    @staticmethod
    def _pm_decision_resume_payload(
        *,
        assignment: Mapping[str, Any],
        correlation_id: str,
        blocking: bool,
        outcome: str,
        record_kind: str,
        tracker: Mapping[str, Any],
    ) -> dict[str, Any]:
        record_id = str(tracker["id"])
        record_url = str(tracker["url"])
        turn_id = f"decision:{correlation_id}:{outcome}"
        content = (
            f"Decision {correlation_id} is committed as {record_kind} {record_id}: "
            f"{record_url}. Re-read authoritative Map state with map_governance_pm "
            f"inspect, then acknowledge correlation {correlation_id}. "
            + (
                "Continue only after acknowledgment."
                if outcome == "continue"
                else "Remain blocked after acknowledgment."
            )
        )
        return {
            "profile_name": str(assignment["profile_name"]),
            "session_id": str(assignment["session_id"]),
            "coordinator_id": str(assignment["coordinator_id"]),
            "turn_id": turn_id,
            "content": content,
            "correlation_id": correlation_id,
            "blocking": blocking,
            "outcome": outcome,
            "record_kind": record_kind,
            "tracker_record_id": record_id,
            "tracker_record_url": record_url,
        }

    @staticmethod
    def _pm_decision_resume_effect_id(
        *, map_id: str, correlation_id: str, outcome: str
    ) -> str:
        return f"coordinator-resume:{map_id}:decision:{correlation_id}:{outcome}"

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
        binding = self._ensure_map_writable(map_id=map_id)
        pm_reports = self._tracker_read(
            project_id=str(binding["project_id"]),
            operation=lambda: self._tracker.list_pm_reports(str(binding["issue_url"])),
        )
        if any(
            record.report.content.report_type == "question"
            and record.report.content.correlation_id == decision.decision_id
            for record in pm_reports
        ):
            self._deny_governance_request(
                map_id=map_id,
                action="record_decision",
                request_identity=request_identity,
                reason="pm_question_requires_answer_action",
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
        pm_decision_resume: Mapping[str, Any] | None = None,
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
                    **(
                        {"pm_decision_resume": dict(pm_decision_resume)}
                        if pm_decision_resume is not None
                        else {}
                    ),
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
        self._ensure_map_writable(map_id=map_id)
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
        if (
            packet.decision_class == "remote_publication"
            or packet.proposed_action == "publish_map"
        ):
            self._validate_publication_approval_packet(
                map_id=map_id,
                packet=packet,
                request_identity=request_identity,
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

    def _validate_publication_approval_packet(
        self,
        *,
        map_id: str,
        packet: ApprovalPacket,
        request_identity: GovernanceRequestIdentity,
    ) -> None:
        binding = self._ensure_map_writable(map_id=map_id)
        if self._unresolved_publication_incident(binding=binding) is not None:
            self._deny_approval_enforcement(
                map_id=map_id,
                action="request_approval",
                reason="publication_repair_required",
                profile_name=request_identity.profile_name,
                session_id=request_identity.session_id,
            )
        issue = self._tracker_read(
            project_id=str(binding["project_id"]),
            operation=lambda: self._tracker.get_issue(binding["issue_url"]),
        )
        acceptance_report = next(
            (
                report
                for report in self._storage.recent_pm_reports(map_id=map_id)
                if report["type"] == "acceptance"
                and isinstance(report.get("acceptance"), dict)
            ),
            None,
        )
        acceptance = (
            dict(acceptance_report["acceptance"])
            if acceptance_report is not None
            else None
        )
        approval_binding = (
            self._publication_approval_binding(
                map_id=map_id,
                acceptance=acceptance,
                acceptance_report_id=acceptance_report["record_id"],
            )
            if acceptance is not None and acceptance_report is not None
            else None
        )
        if (
            issue.id != map_id
            or self._executive_stage(issue) != "acceptance"
            or packet.decision_class != "remote_publication"
            or packet.proposed_action != "publish_map"
            or approval_binding is None
            or normalized_json(packet.requested_scope)
            != normalized_json(approval_binding["requested_scope"])
            or normalized_json(packet.decision_payload)
            != normalized_json(approval_binding["decision_payload"])
        ):
            self._deny_approval_enforcement(
                map_id=map_id,
                action="request_approval",
                reason="publication_acceptance_mismatch",
                profile_name=request_identity.profile_name,
                session_id=request_identity.session_id,
            )

    def _publication_approval_binding(
        self,
        *,
        map_id: str,
        acceptance: Mapping[str, Any],
        acceptance_report_id: str,
    ) -> dict[str, Any] | None:
        """Project the exact secret-free binding a chairman may approve."""
        if self._publication_authority_ref is None:
            return None
        requested = acceptance.get("requested_publication_action")
        if not isinstance(requested, Mapping):
            return None
        target = requested.get("target")
        action = requested.get("action")
        return {
            "decision_class": "remote_publication",
            "proposed_action": "publish_map",
            "requested_scope": {
                "map_id": map_id,
                "publication_target": target,
                "publisher_authority_ref": self._publication_authority_ref,
            },
            "decision_payload": {
                "revision": acceptance.get("revision"),
                "publication_action": action,
                "publication_target": target,
                "publisher_authority_ref": self._publication_authority_ref,
                "acceptance_evidence_hash": normalized_hash(acceptance),
                "acceptance_report_id": acceptance_report_id,
            },
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
        self._ensure_map_writable(map_id=map_id)
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
                        result = {
                            "map_id": map_id,
                            "approval": self._approval_projection(approval),
                            "idempotent": True,
                        }
                        result = self._complete_publication_acceptance_decision(
                            result=result,
                            row=approval,
                        )
                        return self._resume_correlated_approval_decision(
                            result=result,
                            row=approval,
                        )
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
                result = {
                    "map_id": map_id,
                    "approval": self._approval_projection(stored),
                    "idempotent": False,
                }
                result = self._complete_publication_acceptance_decision(
                    result=result,
                    row=stored,
                )
                return self._resume_correlated_approval_decision(
                    result=result,
                    row=stored,
                )

    def _complete_publication_acceptance_decision(
        self,
        *,
        result: dict[str, Any],
        row: Mapping[str, Any],
    ) -> dict[str, Any]:
        if row.get("proposed_action") != "publish_map" or row.get("status") not in {
            "rejected",
            "revision",
        }:
            return result
        binding = self._storage.map_binding(str(row["map_id"]))
        if binding is None:
            raise MapBindingError(f"Map is not bound: {row['map_id']}")
        effect_id = f"stage-transition:acceptance-decision:{row['request_id']}"
        try:
            enqueued = self._outbox.enqueue(
                effect_id=effect_id,
                effect_type=TRACKER_STAGE_TRANSITION,
                map_id=str(row["map_id"]),
                payload={
                    "issue_url": binding["issue_url"],
                    "issue_id": row["map_id"],
                    "project_id": binding["project_id"],
                    "expected_stage": "acceptance",
                    "requested_stage": "delivery",
                    "protected_mutation_id": None,
                },
                created_at=self._synchronized_at(),
            )
        except OutboxConflictError as error:
            raise TrackerEffectPayloadConflict(
                "Acceptance decision transition identity belongs to other content"
            ) from error
        self._outbox_dispatcher.dispatch_effect(
            effect_id=effect_id,
            owner_id=self._outbox_owner_id,
            expedite_retry=not enqueued.created,
        )
        self._require_effect_success(
            effect_id,
            "Acceptance rejection has not returned the Map to delivery",
        )
        result = {
            **result,
            "acceptance_outcome": {
                "state": "changes_requested",
                "request_id": row["request_id"],
                "decision": row["status"],
                "requested_changes": row["decision_note"],
            },
        }
        tracker = result["approval"]["tracker"].get("decision")
        if self._coordinator_resume is None or not isinstance(tracker, dict):
            return result
        assignment = self._storage.pm_assignment(str(row["map_id"]))
        if assignment is None:
            raise EffectRetryableError(
                "PM assignment is unavailable for acceptance changes"
            )
        resume_effect_id = f"acceptance-changes:{row['request_id']}:{row['status']}"
        turn_id = f"acceptance:{row['request_id']}:{row['status']}"
        content = (
            f"Chairman acceptance decision {tracker['id']} requested changes: "
            f"{row['decision_note']} Review {tracker['url']}, return to delivery, "
            "and submit a new structured acceptance report before requesting "
            "publication again."
        )
        try:
            resume_intent = self._outbox.enqueue(
                effect_id=resume_effect_id,
                effect_type=COORDINATOR_RESUME,
                map_id=str(row["map_id"]),
                payload={
                    "profile_name": str(assignment["profile_name"]),
                    "session_id": str(assignment["session_id"]),
                    "coordinator_id": str(assignment["coordinator_id"]),
                    "turn_id": turn_id,
                    "content": content,
                    "acceptance_request_id": str(row["request_id"]),
                    "outcome": "changes_requested",
                    "tracker_record_id": str(tracker["id"]),
                    "tracker_record_url": str(tracker["url"]),
                },
                created_at=self._synchronized_at(),
            )
        except OutboxConflictError as error:
            raise EffectTerminalError(
                "Acceptance change resume identity belongs to another decision"
            ) from error
        self._outbox_dispatcher.dispatch_effect(
            effect_id=resume_effect_id,
            owner_id=self._outbox_owner_id,
            expedite_retry=not resume_intent.created,
        )
        self._require_effect_success(
            resume_effect_id,
            "Acceptance requested changes have not resumed the PM",
        )
        return {
            **result,
            "resume": {
                "effect_id": resume_effect_id,
                "state": "succeeded",
                "turn_id": turn_id,
                "idempotent": not resume_intent.created,
            },
        }

    def _resume_correlated_approval_decision(
        self,
        *,
        result: dict[str, Any],
        row: dict[str, Any],
    ) -> dict[str, Any]:
        tracker = result["approval"]["tracker"].get("decision")
        if not isinstance(tracker, dict):
            return result
        resume_parameters = self._enqueue_correlated_approval_resume(
            row=row,
            tracker=tracker,
        )
        if resume_parameters is None:
            return result
        resume = self._resume_pm_after_committed_answer(
            map_id=str(resume_parameters["map_id"]),
            correlation_id=str(resume_parameters["correlation_id"]),
            blocking=bool(resume_parameters["blocking"]),
            outcome=str(resume_parameters["outcome"]),
            record_kind=str(resume_parameters["record_kind"]),
            tracker=tracker,
        )
        return {
            **result,
            "correlation_id": resume_parameters["correlation_id"],
            "resume": resume,
        }

    def _enqueue_correlated_approval_resume(
        self,
        *,
        row: Mapping[str, Any],
        tracker: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        scope = row.get("requested_scope")
        payload = row.get("decision_payload")
        correlation_id = (
            scope.get("correlation_id") if isinstance(scope, dict) else None
        )
        if (
            not isinstance(scope, dict)
            or not isinstance(correlation_id, str)
            or not correlation_id
            or not isinstance(payload, dict)
        ):
            return None
        status = str(row["status"])
        outcome = (
            str(payload.get("outcome"))
            if status == "approved"
            and payload.get("outcome") in {"continue", "blocked"}
            else "blocked"
        )
        parameters = {
            "map_id": str(row["map_id"]),
            "correlation_id": correlation_id,
            "blocking": bool(scope.get("blocking")),
            "outcome": outcome,
            "record_kind": f"chairman {status}",
        }
        self._enqueue_pm_decision_resume(
            map_id=str(parameters["map_id"]),
            correlation_id=str(parameters["correlation_id"]),
            blocking=bool(parameters["blocking"]),
            outcome=str(parameters["outcome"]),
            record_kind=str(parameters["record_kind"]),
            tracker=tracker,
        )
        return parameters

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
        self._ensure_map_writable(map_id=map_id)
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

    def _approval_reconcile_projections(
        self,
        *,
        map_id: str,
        records: dict[str, TrackerApprovalRecord],
    ) -> list[dict[str, Any]]:
        """Validate Issue approval history into rebuildable ledger rows."""
        grouped: dict[str, list[TrackerApprovalRecord]] = {}
        for record in records.values():
            grouped.setdefault(record.event.request_id, []).append(record)
        projections: list[dict[str, Any]] = []
        for request_id, history in grouped.items():
            ordered = sorted(
                history,
                key=lambda record: datetime.fromisoformat(
                    record.event.occurred_at.replace("Z", "+00:00")
                ),
            )
            requests = [
                record for record in ordered if record.event.event_type == "requested"
            ]
            if len(requests) != 1:
                raise ApprovalRequestConflict(
                    request_id=request_id,
                    reason="tracker history must contain exactly one approval request",
                )
            requested = requests[0]
            details = requested.event.details
            try:
                packet = ApprovalPacket(
                    request_id=details["request_id"],
                    decision_class=details["decision_class"],
                    proposed_action=details["proposed_action"],
                    alternatives=tuple(details["alternatives"]),
                    rationale=details["rationale"],
                    cost_risk=details["cost_risk"],
                    evidence=tuple(details["evidence"]),
                    requested_scope=details["requested_scope"],
                    decision_payload=details["decision_payload"],
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ApprovalRequestConflict(
                    request_id=request_id,
                    reason="tracker approval request packet is malformed",
                ) from error
            if (
                packet.request_id != request_id
                or packet.requested_scope.get("map_id") != map_id
                or packet.payload_hash != requested.event.payload_hash
                or details.get("payload_hash") != packet.payload_hash
                or details.get("packet_hash") != packet.packet_hash
            ):
                raise ApprovalRequestConflict(
                    request_id=request_id,
                    reason="tracker approval request packet identity is inconsistent",
                )
            if any(
                record.event.payload_hash != packet.payload_hash for record in ordered
            ):
                raise ApprovalRequestConflict(
                    request_id=request_id,
                    reason="tracker approval history belongs to another payload",
                )

            outcomes = [
                record
                for record in ordered
                if record.event.event_type in APPROVAL_DECISIONS
            ]
            revocations = [
                record for record in ordered if record.event.event_type == "revoked"
            ]
            consumptions = [
                record for record in ordered if record.event.event_type == "consumed"
            ]
            if len(outcomes) > 1 or len(revocations) > 1 or len(consumptions) > 1:
                raise ApprovalRequestConflict(
                    request_id=request_id,
                    reason="tracker approval history has multiple terminal outcomes",
                )
            if ordered[0] is not requested:
                raise ApprovalRequestConflict(
                    request_id=request_id,
                    reason="tracker approval outcome precedes its request",
                )

            status = "pending"
            decided_by = None
            decided_by_profile = None
            decision_note = None
            decided_at = None
            expires_at = None
            decision_record: TrackerApprovalRecord | None = None
            if outcomes:
                outcome = outcomes[0]
                outcome_details = outcome.event.details
                if outcome_details.get("decision") != outcome.event.event_type:
                    raise ApprovalRequestConflict(
                        request_id=request_id,
                        reason="tracker approval decision marker is inconsistent",
                    )
                status = outcome.event.event_type
                decided_by = self._approval_history_text(
                    outcome_details, "actor_id", request_id
                )
                decided_by_profile = self._approval_history_text(
                    outcome_details, "actor_profile", request_id
                )
                decision_note = self._approval_history_text(
                    outcome_details, "note", request_id
                )
                decided_at = outcome.event.occurred_at
                expires_at = outcome_details.get("expires_at")
                if status == "approved":
                    if not isinstance(expires_at, str):
                        raise ApprovalRequestConflict(
                            request_id=request_id,
                            reason="approved tracker history has no expiry",
                        )
                    try:
                        datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                    except ValueError as error:
                        raise ApprovalRequestConflict(
                            request_id=request_id,
                            reason="approved tracker history expiry is malformed",
                        ) from error
                elif expires_at is not None:
                    raise ApprovalRequestConflict(
                        request_id=request_id,
                        reason="non-approved tracker outcome carries an expiry",
                    )
                decision_record = outcome
            if revocations:
                revocation = revocations[0]
                if status != "approved" or ordered.index(revocation) < ordered.index(
                    outcomes[0]
                ):
                    raise ApprovalRequestConflict(
                        request_id=request_id,
                        reason="tracker revocation has no preceding approval",
                    )
                status = "revoked"
                decided_by = self._approval_history_text(
                    revocation.event.details, "actor_id", request_id
                )
                decided_by_profile = self._approval_history_text(
                    revocation.event.details, "actor_profile", request_id
                )
                decision_note = self._approval_history_text(
                    revocation.event.details, "note", request_id
                )
                decided_at = revocation.event.occurred_at
                expires_at = None
                decision_record = revocation

            consumption_record = consumptions[0] if consumptions else None
            if consumption_record is not None:
                if status != "approved" or ordered.index(
                    consumption_record
                ) < ordered.index(outcomes[0]):
                    raise ApprovalRequestConflict(
                        request_id=request_id,
                        reason="tracker consumption has no preceding approval",
                    )
                mutation_id = self._approval_history_text(
                    consumption_record.event.details,
                    "mutation_id",
                    request_id,
                )
                consumed_action = self._approval_history_text(
                    consumption_record.event.details,
                    "action",
                    request_id,
                )
                if consumed_action != packet.proposed_action:
                    raise ApprovalRequestConflict(
                        request_id=request_id,
                        reason="tracker consumption belongs to another action",
                    )
                if expires_at is None or datetime.fromisoformat(
                    consumption_record.event.occurred_at.replace("Z", "+00:00")
                ) > datetime.fromisoformat(expires_at.replace("Z", "+00:00")):
                    raise ApprovalRequestConflict(
                        request_id=request_id,
                        reason="tracker consumption occurred after approval expiry",
                    )
                status = "consumed"

            existing = self._storage.approval(request_id)
            if existing is not None and (
                existing["map_id"] != map_id
                or existing["packet_hash"] != packet.packet_hash
            ):
                raise ApprovalRequestConflict(
                    request_id=request_id,
                    reason="local enforcement record belongs to another packet",
                )
            local_terminal = existing is not None and existing["status"] in {
                "expired",
                "consumed",
            }
            if local_terminal and status not in {"approved", "consumed"}:
                raise ApprovalRequestConflict(
                    request_id=request_id,
                    reason="tracker outcome conflicts with local enforcement record",
                )
            effective_status = (
                existing["status"]
                if local_terminal and existing is not None
                else status
            )
            consumption = None
            consumed_by_mutation_id = (
                mutation_id
                if consumption_record is not None
                else (
                    existing.get("consumed_by_mutation_id")
                    if existing is not None
                    else None
                )
            )
            consumed_at = (
                consumption_record.event.occurred_at
                if consumption_record is not None
                else (existing.get("consumed_at") if existing is not None else None)
            )
            if consumed_by_mutation_id:
                consumption = {
                    "mutation_id": consumed_by_mutation_id,
                    "consumed_at": consumed_at,
                }
            decision = (
                {
                    "actor_id": decided_by,
                    "actor_profile": decided_by_profile,
                    "note": decision_note,
                    "decided_at": decided_at,
                }
                if decided_by is not None
                else None
            )
            events = [
                {
                    "event_id": record.event.event_id,
                    "event_type": record.event.event_type,
                    "occurred_at": record.event.occurred_at,
                    "actor_id": record.event.details.get("actor_id"),
                    "actor_profile": record.event.details.get("actor_profile"),
                    "note": record.event.details.get("note"),
                    "expires_at": record.event.details.get("expires_at"),
                    "payload_hash": record.event.payload_hash,
                    "tracker_record_id": record.tracker_record_id,
                    "tracker_record_url": record.tracker_record_url,
                }
                for record in ordered
            ]
            projections.append(
                {
                    **packet.payload(),
                    "map_id": map_id,
                    "packet_hash": packet.packet_hash,
                    "status": effective_status,
                    "requested_by_profile": (
                        existing["requested_by_profile"]
                        if existing is not None
                        else "tracker-reconcile"
                    ),
                    "requested_by_session": (
                        existing["requested_by_session"]
                        if existing is not None
                        else "tracker-reconcile"
                    ),
                    "requested_at": requested.event.occurred_at,
                    "tracker_request_id": requested.tracker_record_id,
                    "tracker_request_url": requested.tracker_record_url,
                    "decided_by": decided_by,
                    "decided_by_profile": decided_by_profile,
                    "decision_note": decision_note,
                    "decided_at": decided_at,
                    "expires_at": expires_at,
                    "tracker_decision_id": (
                        decision_record.tracker_record_id
                        if decision_record is not None
                        else None
                    ),
                    "tracker_decision_url": (
                        decision_record.tracker_record_url
                        if decision_record is not None
                        else None
                    ),
                    "consumed_by_mutation_id": consumed_by_mutation_id,
                    "consumed_at": consumed_at,
                    "updated_at": (
                        existing["updated_at"]
                        if local_terminal and existing is not None
                        else (consumed_at or decided_at or requested.event.occurred_at)
                    ),
                    "events": events,
                    "public": {
                        **packet.payload(),
                        "status": effective_status,
                        "requested_at": requested.event.occurred_at,
                        "expires_at": expires_at,
                        "decision": decision,
                        "consumption": consumption,
                        "history": [
                            {
                                **event,
                                "tracker": {
                                    "id": event["tracker_record_id"],
                                    "url": event["tracker_record_url"],
                                },
                            }
                            for event in events
                        ],
                        "tracker": {
                            "request": {
                                "id": requested.tracker_record_id,
                                "url": requested.tracker_record_url,
                            },
                            "decision": (
                                {
                                    "id": decision_record.tracker_record_id,
                                    "url": decision_record.tracker_record_url,
                                }
                                if decision_record is not None
                                else None
                            ),
                        },
                    },
                }
            )
        return projections

    def _authoritative_map_history(
        self,
        *,
        issue: TrackerIssue,
        synchronized_at: str,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        """Load every tracker-backed Map history projection through one seam."""
        decisions = [
            self._decision_projection(record, confirmed_at=synchronized_at)
            for record in self._decision_records_by_id(
                self._tracker.list_decisions(issue.url)
            ).values()
        ]
        list_reports = getattr(self._tracker, "list_pm_reports", None)
        if callable(list_reports):
            reports = []
            for record in self._pm_report_records_by_id(
                list_reports(issue.url)
            ).values():
                if record.report.assignment_map_id != issue.id:
                    raise MapBindingError(
                        "Tracker PM report belongs to another assigned Map"
                    )
                reports.append(
                    self._pm_report_projection(record, confirmed_at=synchronized_at)
                )
        elif self._storage.recent_pm_reports(map_id=issue.id, limit=1):
            raise MapBindingError(
                "Tracker adapter cannot authoritatively reconcile PM report history"
            )
        else:
            reports = []
        list_approvals = getattr(self._tracker, "list_approval_events", None)
        if callable(list_approvals):
            approval_records = self._approval_records_by_event_id(
                list_approvals(issue.url)
            )
        elif self._storage.approvals(map_id=issue.id):
            raise MapBindingError(
                "Tracker adapter cannot authoritatively reconcile approval history"
            )
        else:
            approval_records = {}
        approvals = self._approval_reconcile_projections(
            map_id=issue.id,
            records=approval_records,
        )
        list_publications = getattr(self._tracker, "list_publication_records", None)
        if callable(list_publications):
            publications = []
            for record in self._publication_records_by_id(
                list_publications(issue.url)
            ).values():
                if record.record.map_id != issue.id:
                    raise MapBindingError(
                        "Tracker publication record belongs to another Map"
                    )
                publications.append(
                    self._publication_projection(
                        record,
                        confirmed_at=synchronized_at,
                    )
                )
        elif self._storage.recent_publication_records(map_id=issue.id):
            raise MapBindingError(
                "Tracker adapter cannot authoritatively reconcile publication history"
            )
        else:
            publications = []
        for publication in publications:
            if publication["status"] not in {"succeeded", "aborted"}:
                continue
            approval = next(
                (
                    item
                    for item in approvals
                    if item["request_id"] == publication["approval_request_id"]
                    and item["proposed_action"] == "publish_map"
                    and item["status"] in {"approved", "consumed"}
                ),
                None,
            )
            if approval is not None:
                approval["status"] = "consumed"
                approval["consumed_by_mutation_id"] = publication["action_id"]
                approval["consumed_at"] = publication["occurred_at"]
                approval["updated_at"] = publication["occurred_at"]
                approval["public"] = {
                    **approval["public"],
                    "status": "consumed",
                    "consumption": {
                        "mutation_id": publication["action_id"],
                        "consumed_at": publication["occurred_at"],
                    },
                }
        if issue.state == "closed" and issue.state_reason == "not_planned":
            cancellation = next(
                (
                    approval
                    for approval in approvals
                    if approval["proposed_action"] == "cancel_map"
                    and approval["status"] == "consumed"
                    and approval.get("consumed_by_mutation_id")
                    and approval["decision_payload"] == {"state_reason": "not_planned"}
                ),
                None,
            )
            if cancellation is None:
                raise MapBindingError(
                    "Cancelled Map Issue has no consumed cancellation authority"
                )
        elif issue.state == "closed":
            latest_acceptance = next(
                (
                    report
                    for report in reversed(reports)
                    if report["type"] == "acceptance"
                    and isinstance(report.get("acceptance"), dict)
                ),
                None,
            )
            unresolved_incident = any(
                record["status"] == "repair_required"
                and not any(
                    later["action_id"] == record["action_id"]
                    and later["status"] in {"succeeded", "aborted"}
                    for later in publications[index + 1 :]
                )
                for index, record in enumerate(publications)
            )
            valid_publication = False
            if latest_acceptance is not None and not unresolved_incident:
                acceptance = dict(latest_acceptance["acceptance"])
                requested = acceptance.get("requested_publication_action")
                for record in publications:
                    if record["status"] != "succeeded":
                        continue
                    approval = next(
                        (
                            item
                            for item in approvals
                            if item["request_id"] == record["approval_request_id"]
                        ),
                        None,
                    )
                    if not isinstance(requested, dict) or approval is None:
                        continue
                    payload = approval["decision_payload"]
                    expires_at = approval.get("expires_at")
                    decided_at = approval.get("decided_at")
                    approval_active_at_publication = (
                        isinstance(decided_at, str)
                        and isinstance(expires_at, str)
                        and datetime.fromisoformat(decided_at.replace("Z", "+00:00"))
                        <= datetime.fromisoformat(
                            record["occurred_at"].replace("Z", "+00:00")
                        )
                        < datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                    )
                    valid_publication = (
                        approval["proposed_action"] == "publish_map"
                        and approval["status"] == "consumed"
                        and approval_active_at_publication
                        and approval.get("consumed_by_mutation_id")
                        == record["action_id"]
                        and payload.get("acceptance_report_id")
                        == latest_acceptance["record_id"]
                        and payload.get("acceptance_evidence_hash")
                        == normalized_hash(acceptance)
                        and record["revision"] == acceptance.get("revision")
                        and record["action"] == requested.get("action")
                        and normalized_json(record["target"])
                        == normalized_json(requested.get("target"))
                    )
                    if valid_publication:
                        break
            if not valid_publication:
                raise MapBindingError(
                    "Completed Map Issue has no exact governed publication lineage"
                )
        return decisions, reports, approvals, publications

    @staticmethod
    def _publication_records_by_id(
        records: list[TrackerPublicationRecord],
    ) -> dict[str, TrackerPublicationRecord]:
        by_id: dict[str, TrackerPublicationRecord] = {}
        for record in records:
            record_id = record.record.record_id
            existing = by_id.get(record_id)
            if existing is not None and existing.record != record.record:
                raise MapBindingError(
                    "Tracker publication record identity has conflicting history"
                )
            by_id.setdefault(record_id, record)
        return by_id

    @staticmethod
    def _publication_projection(
        record: TrackerPublicationRecord,
        *,
        confirmed_at: str,
    ) -> dict[str, Any]:
        return {
            **record.record.payload(),
            "tracker": {
                "id": record.tracker_record_id,
                "url": record.tracker_record_url,
            },
            "confirmed_at": confirmed_at,
        }

    @staticmethod
    def _approval_history_text(
        details: dict[str, Any], field: str, request_id: str
    ) -> str:
        value = details.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ApprovalRequestConflict(
                request_id=request_id,
                reason=f"tracker approval history has no {field}",
            )
        return value.strip()

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

    def _current_approval(
        self,
        *,
        request_id: str,
        persist_expiry: bool = True,
    ) -> dict[str, Any] | None:
        approval = self._storage.approval(request_id)
        if approval is None:
            return None
        if approval["status"] == "approved" and approval.get("expires_at"):
            expires_at = datetime.fromisoformat(
                str(approval["expires_at"]).replace("Z", "+00:00")
            )
            if expires_at <= self._current_datetime():
                if persist_expiry:
                    self._storage.expire_approval(
                        request_id=request_id,
                        expired_at=self._synchronized_at(),
                    )
                    approval = self._storage.approval(request_id)
                else:
                    approval = {**approval, "status": "expired"}
        return approval

    def _approval_collection(self, *, map_id: str) -> dict[str, Any]:
        persist_expiry = True
        try:
            self._ensure_map_writable(map_id=map_id)
        except StaleProjectionError:
            persist_expiry = False
        approvals = []
        for row in self._storage.approvals(map_id=map_id):
            current = self._current_approval(
                request_id=row["request_id"],
                persist_expiry=persist_expiry,
            )
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
        self._ensure_map_writable(map_id=map_id)
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
                    preserve_existing_identity=True,
                )
            stored_bootstrap_hash = str(
                existing_binding.get("bootstrap_hash") or bootstrap_hash
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
                        bootstrap_hash=stored_bootstrap_hash,
                    )

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
            return self._repair_required(
                map_id=map_id,
                identity=identity,
                title=title,
                reason="recorded_session_lineage_missing_or_mismatched",
                candidate_count=len(recovered),
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
        preserve_existing_identity: bool = False,
    ):
        self._storage.save_ceo_session_repair_required(
            map_id=map_id,
            profile_name=self._profile_name or "",
            canonical_identity=identity,
            canonical_title=title,
            reason=reason,
            candidate_count=candidate_count,
            updated_at=self._synchronized_at(),
            preserve_existing_identity=preserve_existing_identity,
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
        existing = self._storage.project_binding_for_url(project_url)
        if existing is not None:
            self._ensure_project_writable(project_id=str(existing["project_id"]))
        try:
            project = self._tracker.get_project(project_url)
        except TrackerError as error:
            if existing is not None:
                self._mark_tracker_stale(
                    project_id=str(existing["project_id"]), reason=str(error)
                )
            raise
        synchronized_at = self._synchronized_at()
        stored = self._stored_project(project)
        self._storage.save_configured_project(
            project=stored,
            synchronized_at=synchronized_at,
        )
        return self._configured_project(project, synchronized_at=synchronized_at)

    def bind_map(self, *, project_id: str, issue_url: str) -> dict[str, Any]:
        """Bind an existing tracker Issue as exactly one Map card."""
        project_binding = self._storage.project_binding(project_id)
        if project_binding is None:
            raise MapBindingError(f"CEO project is not configured: {project_id}")
        self._ensure_project_writable(project_id=project_id)
        synchronized_at = self._synchronized_at()
        try:
            issue = self._tracker.get_issue(issue_url)
            decisions, reports, approvals, publications = (
                self._authoritative_map_history(
                    issue=issue,
                    synchronized_at=synchronized_at,
                )
            )
        except TrackerError as error:
            self._mark_tracker_stale(project_id=project_id, reason=str(error))
            raise
        card = self._stored_card(
            issue,
            project_id=project_binding["project_id"],
            synchronized_at=synchronized_at,
        )
        try:
            self._storage.save_bound_map(card=card, issue_url=issue.url)
        except ValueError as error:
            raise MapBindingError(str(error)) from error
        self._storage.replace_decision_projections(
            map_id=issue.id,
            decisions=decisions,
        )
        self._storage.replace_pm_report_projections(
            map_id=issue.id,
            reports=reports,
        )
        self._storage.replace_approval_projections(
            map_id=issue.id,
            approvals=approvals,
            reconciled_at=synchronized_at,
        )
        self._storage.replace_publication_projections(
            map_id=issue.id,
            records=publications,
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
            self._reconcile_project(project_binding)
        return self.board()

    def reconcile_project(self, *, project_id: str) -> dict[str, Any]:
        """Fetch all tracker truth and clear stale only after atomic reconcile."""
        binding = self._storage.project_binding(project_id)
        if binding is None:
            raise MapBindingError(f"CEO project is not configured: {project_id}")
        self._reconcile_project(binding)
        return self.board()

    def _reconcile_project(self, binding: dict[str, Any]) -> bool:
        project_id = str(binding["project_id"])
        changed_at = self._synchronized_at()
        self._storage.set_project_reachability(
            project_id=project_id,
            source="tracker",
            state="reconciling",
            reason=None,
            changed_at=changed_at,
        )
        try:
            project = self._tracker.get_project(str(binding["project_url"]))
            if project.id != project_id:
                raise MapBindingError("Configured GitHub Project identity changed")
            synchronized_at = self._synchronized_at()
            cards: list[dict[str, Any]] = []
            decisions: dict[str, list[dict[str, Any]]] = {}
            reports: dict[str, list[dict[str, Any]]] = {}
            approvals: dict[str, list[dict[str, Any]]] = {}
            publications: dict[str, list[dict[str, Any]]] = {}
            for map_binding in self._storage.map_bindings(project_id):
                issue = self._tracker.get_issue(str(map_binding["issue_url"]))
                if issue.id != map_binding["map_id"]:
                    raise MapBindingError("Bound GitHub Issue identity changed")
                cards.append(
                    self._stored_card(
                        issue,
                        project_id=project_id,
                        synchronized_at=synchronized_at,
                    )
                )
                (
                    decisions[issue.id],
                    reports[issue.id],
                    approvals[issue.id],
                    publications[issue.id],
                ) = self._authoritative_map_history(
                    issue=issue,
                    synchronized_at=synchronized_at,
                )
            self._storage.apply_project_reconcile(
                project=self._stored_project(project),
                cards=cards,
                decisions=decisions,
                reports=reports,
                approvals=approvals,
                publications=publications,
                synchronized_at=synchronized_at,
            )
        except (
            TrackerError,
            MapBindingError,
            ApprovalRequestConflict,
            StructuredDecisionConflict,
            PMReportConflict,
            ValueError,
        ) as error:
            self._mark_tracker_stale(project_id=project_id, reason=str(error))
            return False
        return True

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
                        commissioning_ready_record_id=None,
                    )
            return self._transition_map(
                map_id=map_id,
                expected_stage=expected_stage,
                requested_stage=requested_stage,
                approval_request_id=approval_request_id,
                mutation_id=mutation_id,
                actor_identity=actor_identity,
                commissioning_ready_record_id=None,
            )

    def _transition_commissioned_delivery(
        self,
        *,
        map_id: str,
        ready_record_id: str,
        mutation_id: str | None,
    ) -> dict[str, Any]:
        """Commit the one internal tracker transition unlocked by PM readiness."""
        lock_key = f"{self._storage.database}:{map_id}"
        with _operation_lock("transition", lock_key):
            return self._transition_map(
                map_id=map_id,
                expected_stage="authorized",
                requested_stage="delivery",
                approval_request_id=None,
                mutation_id=mutation_id,
                actor_identity=None,
                commissioning_ready_record_id=ready_record_id,
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
        commissioning_ready_record_id: str | None,
    ) -> dict[str, Any]:
        binding = self._storage.map_binding(map_id)
        if binding is None:
            raise MapBindingError(f"Map is not bound: {map_id}")
        project = self._storage.project_binding(binding["project_id"])
        if project is None:
            raise MapBindingError(
                f"CEO project is not configured: {binding['project_id']}"
            )
        self._ensure_project_writable(project_id=str(binding["project_id"]))

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

        current_issue = self._tracker_read(
            project_id=str(binding["project_id"]),
            operation=lambda: self._tracker.get_issue(binding["issue_url"]),
        )
        if current_issue.id != map_id:
            raise MapBindingError("Bound GitHub Issue identity changed")
        current_stage = self._executive_stage(current_issue)
        replaying_committed_transition = current_stage == requested_stage and (
            existing_mutation is not None or mutation_id is not None
        )
        if (
            not replaying_committed_transition
            and current_stage == "authorized"
            and requested_stage == "delivery"
        ):
            runtime = (
                self._coordinator_runtime.status(map_id=map_id)
                if self._coordinator_runtime is not None
                else {"state": "not_commissioned"}
            )
            if (
                not commissioning_ready_record_id
                or runtime.get("state") != "ready_confirmed"
                or runtime.get("ready_record_id") != commissioning_ready_record_id
                or runtime.get("failure") is not None
            ):
                raise MapTransitionError(
                    current_stage=current_stage,
                    requested_stage=requested_stage,
                    reason=(
                        "Hermes PM ready checkpoint is not tracker-confirmed; "
                        "use the explicit commission/resume seam"
                    ),
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
                observed = self._tracker_read(
                    project_id=str(binding["project_id"]),
                    operation=lambda: self._tracker.get_issue(binding["issue_url"]),
                )
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

    def _successful_publication_record(
        self,
        intent: OutboxIntent,
        evidence: RemotePublicationEvidence,
    ) -> PublicationRecord:
        action = ApprovedPublicationAction.from_payload(dict(intent.payload["action"]))
        if evidence.action_id != action.action_id:
            raise EffectTerminalError(
                "Publisher evidence belongs to another approved action"
            )
        attempted_at = self._outbox.external_call_started_at(intent.effect_id)
        if attempted_at is None:
            raise EffectTerminalError(
                "Publisher evidence has no durable authorized attempt timestamp"
            )
        return PublicationRecord(
            record_id=f"publication:{action.action_id}:succeeded",
            action_id=action.action_id,
            map_id=action.map_id,
            approval_request_id=str(intent.payload["approval_request_id"]),
            status="succeeded",
            revision=action.revision,
            action=action.action,
            target=action.target,
            occurred_at=attempted_at,
            evidence=evidence,
        )

    @staticmethod
    def _publication_record_successor(
        intent: OutboxIntent,
        record: PublicationRecord,
    ) -> dict[str, Any]:
        return {
            "effect_id": f"tracker-publication:{record.action_id}:{record.status}",
            "payload": {
                "issue_url": intent.payload["issue_url"],
                "issue_id": intent.map_id,
                "project_id": intent.payload["project_id"],
                "record": record.payload(),
                "acceptance_report_id": intent.payload["acceptance_report_id"],
            },
        }

    def _complete_external_effect(
        self,
        intent: OutboxIntent,
        confirmation: EffectConfirmation,
    ) -> None:
        if intent.effect_type == PUBLISHER_EXECUTE:
            raw_evidence = confirmation.acknowledgment.get("evidence")
            if not isinstance(raw_evidence, dict):
                raise EffectRetryableError(
                    "Publisher confirmation has no immutable remote evidence"
                )
            evidence = RemotePublicationEvidence.from_payload(raw_evidence)
            action = ApprovedPublicationAction.from_payload(
                dict(intent.payload["action"])
            )
            incident_effect_id = (
                f"tracker-publication:{action.action_id}:repair-required"
            )
            if (
                confirmation.reconciled_by_readback
                and self._outbox.external_call_started(intent.effect_id)
                and self._outbox.intent(incident_effect_id) is None
            ):
                attempted_at = self._outbox.external_call_started_at(intent.effect_id)
                if attempted_at is None:  # pragma: no cover - checked above
                    raise EffectTerminalError("Publisher attempt marker disappeared")
                incident = PublicationRecord(
                    record_id=f"publication:{action.action_id}:repair-required",
                    action_id=action.action_id,
                    map_id=action.map_id,
                    approval_request_id=str(intent.payload["approval_request_id"]),
                    status="repair_required",
                    revision=action.revision,
                    action=action.action,
                    target=action.target,
                    occurred_at=attempted_at,
                    reason=(
                        "Publisher restarted after the remote-call marker; provider "
                        "readback was required before resolution."
                    ),
                )
                incident_successor = self._publication_record_successor(
                    intent, incident
                )
                self._outbox.enqueue(
                    effect_id=str(incident_successor["effect_id"]),
                    effect_type=TRACKER_PUBLICATION_RECORD,
                    map_id=intent.map_id,
                    payload=dict(incident_successor["payload"]),
                    created_at=self._synchronized_at(),
                )
            self._confirm_publication_incident_if_present(action_id=action.action_id)
            record = self._successful_publication_record(intent, evidence)
            successor = self._publication_record_successor(intent, record)
            try:
                self._outbox.enqueue(
                    effect_id=str(successor["effect_id"]),
                    effect_type=TRACKER_PUBLICATION_RECORD,
                    map_id=intent.map_id,
                    payload=dict(successor["payload"]),
                    created_at=self._synchronized_at(),
                )
            except OutboxConflictError as error:
                raise EffectTerminalError(
                    "Publication evidence identity belongs to other content"
                ) from error
            return
        if intent.effect_type == TRACKER_PUBLICATION_RECORD:
            raw_record = confirmation.acknowledgment.get("record")
            if not isinstance(raw_record, dict):
                raise EffectRetryableError(
                    "Tracker confirmation has no publication record"
                )
            record = PublicationRecord.from_payload(raw_record)
            projection = {
                **record.payload(),
                "tracker": {
                    "id": str(confirmation.acknowledgment["tracker_record_id"]),
                    "url": str(confirmation.acknowledgment["tracker_record_url"]),
                },
                "confirmed_at": self._synchronized_at(),
            }
            self._storage.save_publication_projection(
                map_id=intent.map_id,
                record=projection,
            )
            if record.status == "succeeded":
                try:
                    self._outbox.enqueue(
                        effect_id=f"tracker-close:{record.action_id}:completed",
                        effect_type=TRACKER_ISSUE_CLOSE,
                        map_id=intent.map_id,
                        payload={
                            "issue_url": intent.payload["issue_url"],
                            "issue_id": intent.map_id,
                            "project_id": intent.payload["project_id"],
                            "state_reason": "completed",
                            "expected_stage": "acceptance",
                            "publication_record_id": record.record_id,
                            "acceptance_report_id": intent.payload[
                                "acceptance_report_id"
                            ],
                            "protected_mutation_id": record.action_id,
                        },
                        created_at=self._synchronized_at(),
                    )
                except OutboxConflictError as error:
                    raise EffectTerminalError(
                        "Completed closeout identity belongs to other content"
                    ) from error
            return
        if intent.effect_type == TRACKER_ISSUE_CLOSE:
            issue_payload = confirmation.acknowledgment.get("issue")
            if not isinstance(issue_payload, dict):
                raise EffectRetryableError(
                    "Tracker close confirmation has no authoritative Issue"
                )
            issue = TrackerEffectAdapter.issue_from_payload(issue_payload)
            card = self._stored_card(
                issue,
                project_id=str(intent.payload["project_id"]),
                synchronized_at=self._synchronized_at(),
            )
            competing_stage = self._storage.compare_and_save_map_projection(
                card,
                expected_stage=str(intent.payload["expected_stage"]),
            )
            if competing_stage is not None and competing_stage != card["stage"]:
                raise EffectRetryableError(
                    "local projection changed after tracker close confirmation"
                )
            mutation_id = intent.payload.get("protected_mutation_id")
            if mutation_id:
                self._storage.confirm_protected_mutation(
                    mutation_id=str(mutation_id),
                    confirmed_at=self._synchronized_at(),
                )
            return
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
            resume = intent.payload.get("pm_decision_resume")
            if isinstance(resume, dict):
                self._enqueue_pm_decision_resume(
                    map_id=intent.map_id,
                    correlation_id=str(resume["correlation_id"]),
                    blocking=bool(resume["blocking"]),
                    outcome=str(resume["outcome"]),
                    record_kind=str(resume["record_kind"]),
                    tracker={
                        "id": record.tracker_record_id,
                        "url": record.tracker_record_url,
                    },
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
        if report.content.report_type == "question":
            self._route_pm_question_to_ceo(map_id=map_id, report=record)

    def _route_pm_question_to_ceo(
        self,
        *,
        map_id: str,
        report: TrackerPMReportRecord,
    ) -> None:
        """Wake the canonical CEO once, after the question is tracker-confirmed."""
        question = report.report.content
        correlation_id = question.correlation_id
        if correlation_id is None or not self._session_resume_outbox:
            return
        session = self._storage.ceo_session_binding(map_id)
        if (
            session is None
            or session.get("state") != "ready"
            or not session.get("root_session_id")
        ):
            raise EffectRetryableError(
                "Canonical CEO session is not ready for the PM question"
            )
        content = "\n".join(
            (
                "A tracker-confirmed Hermes PM decision request is ready.",
                f"Correlation: {correlation_id}",
                f"Committed question: {report.tracker_record_url}",
                "Use exactly one map_governance_ceo answer_question action for this correlation.",
                json.dumps(
                    {
                        "map_id": map_id,
                        "correlation_id": correlation_id,
                        "decision_class": question.decision_class,
                        "scope": question.scope,
                        "blocking": question.blocking,
                        "question": question.summary,
                        "evidence": list(question.evidence),
                        "options": list(question.options),
                        "continuation_requirement": question.continuation_requirement,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
        idempotency_key = f"map-governance:pm-question:{map_id}:{correlation_id}"
        effect_id = f"session-resume:{map_id}:pm-question:{correlation_id}"
        payload = {
            "root_session_id": str(session["root_session_id"]),
            "content": content,
            "idempotency_key": idempotency_key,
            "profile_name": str(session["profile_name"]),
            "canonical_identity": str(session["canonical_identity"]),
            "canonical_title": str(session["canonical_title"]),
            "bootstrap_hash": str(session["bootstrap_hash"]),
            "correlation_id": correlation_id,
            "tracker_record_url": report.tracker_record_url,
        }
        try:
            enqueued = self._outbox.enqueue(
                effect_id=effect_id,
                effect_type=SESSION_RESUME,
                map_id=map_id,
                payload=payload,
                created_at=self._synchronized_at(),
            )
        except OutboxConflictError as error:
            raise EffectTerminalError(
                "PM question correlation belongs to another CEO turn"
            ) from error
        self._outbox_dispatcher.dispatch_effect(
            effect_id=effect_id,
            owner_id=self._outbox_owner_id,
            expedite_retry=not enqueued.created,
        )
        status = self._outbox.intent(effect_id)
        if status is None:  # pragma: no cover
            raise EffectRetryableError("PM question CEO turn disappeared")
        if status.state != "succeeded":
            raise EffectRetryableError(
                status.last_error_message
                or status.terminal_reason
                or "PM question CEO turn is pending"
            )

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
                if not (
                    approval["decided_by"] == details.get("actor_id")
                    and approval["decided_by_profile"] == details.get("actor_profile")
                    and approval["decision_note"] == details.get("note")
                ):
                    raise TrackerEffectPayloadConflict(
                        "approval decision projection has different content"
                    )
            elif approval["status"] != "pending":
                raise TrackerEffectPayloadConflict(
                    "approval projection is no longer pending"
                )
            else:
                self._storage.apply_approval_decision(
                    request_id=event.request_id,
                    decision=event.event_type,
                    actor_id=str(details["actor_id"]),
                    actor_profile=str(details["actor_profile"]),
                    note=str(details["note"]),
                    decided_at=event.occurred_at,
                    expires_at=(
                        str(details["expires_at"])
                        if details.get("expires_at")
                        else None
                    ),
                    tracker_record_id=tracker_record_id,
                    tracker_record_url=tracker_record_url,
                )
            stored = self._storage.approval(event.request_id)
            if stored is None:  # pragma: no cover
                raise EffectRetryableError("Approval decision projection disappeared")
            self._enqueue_correlated_approval_resume(
                row=stored,
                tracker={"id": tracker_record_id, "url": tracker_record_url},
            )
            return
        if operation == "consumption":
            mutation_id = str(completion.get("mutation_id") or "")
            if (
                event.event_type != "consumed"
                or details.get("mutation_id") != mutation_id
                or details.get("action") != completion.get("action")
                or approval["status"] != "consumed"
                or approval.get("consumed_by_mutation_id") != mutation_id
            ):
                raise TrackerEffectPayloadConflict(
                    "approval consumption projection has different content"
                )
            close_payload = completion.get("close_payload")
            if not isinstance(close_payload, dict):
                raise EffectRetryableError(
                    "Approval consumption has no closeout payload"
                )
            try:
                self._outbox.enqueue(
                    effect_id=str(completion["close_effect_id"]),
                    effect_type=TRACKER_ISSUE_CLOSE,
                    map_id=intent.map_id,
                    payload=close_payload,
                    created_at=event.occurred_at,
                )
            except OutboxConflictError as error:
                raise TrackerEffectPayloadConflict(
                    "Cancellation closeout identity belongs to other content"
                ) from error
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
        projection = cls._configured_project(
            project,
            synchronized_at=row["synchronized_at"],
        )
        state = str(row.get("authority_state") or "healthy")
        last_success_at = str(
            row.get("authority_last_success_at") or row["synchronized_at"]
        )
        reason = row.get("authority_reason")
        source = {
            "source": "tracker",
            "state": state,
            "last_success_at": last_success_at,
            "reason": reason,
        }
        projection["authority"] = {
            "state": state,
            "last_success_at": last_success_at,
            "reason": reason,
            "sources": [source],
            "recovery": (
                "Reconnect tracker authority and complete authoritative reconcile."
                if state != "healthy"
                else None
            ),
        }
        return projection

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
            "statuses": [
                {
                    "request_id": item["request_id"],
                    "status": item["status"],
                    "requested_at": item["requested_at"],
                }
                for item in approvals["items"]
            ],
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
