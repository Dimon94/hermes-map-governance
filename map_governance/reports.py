"""Structured Hermes PM reports projected onto the executive Map."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any


PM_REPORT_TYPES = frozenset(
    {"checkpoint", "question", "blocker", "acceptance", "failure"}
)


def _text(value: Any, *, name: str, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"PM report {name} must be a non-empty string")
    normalized = value.strip()
    if maximum is not None and len(normalized) > maximum:
        raise ValueError(f"PM report {name} must not exceed {maximum} characters")
    return normalized


def _timestamp(value: Any) -> str:
    normalized = _text(value, name="timestamp")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("PM report timestamp must be RFC 3339") from error
    if parsed.tzinfo is None:
        raise ValueError("PM report timestamp must include a timezone")
    return normalized


def _evidence(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("PM report evidence must be a list of strings")
    return tuple(_text(item, name="evidence item") for item in value)


def _scope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError("PM question scope must be a non-empty object")
    try:
        normalized = json.loads(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
    except (TypeError, ValueError) as error:
        raise ValueError("PM question scope must be JSON serializable") from error
    return normalized


@dataclass(frozen=True)
class PMReportDraft:
    """Model-supplied report content before request identity assigns its Map."""

    record_id: str
    report_type: str
    summary: str
    timestamp: str
    evidence: tuple[str, ...] = ()
    blocking: bool | None = None
    continuation_requirement: str | None = None
    failure_code: str | None = None
    correlation_id: str | None = None
    decision_class: str | None = None
    scope: dict[str, Any] | None = None
    options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "record_id",
            _text(self.record_id, name="record_id", maximum=128),
        )
        if self.report_type not in PM_REPORT_TYPES:
            raise ValueError("PM report type is not supported")
        object.__setattr__(self, "summary", _text(self.summary, name="summary"))
        object.__setattr__(self, "timestamp", _timestamp(self.timestamp))
        object.__setattr__(self, "evidence", _evidence(self.evidence))
        if self.report_type not in {"question", "acceptance"} and self.evidence:
            raise ValueError(
                "Only a PM question or acceptance report may include evidence"
            )
        if self.report_type in {"question", "blocker"}:
            if not isinstance(self.blocking, bool):
                raise ValueError(
                    f"PM {self.report_type} report must declare blocking impact"
                )
            object.__setattr__(
                self,
                "continuation_requirement",
                _text(
                    self.continuation_requirement,
                    name="continuation_requirement",
                ),
            )
        elif self.blocking is not None or self.continuation_requirement is not None:
            raise ValueError(
                "Only PM question and blocker reports may declare blocking impact"
            )
        if self.report_type == "acceptance" and not self.evidence:
            raise ValueError("PM acceptance report must include evidence")
        if self.report_type == "question":
            object.__setattr__(
                self,
                "correlation_id",
                _text(self.correlation_id, name="correlation_id", maximum=128),
            )
            object.__setattr__(
                self,
                "decision_class",
                _text(self.decision_class, name="decision_class", maximum=128),
            )
            object.__setattr__(self, "scope", _scope(self.scope))
            object.__setattr__(self, "options", _evidence(self.options))
            if not self.evidence:
                raise ValueError("PM question must include decision evidence")
            if len(self.options) < 2:
                raise ValueError("PM question must include at least two options")
        elif (
            any(
                value is not None
                for value in (self.correlation_id, self.decision_class, self.scope)
            )
            or self.options
        ):
            raise ValueError(
                "Only a PM question may declare correlation, decision class, scope, "
                "or options"
            )
        if self.report_type == "failure":
            object.__setattr__(
                self,
                "failure_code",
                _text(self.failure_code, name="failure_code", maximum=128),
            )
        elif self.failure_code is not None:
            raise ValueError("Only a PM failure report may declare a failure code")

    def assign_to(self, map_id: str) -> "PMReport":
        return PMReport(assignment_map_id=map_id, content=self)

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "record_id": self.record_id,
            "type": self.report_type,
            "summary": self.summary,
            "timestamp": self.timestamp,
        }
        if self.evidence:
            payload["evidence"] = list(self.evidence)
        if self.blocking is not None:
            payload["blocking"] = self.blocking
        if self.continuation_requirement is not None:
            payload["continuation_requirement"] = self.continuation_requirement
        if self.failure_code is not None:
            payload["failure_code"] = self.failure_code
        if self.correlation_id is not None:
            payload["correlation_id"] = self.correlation_id
            payload["decision_class"] = self.decision_class
            payload["scope"] = self.scope
            payload["options"] = list(self.options)
        return payload


@dataclass(frozen=True)
class PMReport:
    """One tracker-authoritative report bound to its stable PM assignment."""

    assignment_map_id: str
    content: PMReportDraft

    def __post_init__(self) -> None:
        map_id = _text(self.assignment_map_id, name="assignment_map_id")
        object.__setattr__(self, "assignment_map_id", map_id)
        if not isinstance(self.content, PMReportDraft):
            raise TypeError("PM report content must be a PMReportDraft")

    def payload(self) -> dict[str, Any]:
        return {
            "assignment_map_id": self.assignment_map_id,
            **self.content.payload(),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "PMReport":
        normalized = dict(payload)
        assignment_map_id = normalized.pop("assignment_map_id", None)
        normalized["report_type"] = normalized.pop("type", None)
        return cls(
            assignment_map_id=assignment_map_id,
            content=PMReportDraft(**normalized),
        )


@dataclass(frozen=True)
class TrackerPMReportRecord:
    """A PM report confirmed in authoritative Issue history."""

    report: PMReport
    tracker_record_id: str
    tracker_record_url: str


@dataclass(frozen=True)
class PMDecisionResponse:
    """One CEO recommendation bound to a tracker-confirmed PM question."""

    correlation_id: str
    recommendation: str
    rationale: str
    cost_risk: str
    decision_payload: dict[str, Any]
    outcome: str
    timestamp: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "correlation_id",
            _text(self.correlation_id, name="correlation_id", maximum=128),
        )
        object.__setattr__(
            self,
            "recommendation",
            _text(self.recommendation, name="recommendation", maximum=128),
        )
        object.__setattr__(self, "rationale", _text(self.rationale, name="rationale"))
        object.__setattr__(self, "cost_risk", _text(self.cost_risk, name="cost_risk"))
        object.__setattr__(self, "decision_payload", _scope(self.decision_payload))
        if self.outcome not in {"continue", "blocked"}:
            raise ValueError("PM decision response outcome must be continue or blocked")
        object.__setattr__(self, "timestamp", _timestamp(self.timestamp))

    def payload(self) -> dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "recommendation": self.recommendation,
            "rationale": self.rationale,
            "cost_risk": self.cost_risk,
            "decision_payload": self.decision_payload,
            "outcome": self.outcome,
            "timestamp": self.timestamp,
        }
