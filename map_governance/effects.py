"""Production external-effect adapters used by the durable Outbox."""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from .approvals import (
    ApprovalHistoryEvent,
    approval_events_semantically_compatible,
)
from .outbox import (
    EffectAdapter,
    EffectConfirmation,
    EffectRetryableError,
    EffectTerminalError,
    OutboxIntent,
)
from .reports import PMReport, TrackerPMReportRecord
from .sessions import CanonicalSession
from .stages import executive_stage
from .tracker import (
    StructuredDecision,
    TrackerAdapter,
    TrackerApprovalRecord,
    TrackerConflictError,
    TrackerDecisionRecord,
    TrackerIssue,
)


TRACKER_STAGE_TRANSITION = "tracker.stage-transition"
TRACKER_DECISION = "tracker.decision"
TRACKER_APPROVAL_EVENT = "tracker.approval-event"
TRACKER_PM_REPORT = "tracker.pm-report"
SESSION_RESUME = "session.resume"
COORDINATOR_RESUME = "coordinator.resume"


class SessionResumeBoundary(Protocol):
    def has_resume_marker(
        self,
        *,
        root_session_id: str,
        idempotency_key: str,
    ) -> bool: ...

    def resolve(self, *, root_session_id: str) -> CanonicalSession | None: ...

    def resume_once(
        self,
        *,
        root_session_id: str,
        content: str,
        idempotency_key: str,
    ) -> CanonicalSession: ...


class CoordinatorResumeBoundary(Protocol):
    def readback(self, *, map_id: str, turn_id: str) -> Mapping[str, Any] | None: ...

    def resume(
        self,
        *,
        map_id: str,
        profile_name: str,
        session_id: str,
        coordinator_id: str,
        turn_id: str,
    ) -> None: ...


class SessionResumeEffectAdapter(EffectAdapter):
    """Resume one canonical Hermes lineage using its platform-message marker."""

    def __init__(self, boundary: SessionResumeBoundary) -> None:
        self._boundary = boundary

    def readback(self, intent: OutboxIntent) -> EffectConfirmation | None:
        payload = intent.payload
        root_session_id = str(payload["root_session_id"])
        idempotency_key = str(payload["idempotency_key"])
        if not self._boundary.has_resume_marker(
            root_session_id=root_session_id,
            idempotency_key=idempotency_key,
        ):
            return None
        session = self._boundary.resolve(root_session_id=root_session_id)
        if session is None:
            raise EffectRetryableError(
                "Hermes resume marker exists but its session lineage is unavailable"
            )
        return EffectConfirmation({"session": _session_payload(session)})

    def apply(self, intent: OutboxIntent) -> None:
        payload = intent.payload
        self._boundary.resume_once(
            root_session_id=str(payload["root_session_id"]),
            content=str(payload["content"]),
            idempotency_key=str(payload["idempotency_key"]),
        )


class CoordinatorResumeEffectAdapter(EffectAdapter):
    """Resume a controllable coordinator behind a stable turn marker."""

    def __init__(self, boundary: CoordinatorResumeBoundary) -> None:
        self._boundary = boundary

    def readback(self, intent: OutboxIntent) -> EffectConfirmation | None:
        payload = intent.payload
        acknowledgment = self._boundary.readback(
            map_id=intent.map_id,
            turn_id=str(payload["turn_id"]),
        )
        return EffectConfirmation(acknowledgment) if acknowledgment else None

    def apply(self, intent: OutboxIntent) -> None:
        payload = intent.payload
        self._boundary.resume(
            map_id=intent.map_id,
            profile_name=str(payload["profile_name"]),
            session_id=str(payload["session_id"]),
            coordinator_id=str(payload["coordinator_id"]),
            turn_id=str(payload["turn_id"]),
        )


def _session_payload(session: CanonicalSession) -> dict[str, Any]:
    return {
        "root_session_id": session.root_session_id,
        "live_session_id": session.live_session_id,
        "title": session.title,
        "last_activity_at": session.last_activity_at,
    }


class TrackerStageEffectConflict(EffectTerminalError):
    """The authoritative tracker stage no longer matches this transition."""

    def __init__(self, *, current_stage: str, requested_stage: str) -> None:
        self.current_stage = current_stage
        self.requested_stage = requested_stage
        super().__init__(f"tracker stage is {current_stage!r}, not {requested_stage!r}")


class TrackerEffectPayloadConflict(EffectTerminalError):
    """A downstream stable marker already belongs to different content."""


class TrackerEffectAdapter(EffectAdapter):
    """Apply tracker mutations and prove them through authoritative readback."""

    def __init__(self, tracker: TrackerAdapter) -> None:
        self._tracker = tracker

    def readback(self, intent: OutboxIntent) -> EffectConfirmation | None:
        if intent.effect_type == TRACKER_APPROVAL_EVENT:
            return self._approval_readback(intent)
        if intent.effect_type == TRACKER_PM_REPORT:
            return self._pm_report_readback(intent)
        if intent.effect_type == TRACKER_DECISION:
            return self._decision_readback(intent)
        if intent.effect_type != TRACKER_STAGE_TRANSITION:
            raise EffectTerminalError(
                f"Unsupported tracker effect: {intent.effect_type}"
            )
        payload = intent.payload
        issue = self._tracker.get_issue(self._required(payload, "issue_url"))
        issue_id = self._required(payload, "issue_id")
        if issue.id != issue_id:
            raise EffectTerminalError("Bound GitHub Issue identity changed")
        current_stage = self._stage(issue)
        requested_stage = self._required(payload, "requested_stage")
        if current_stage == requested_stage:
            return EffectConfirmation({"issue": self.issue_payload(issue)})
        if current_stage == self._required(payload, "expected_stage"):
            return None
        raise TrackerStageEffectConflict(
            current_stage=current_stage,
            requested_stage=requested_stage,
        )

    def apply(self, intent: OutboxIntent) -> None:
        if intent.effect_type == TRACKER_APPROVAL_EVENT:
            payload = intent.payload
            self._tracker.append_approval_event(
                self._required(payload, "issue_url"),
                issue_id=self._required(payload, "issue_id"),
                event=ApprovalHistoryEvent(**self._object(payload, "event")),
            )
            return
        if intent.effect_type == TRACKER_PM_REPORT:
            payload = intent.payload
            self._tracker.append_pm_report(
                self._required(payload, "issue_url"),
                issue_id=self._required(payload, "issue_id"),
                report=PMReport.from_payload(self._object(payload, "report")),
            )
            return
        if intent.effect_type == TRACKER_DECISION:
            payload = intent.payload
            self._tracker.append_decision(
                self._required(payload, "issue_url"),
                issue_id=self._required(payload, "issue_id"),
                decision=StructuredDecision(**self._object(payload, "decision")),
            )
            return
        payload = intent.payload
        try:
            self._tracker.transition_issue_stage(
                self._required(payload, "issue_url"),
                expected_stage=self._required(payload, "expected_stage"),
                requested_stage=self._required(payload, "requested_stage"),
            )
        except TrackerConflictError as error:
            raise TrackerStageEffectConflict(
                current_stage=error.current_stage,
                requested_stage=error.requested_stage,
            ) from error

    def _decision_readback(
        self,
        intent: OutboxIntent,
    ) -> EffectConfirmation | None:
        payload = intent.payload
        requested = StructuredDecision(**self._object(payload, "decision"))
        matching: TrackerDecisionRecord | None = None
        for record in self._tracker.list_decisions(
            self._required(payload, "issue_url")
        ):
            if record.decision.decision_id != requested.decision_id:
                continue
            if matching is not None and matching.decision != record.decision:
                raise TrackerEffectPayloadConflict(
                    "tracker decision marker has conflicting history"
                )
            matching = matching or record
        if matching is None:
            return None
        if matching.decision != requested:
            raise TrackerEffectPayloadConflict(
                "tracker decision marker belongs to another payload"
            )
        return EffectConfirmation(
            {
                "decision": matching.decision.payload(),
                "tracker_record_id": matching.tracker_record_id,
                "tracker_record_url": matching.tracker_record_url,
            }
        )

    def _approval_readback(
        self,
        intent: OutboxIntent,
    ) -> EffectConfirmation | None:
        payload = intent.payload
        requested = ApprovalHistoryEvent(**self._object(payload, "event"))
        matching: TrackerApprovalRecord | None = None
        for record in self._tracker.list_approval_events(
            self._required(payload, "issue_url")
        ):
            if record.event.event_id != requested.event_id:
                continue
            if matching is not None and matching.event != record.event:
                raise TrackerEffectPayloadConflict(
                    "tracker approval marker has conflicting history"
                )
            matching = matching or record
        if matching is None:
            return None
        if not approval_events_semantically_compatible(matching.event, requested):
            raise TrackerEffectPayloadConflict(
                "tracker approval marker belongs to another payload"
            )
        return EffectConfirmation(
            {
                "event": matching.event.payload(),
                "tracker_record_id": matching.tracker_record_id,
                "tracker_record_url": matching.tracker_record_url,
            }
        )

    def _pm_report_readback(
        self,
        intent: OutboxIntent,
    ) -> EffectConfirmation | None:
        payload = intent.payload
        requested = PMReport.from_payload(self._object(payload, "report"))
        matching: TrackerPMReportRecord | None = None
        for record in self._tracker.list_pm_reports(
            self._required(payload, "issue_url")
        ):
            if record.report.content.record_id != requested.content.record_id:
                continue
            if matching is not None and matching.report != record.report:
                raise TrackerEffectPayloadConflict(
                    "tracker PM report marker has conflicting history"
                )
            matching = matching or record
        if matching is None:
            return None
        if matching.report != requested:
            raise TrackerEffectPayloadConflict(
                "tracker PM report marker belongs to another payload"
            )
        return EffectConfirmation(
            {
                "report": matching.report.payload(),
                "tracker_record_id": matching.tracker_record_id,
                "tracker_record_url": matching.tracker_record_url,
            }
        )

    @staticmethod
    def issue_payload(issue: TrackerIssue) -> dict[str, Any]:
        return {
            "id": issue.id,
            "repository": issue.repository,
            "number": issue.number,
            "title": issue.title,
            "url": issue.url,
            "state": issue.state,
            "state_reason": issue.state_reason,
            "labels": list(issue.labels),
        }

    @staticmethod
    def issue_from_payload(payload: dict[str, Any]) -> TrackerIssue:
        return TrackerIssue(
            id=str(payload["id"]),
            repository=str(payload["repository"]),
            number=int(payload["number"]),
            title=str(payload["title"]),
            url=str(payload["url"]),
            state=str(payload["state"]),
            state_reason=(
                str(payload["state_reason"])
                if payload.get("state_reason") is not None
                else None
            ),
            labels=tuple(str(label) for label in payload["labels"]),
        )

    @staticmethod
    def _required(payload: dict[str, Any], name: str) -> str:
        value = payload.get(name)
        if not isinstance(value, str) or not value:
            raise EffectTerminalError(
                f"Tracker effect payload requires a non-empty {name}"
            )
        return value

    @staticmethod
    def _object(payload: dict[str, Any], name: str) -> dict[str, Any]:
        value = payload.get(name)
        if not isinstance(value, dict):
            raise EffectTerminalError(f"Tracker effect payload requires {name}")
        return value

    @staticmethod
    def _stage(issue: TrackerIssue) -> str:
        try:
            return executive_stage(
                issue_state=issue.state,
                state_reason=issue.state_reason,
                labels=issue.labels,
            )
        except ValueError as error:
            raise EffectTerminalError(str(error)) from error
