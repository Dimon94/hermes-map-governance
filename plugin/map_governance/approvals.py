"""Authority-envelope and chairman-approval domain contracts."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping


CEO_AUTONOMOUS_DECISION_CLASSES = frozenset({"product", "operational"})
CHAIRMAN_REQUIRED_DECISION_CLASSES = frozenset(
    {
        "delivery_authorization",
        "budget_increase",
        "scope_expansion",
        "schedule_change",
        "security",
        "legal",
        "cancellation",
        "remote_publication",
        "final_acceptance",
    }
)
APPROVAL_DECISIONS = frozenset({"approved", "rejected", "revision"})
APPROVAL_STATUSES = frozenset(
    {"pending", "approved", "rejected", "revision", "revoked", "expired", "consumed"}
)


def _non_empty_string(value: Any, *, name: str, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    normalized = value.strip()
    if maximum is not None and len(normalized) > maximum:
        raise ValueError(f"{name} must not exceed {maximum} characters")
    return normalized


def normalized_json(value: Any) -> str:
    """Return one deterministic JSON encoding suitable for governance hashes."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Governance payload must be finite JSON data") from error


def normalized_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(normalized_json(value).encode()).hexdigest()


def _json_object(value: Any, *, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    normalized = json.loads(normalized_json(dict(value)))
    if not isinstance(normalized, dict):  # pragma: no cover - guarded above
        raise ValueError(f"{name} must be a JSON object")
    return normalized


def _string_tuple(
    value: Any, *, name: str, allow_empty: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list of strings")
    normalized = tuple(_non_empty_string(item, name=f"{name} item") for item in value)
    if not allow_empty and not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _rfc3339(value: Any, *, name: str) -> str:
    normalized = _non_empty_string(value, name=name)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be RFC 3339") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return normalized


@dataclass(frozen=True)
class AuthorityEnvelopePolicy:
    """Classify CEO-autonomous and chairman-required decision classes."""

    ceo_autonomous_decision_classes: frozenset[str]
    chairman_required_decision_classes: frozenset[str]
    authority_thresholds: dict[str, dict[str, Any]]
    chairman_actor_ids: frozenset[str]
    approval_ttl: timedelta

    @classmethod
    def from_settings(
        cls, settings: Mapping[str, Any] | None
    ) -> "AuthorityEnvelopePolicy":
        raw = dict(settings or {})
        autonomous = frozenset(
            _non_empty_string(item, name="CEO autonomous decision class")
            for item in raw.get(
                "ceo_autonomous_decision_classes",
                sorted(CEO_AUTONOMOUS_DECISION_CLASSES),
            )
        )
        chairman = frozenset(
            _non_empty_string(item, name="chairman-required decision class")
            for item in raw.get(
                "chairman_required_decision_classes",
                sorted(CHAIRMAN_REQUIRED_DECISION_CLASSES),
            )
        )
        overlap = autonomous & chairman
        if overlap:
            raise ValueError(
                "Decision classes cannot be both CEO and chairman authority: "
                + ", ".join(sorted(overlap))
            )
        raw_thresholds = raw.get("authority_thresholds", {})
        if not isinstance(raw_thresholds, Mapping):
            raise ValueError("authority_thresholds must be an object")
        thresholds = {
            _non_empty_string(decision_class, name="threshold decision class"): (
                cls._threshold_from_settings(threshold)
            )
            for decision_class, threshold in raw_thresholds.items()
        }
        unknown_thresholds = set(thresholds) - chairman
        if unknown_thresholds:
            raise ValueError(
                "Authority thresholds require chairman decision classes: "
                + ", ".join(sorted(unknown_thresholds))
            )
        raw_actor_ids = raw.get("chairman_actor_ids", ())
        if not isinstance(raw_actor_ids, (list, tuple)):
            raise ValueError("chairman_actor_ids must be a list of strings")
        actor_ids = frozenset(
            _non_empty_string(actor_id, name="chairman actor id")
            for actor_id in raw_actor_ids
        )
        raw_ttl = raw.get("approval_ttl_seconds", 24 * 60 * 60)
        if isinstance(raw_ttl, bool) or not isinstance(raw_ttl, int) or raw_ttl < 1:
            raise ValueError("approval_ttl_seconds must be a positive integer")
        return cls(
            ceo_autonomous_decision_classes=autonomous,
            chairman_required_decision_classes=chairman,
            authority_thresholds=thresholds,
            chairman_actor_ids=actor_ids,
            approval_ttl=timedelta(seconds=raw_ttl),
        )

    @staticmethod
    def _threshold_from_settings(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("Each authority threshold must be an object")
        threshold = dict(value)
        source = threshold.get("source", "decision_payload")
        if source not in {"decision_payload", "requested_scope"}:
            raise ValueError(
                "Authority threshold source must be decision_payload or requested_scope"
            )
        field = _non_empty_string(threshold.get("field"), name="threshold field")
        has_maximum = "maximum" in threshold
        has_allowed_values = "allowed_values" in threshold
        if has_maximum == has_allowed_values:
            raise ValueError(
                "Authority threshold must define exactly one of maximum or allowed_values"
            )
        normalized: dict[str, Any] = {"source": source, "field": field}
        if has_maximum:
            maximum = threshold["maximum"]
            if (
                isinstance(maximum, bool)
                or not isinstance(maximum, (int, float))
                or not math.isfinite(maximum)
                or maximum < 0
            ):
                raise ValueError(
                    "Authority threshold maximum must be finite and non-negative"
                )
            normalized["maximum"] = maximum
        else:
            allowed_values = threshold["allowed_values"]
            if not isinstance(allowed_values, (list, tuple)) or not allowed_values:
                raise ValueError("Authority threshold allowed_values must not be empty")
            normalized["allowed_values"] = json.loads(normalized_json(allowed_values))
        unknown_keys = set(threshold) - {
            "source",
            "field",
            "maximum",
            "allowed_values",
        }
        if unknown_keys:
            raise ValueError(
                "Unknown authority threshold settings: "
                + ", ".join(sorted(unknown_keys))
            )
        return normalized

    def classify(
        self,
        decision_class: str,
        *,
        decision_payload: Mapping[str, Any] | None = None,
        requested_scope: Mapping[str, Any] | None = None,
    ) -> str:
        normalized = _non_empty_string(decision_class, name="decision_class")
        if normalized in self.ceo_autonomous_decision_classes:
            return "ceo"
        if normalized not in self.chairman_required_decision_classes:
            # Unknown classes fail closed rather than silently widening authority.
            return "unconfigured"
        threshold = self.authority_thresholds.get(normalized)
        if threshold is None:
            return "chairman"
        source = decision_payload
        if threshold["source"] == "requested_scope":
            source = requested_scope
        if not isinstance(source, Mapping) or threshold["field"] not in source:
            return "chairman"
        actual = source[threshold["field"]]
        if "maximum" in threshold:
            if (
                isinstance(actual, bool)
                or not isinstance(actual, (int, float))
                or not math.isfinite(actual)
                or actual < 0
            ):
                return "chairman"
            return "ceo" if actual <= threshold["maximum"] else "chairman"
        return "ceo" if actual in threshold["allowed_values"] else "chairman"

    def authorizes_chairman_actor(self, actor_id: str) -> bool:
        return actor_id in self.chairman_actor_ids

    def projection(self) -> dict[str, Any]:
        return {
            "ceo_autonomous_decision_classes": sorted(
                self.ceo_autonomous_decision_classes
            ),
            "chairman_required_decision_classes": sorted(
                self.chairman_required_decision_classes
            ),
            "authority_thresholds": self.authority_thresholds,
            "approval_ttl_seconds": int(self.approval_ttl.total_seconds()),
        }


@dataclass(frozen=True)
class GovernanceActorIdentity:
    """Actor identity derived from one active request, never process state."""

    role: str
    profile_name: str
    actor_id: str
    session_id: str = ""


@dataclass(frozen=True)
class ApprovalPacket:
    """A complete, content-bound CEO escalation to the chairman."""

    request_id: str
    decision_class: str
    proposed_action: str
    alternatives: tuple[str, ...]
    rationale: str
    cost_risk: str
    evidence: tuple[str, ...]
    requested_scope: dict[str, Any]
    decision_payload: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_id",
            _non_empty_string(self.request_id, name="request_id", maximum=128),
        )
        object.__setattr__(
            self,
            "decision_class",
            _non_empty_string(self.decision_class, name="decision_class", maximum=128),
        )
        object.__setattr__(
            self,
            "proposed_action",
            _non_empty_string(
                self.proposed_action, name="proposed_action", maximum=128
            ),
        )
        object.__setattr__(
            self,
            "alternatives",
            _string_tuple(self.alternatives, name="alternatives"),
        )
        object.__setattr__(
            self,
            "rationale",
            _non_empty_string(self.rationale, name="rationale"),
        )
        object.__setattr__(
            self,
            "cost_risk",
            _non_empty_string(self.cost_risk, name="cost_risk"),
        )
        object.__setattr__(
            self,
            "evidence",
            _string_tuple(self.evidence, name="evidence"),
        )
        object.__setattr__(
            self,
            "requested_scope",
            _json_object(self.requested_scope, name="requested_scope"),
        )
        object.__setattr__(
            self,
            "decision_payload",
            _json_object(self.decision_payload, name="decision_payload"),
        )

    @property
    def payload_hash(self) -> str:
        return normalized_hash(
            {
                "action": self.proposed_action,
                "scope": self.requested_scope,
                "payload": self.decision_payload,
            }
        )

    @property
    def packet_hash(self) -> str:
        return normalized_hash(self.payload(include_hash=False))

    def payload(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload = {
            "request_id": self.request_id,
            "decision_class": self.decision_class,
            "proposed_action": self.proposed_action,
            "alternatives": list(self.alternatives),
            "rationale": self.rationale,
            "cost_risk": self.cost_risk,
            "evidence": list(self.evidence),
            "requested_scope": self.requested_scope,
            "decision_payload": self.decision_payload,
        }
        if include_hash:
            payload["payload_hash"] = self.payload_hash
        return payload


@dataclass(frozen=True)
class ApprovalHistoryEvent:
    """One machine-readable approval event stored in Issue history."""

    event_id: str
    request_id: str
    event_type: str
    occurred_at: str
    payload_hash: str
    details: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "event_id",
            _non_empty_string(self.event_id, name="event_id", maximum=256),
        )
        object.__setattr__(
            self,
            "request_id",
            _non_empty_string(self.request_id, name="request_id", maximum=128),
        )
        allowed = {
            "requested",
            "approved",
            "rejected",
            "revision",
            "revoked",
            "consumed",
        }
        if self.event_type not in allowed:
            raise ValueError("Approval event_type is not supported")
        object.__setattr__(
            self, "occurred_at", _rfc3339(self.occurred_at, name="occurred_at")
        )
        if not isinstance(self.payload_hash, str) or not self.payload_hash.startswith(
            "sha256:"
        ):
            raise ValueError("Approval payload_hash must be a sha256 identifier")
        object.__setattr__(
            self, "details", _json_object(self.details, name="approval event details")
        )

    def payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "request_id": self.request_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "payload_hash": self.payload_hash,
            "details": self.details,
        }


def approval_events_semantically_compatible(
    existing: ApprovalHistoryEvent,
    requested: ApprovalHistoryEvent,
) -> bool:
    """Compare the stable meaning of an approval event across retries."""
    if (
        existing.event_id != requested.event_id
        or existing.request_id != requested.request_id
        or existing.event_type != requested.event_type
        or existing.payload_hash != requested.payload_hash
    ):
        return False
    if existing.event_type == "requested":
        return existing.details == requested.details
    stable_keys = {
        "actor_id",
        "actor_profile",
        "note",
        "decision",
        "mutation_id",
        "action",
    }
    return all(
        existing.details.get(key) == requested.details.get(key)
        for key in stable_keys
        if key in existing.details or key in requested.details
    )
