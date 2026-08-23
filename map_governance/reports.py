"""Structured Hermes PM reports projected onto the executive Map."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
        if self.report_type != "acceptance" and self.evidence:
            raise ValueError("Only a PM acceptance report may include evidence")
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
