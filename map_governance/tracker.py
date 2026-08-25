"""Tracker boundary and GitHub implementation for Map Issues and Projects."""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence, TypeVar
from urllib.parse import urlparse

import yaml

from .approvals import ApprovalHistoryEvent, normalized_json
from .coordinator import DeliveryLaneRegistry
from .reports import PMReport, TrackerPMReportRecord
from .publication import PublicationRecord
from .stages import ACTIVE_STAGES, executive_stage


StructuredRecord = TypeVar("StructuredRecord")


class TrackerError(RuntimeError):
    """Raised when tracker data cannot be read or violates the contract."""


class TrackerConflictError(TrackerError):
    """Raised when tracker truth changed after application validation."""

    def __init__(self, *, current_stage: str, requested_stage: str) -> None:
        self.current_stage = current_stage
        self.requested_stage = requested_stage
        super().__init__(
            f"Tracker stage changed to {current_stage!r} while requesting "
            f"{requested_stage!r}"
        )


@dataclass(frozen=True)
class TrackerProject:
    id: str
    owner: str
    owner_type: str
    number: int
    title: str
    url: str


@dataclass(frozen=True)
class TrackerIssue:
    id: str
    repository: str
    number: int
    title: str
    url: str
    state: str
    state_reason: str | None
    labels: tuple[str, ...]
    body: str = ""


@dataclass(frozen=True)
class StructuredDecision:
    """A CEO decision whose id is also its stable idempotency identity."""

    decision_id: str
    type: str
    rationale: str
    authority: str
    affected_stage: str
    timestamp: str
    authority_context: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        values = {
            "decision_id": self.decision_id,
            "type": self.type,
            "rationale": self.rationale,
            "authority": self.authority,
            "affected_stage": self.affected_stage,
            "timestamp": self.timestamp,
        }
        for name, value in values.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Decision {name} must be a non-empty string")
        if len(self.decision_id) > 128:
            raise ValueError("Decision decision_id must not exceed 128 characters")
        if self.affected_stage not in ACTIVE_STAGES | {"done", "cancelled"}:
            raise ValueError("Decision affected_stage is not supported")
        try:
            parsed = datetime.fromisoformat(self.timestamp.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Decision timestamp must be RFC 3339") from error
        if parsed.tzinfo is None:
            raise ValueError("Decision timestamp must include a timezone")
        if self.authority_context is not None:
            if not isinstance(self.authority_context, Mapping):
                raise ValueError("Decision authority_context must be an object")
            context = json.loads(normalized_json(dict(self.authority_context)))
            allowed = {"decision_payload", "requested_scope"}
            if set(context) - allowed:
                raise ValueError(
                    "Decision authority_context only supports decision_payload "
                    "and requested_scope"
                )
            for name, value in context.items():
                if not isinstance(value, dict):
                    raise ValueError(
                        f"Decision authority_context {name} must be an object"
                    )
            object.__setattr__(self, "authority_context", context)

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "decision_id": self.decision_id,
            "type": self.type,
            "rationale": self.rationale,
            "authority": self.authority,
            "affected_stage": self.affected_stage,
            "timestamp": self.timestamp,
        }
        if self.authority_context is not None:
            payload["authority_context"] = self.authority_context
        return payload


@dataclass(frozen=True)
class TrackerDecisionRecord:
    """A structured decision confirmed in authoritative tracker history."""

    decision: StructuredDecision
    tracker_record_id: str
    tracker_record_url: str


@dataclass(frozen=True)
class TrackerApprovalRecord:
    """A structured approval event confirmed in authoritative Issue history."""

    event: ApprovalHistoryEvent
    tracker_record_id: str
    tracker_record_url: str


@dataclass(frozen=True)
class TrackerPublicationRecord:
    """Immutable publication evidence confirmed in Map Issue history."""

    record: PublicationRecord
    tracker_record_id: str
    tracker_record_url: str


@dataclass(frozen=True)
class TrackerDeliveryLaneRegistryRecord:
    """One delivery-pipeline registry checkpoint read from ticket history."""

    registry: DeliveryLaneRegistry
    tracker_record_id: str
    tracker_record_url: str


class TrackerAdapter(Protocol):
    def get_project(self, url: str) -> TrackerProject: ...

    def get_issue(self, url: str) -> TrackerIssue: ...

    def transition_issue_stage(
        self,
        url: str,
        *,
        expected_stage: str,
        requested_stage: str,
    ) -> TrackerIssue: ...

    def list_decisions(self, url: str) -> list[TrackerDecisionRecord]: ...

    def append_decision(
        self,
        url: str,
        *,
        issue_id: str,
        decision: StructuredDecision,
    ) -> TrackerDecisionRecord: ...

    def list_approval_events(self, url: str) -> list[TrackerApprovalRecord]: ...

    def append_approval_event(
        self,
        url: str,
        *,
        issue_id: str,
        event: ApprovalHistoryEvent,
    ) -> TrackerApprovalRecord: ...

    def list_pm_reports(self, url: str) -> list[TrackerPMReportRecord]: ...

    def append_pm_report(
        self,
        url: str,
        *,
        issue_id: str,
        report: PMReport,
    ) -> TrackerPMReportRecord: ...

    def list_publication_records(self, url: str) -> list[TrackerPublicationRecord]: ...

    def append_publication_record(
        self,
        url: str,
        *,
        issue_id: str,
        record: PublicationRecord,
    ) -> TrackerPublicationRecord: ...

    def close_issue(
        self,
        url: str,
        *,
        issue_id: str,
        state_reason: str,
    ) -> TrackerIssue: ...

    def list_delivery_lane_registries(
        self, url: str
    ) -> list[TrackerDeliveryLaneRegistryRecord]: ...

    def append_delivery_lane_registry(
        self,
        url: str,
        *,
        issue_id: str,
        registry: DeliveryLaneRegistry,
    ) -> TrackerDeliveryLaneRegistryRecord: ...


class ProjectTrackerRouter:
    """Route tracker calls through one cached client per configured project."""

    def __init__(
        self,
        *,
        tracker_for_project: Callable[[str], TrackerAdapter],
        project_url_for_resource: Callable[[str], str | None],
    ) -> None:
        self._tracker_for_project = tracker_for_project
        self._project_url_for_resource = project_url_for_resource
        self._active_project_url: ContextVar[str | None] = ContextVar(
            "map_governance_project_tracker_url",
            default=None,
        )
        self._clients: dict[str, TrackerAdapter] = {}
        self._clients_lock = Lock()

    @contextmanager
    def for_project(self, project_url: str) -> Iterator[None]:
        token = self._active_project_url.set(project_url)
        try:
            yield
        finally:
            self._active_project_url.reset(token)

    def _client(self, project_url: str) -> TrackerAdapter:
        with self._clients_lock:
            client = self._clients.get(project_url)
            if client is None:
                client = self._tracker_for_project(project_url)
                self._clients[project_url] = client
            return client

    def __getattr__(self, name: str) -> Any:
        def routed(resource_url: str, *args: Any, **kwargs: Any) -> Any:
            project_url = self._active_project_url.get()
            if project_url is None:
                project_url = self._project_url_for_resource(resource_url)
            if project_url is None and name == "get_project":
                project_url = resource_url
            if project_url is None:
                raise TrackerError(
                    "Tracker resource has no configured project authority"
                )
            operation = getattr(self._client(project_url), name)
            return operation(resource_url, *args, **kwargs)

        return routed


class CommandRunner(Protocol):
    def run(self, arguments: Sequence[str]) -> str: ...


class SubprocessCommandRunner:
    """Run one argv-safe command and return its standard output."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        redact_errors: bool = False,
    ) -> None:
        self._environment = dict(environment) if environment is not None else None
        self._redact_errors = redact_errors

    def run(self, arguments: Sequence[str]) -> str:
        try:
            completed = subprocess.run(
                list(arguments),
                env=self._environment,
                capture_output=True,
                check=False,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TrackerError(f"GitHub CLI request failed: {error}") from error
        if completed.returncode != 0:
            if self._redact_errors:
                raise TrackerError(
                    f"GitHub CLI request failed with exit status {completed.returncode}"
                )
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise TrackerError(f"GitHub CLI request failed: {detail}")
        return completed.stdout


_ORGANIZATION_PROJECT_QUERY = """
query MapGovernanceOrganizationProject($owner: String!, $number: Int!) {
  organization(login: $owner) {
    projectV2(number: $number) {
      id
      number
      title
      url
      owner {
        __typename
        ... on Organization { login }
        ... on User { login }
      }
    }
  }
}
"""


_USER_PROJECT_QUERY = _ORGANIZATION_PROJECT_QUERY.replace(
    "MapGovernanceOrganizationProject",
    "MapGovernanceUserProject",
).replace("organization(login: $owner)", "user(login: $owner)")


_ISSUE_QUERY = """
query MapGovernanceIssue($url: URI!) {
  resource(url: $url) {
    __typename
    ... on Issue {
      id
      number
      title
      body
      url
      state
      stateReason
      repository { nameWithOwner }
      labels(first: 100) {
        nodes { name }
        pageInfo { hasNextPage }
      }
    }
  }
}
"""


_STAGE_TRANSITION_CONTEXT_QUERY = """
query MapGovernanceStageTransitionContext($url: URI!, $label: String!) {
  resource(url: $url) {
    __typename
    ... on Issue {
      id
      state
      stateReason
      labels(first: 100) {
        nodes { id name }
        pageInfo { hasNextPage }
      }
      repository {
        label(name: $label) { id name }
      }
    }
  }
}
"""


_UPDATE_ISSUE_LABELS_MUTATION = """
mutation MapGovernanceUpdateIssueStage($issue: ID!, $labels: [ID!]!) {
  updateIssue(input: {id: $issue, labelIds: $labels}) {
    issue { id }
  }
}
"""


_APPEND_DECISION_MUTATION = """
mutation MapGovernanceAppendDecision($subject: ID!, $body: String!) {
  addComment(input: {subjectId: $subject, body: $body}) {
    commentEdge {
      node { id url body }
    }
  }
}
"""


_APPEND_APPROVAL_MUTATION = _APPEND_DECISION_MUTATION.replace(
    "MapGovernanceAppendDecision",
    "MapGovernanceAppendApproval",
)


_APPEND_PM_REPORT_MUTATION = _APPEND_DECISION_MUTATION.replace(
    "MapGovernanceAppendDecision",
    "MapGovernanceAppendPMReport",
)


_APPEND_PUBLICATION_MUTATION = _APPEND_DECISION_MUTATION.replace(
    "MapGovernanceAppendDecision",
    "MapGovernanceAppendPublication",
)


_CLOSE_ISSUE_MUTATION = """
mutation MapGovernanceCloseIssue(
  $issue: ID!,
  $stateReason: IssueClosedStateReason!
) {
  updateIssue(input: {
    id: $issue,
    state: CLOSED,
    stateReason: $stateReason
  }) {
    issue { id state stateReason }
  }
}
"""


_APPEND_DELIVERY_LANE_MUTATION = _APPEND_DECISION_MUTATION.replace(
    "MapGovernanceAppendDecision",
    "MapGovernanceAppendDeliveryLaneRegistry",
)


_DECISION_HISTORY_QUERY = """
query MapGovernanceDecisionHistory($url: URI!, $after: String) {
  resource(url: $url) {
    __typename
    ... on Issue {
      id
      comments(first: 100, after: $after) {
        nodes { id url body }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


_APPROVAL_HISTORY_QUERY = _DECISION_HISTORY_QUERY.replace(
    "MapGovernanceDecisionHistory",
    "MapGovernanceApprovalHistory",
)


_PM_REPORT_HISTORY_QUERY = _DECISION_HISTORY_QUERY.replace(
    "MapGovernanceDecisionHistory",
    "MapGovernancePMReportHistory",
)


_PUBLICATION_HISTORY_QUERY = _DECISION_HISTORY_QUERY.replace(
    "MapGovernanceDecisionHistory",
    "MapGovernancePublicationHistory",
)


_DELIVERY_LANE_HISTORY_QUERY = _DECISION_HISTORY_QUERY.replace(
    "MapGovernanceDecisionHistory",
    "MapGovernanceDeliveryLaneHistory",
)


_DECISION_MARKER_PREFIX = "<!-- map-governance:decision:v1 "
_DECISION_MARKER_SUFFIX = " -->"
_APPROVAL_MARKER_PREFIX = "<!-- map-governance:approval:v1 "
_APPROVAL_MARKER_SUFFIX = " -->"
_PM_REPORT_MARKER_PREFIX = "<!-- map-governance:pm-report:v1 "
_PM_REPORT_MARKER_SUFFIX = " -->"
_PUBLICATION_MARKER_PREFIX = "<!-- map-governance:publication:v1 "
_PUBLICATION_MARKER_SUFFIX = " -->"
_DELIVERY_LANE_MARKER = "<!-- wayfinder-lane-registry:v1 -->"


class GitHubTrackerAdapter:
    """Read GitHub tracker truth through the authenticated ``gh`` CLI."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        executable: str = "gh",
    ) -> None:
        self._runner = runner or SubprocessCommandRunner()
        self._executable = executable

    def get_project(self, url: str) -> TrackerProject:
        owner_type, owner_login, project_number = self._parse_project_url(url)
        query = (
            _ORGANIZATION_PROJECT_QUERY
            if owner_type == "organization"
            else _USER_PROJECT_QUERY
        )
        payload = self._graphql(
            query,
            owner=owner_login,
            number=str(project_number),
        )
        owner_field = "organization" if owner_type == "organization" else "user"
        try:
            resource = payload["data"][owner_field]["projectV2"]
        except (KeyError, TypeError) as error:
            raise TrackerError(f"GitHub Project was not found: {url}") from error
        if not isinstance(resource, dict):
            raise TrackerError(f"GitHub Project was not found: {url}")
        owner = resource.get("owner")
        if not isinstance(owner, dict) or not owner.get("login"):
            raise TrackerError(f"GitHub Project has no readable owner: {url}")
        owner_type = {
            "Organization": "organization",
            "User": "user",
        }.get(owner.get("__typename"))
        if owner_type is None:
            raise TrackerError(f"GitHub Project has an unsupported owner: {url}")
        try:
            return TrackerProject(
                id=str(resource["id"]),
                owner=str(owner["login"]),
                owner_type=owner_type,
                number=int(resource["number"]),
                title=str(resource["title"]),
                url=str(resource["url"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise TrackerError(
                f"GitHub Project response is incomplete: {url}"
            ) from error

    def get_issue(self, url: str) -> TrackerIssue:
        resource = self._resource(_ISSUE_QUERY, url)
        if resource.get("__typename") != "Issue":
            raise TrackerError(f"GitHub resource is not an Issue: {url}")
        labels = resource.get("labels")
        if not isinstance(labels, dict):
            raise TrackerError(f"GitHub Issue labels are incomplete: {url}")
        if labels.get("pageInfo", {}).get("hasNextPage"):
            raise TrackerError(
                "GitHub Issue has more than 100 labels; cannot prove one active stage"
            )
        nodes = labels.get("nodes")
        if not isinstance(nodes, list):
            raise TrackerError(f"GitHub Issue labels are incomplete: {url}")
        try:
            return TrackerIssue(
                id=str(resource["id"]),
                repository=str(resource["repository"]["nameWithOwner"]),
                number=int(resource["number"]),
                title=str(resource["title"]),
                body=str(resource.get("body") or ""),
                url=str(resource["url"]),
                state=str(resource["state"]).lower(),
                state_reason=(
                    str(resource["stateReason"]).lower()
                    if resource.get("stateReason") is not None
                    else None
                ),
                labels=tuple(str(node["name"]) for node in nodes),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise TrackerError(f"GitHub Issue response is incomplete: {url}") from error

    def transition_issue_stage(
        self,
        url: str,
        *,
        expected_stage: str,
        requested_stage: str,
    ) -> TrackerIssue:
        """Atomically replace the active stage label and verify tracker truth."""
        requested_label = f"map-stage/{requested_stage}"
        resource = self._resource(
            _STAGE_TRANSITION_CONTEXT_QUERY,
            url,
            label=requested_label,
        )
        if resource.get("__typename") != "Issue":
            raise TrackerError(f"GitHub resource is not an Issue: {url}")
        current_stage = self._resource_stage(resource)
        if current_stage != expected_stage:
            raise TrackerConflictError(
                current_stage=current_stage,
                requested_stage=requested_stage,
            )

        labels = resource.get("labels")
        if not isinstance(labels, dict) or labels.get("pageInfo", {}).get(
            "hasNextPage"
        ):
            raise TrackerError(
                "GitHub Issue has more than 100 labels; refusing a partial replacement"
            )
        nodes = labels.get("nodes")
        if not isinstance(nodes, list):
            raise TrackerError(f"GitHub Issue labels are incomplete: {url}")
        try:
            retained_label_ids = [
                str(node["id"])
                for node in nodes
                if not str(node["name"]).startswith("map-stage/")
            ]
            issue_id = str(resource["id"])
            requested = resource["repository"]["label"]
            requested_label_id = str(requested["id"])
            resolved_label_name = str(requested["name"])
        except (KeyError, TypeError) as error:
            raise TrackerError(
                f"GitHub stage transition context is incomplete: {url}"
            ) from error
        if resolved_label_name != requested_label:
            raise TrackerError(
                f"GitHub repository is missing required label: {requested_label}"
            )

        payload = self._graphql(
            _UPDATE_ISSUE_LABELS_MUTATION,
            issue=issue_id,
            labels=(*retained_label_ids, requested_label_id),
        )
        try:
            mutated_issue = payload["data"]["updateIssue"]["issue"]
        except (KeyError, TypeError) as error:
            raise TrackerError(
                "GitHub stage mutation response is incomplete"
            ) from error
        if (
            not isinstance(mutated_issue, dict)
            or str(mutated_issue.get("id")) != issue_id
        ):
            raise TrackerError(
                "GitHub stage mutation did not return the requested Issue"
            )

        committed = self.get_issue(url)
        if committed.id != issue_id:
            raise TrackerError("GitHub Issue identity changed after stage mutation")
        committed_stage = self._stage_or_invalid(
            issue_state=committed.state,
            state_reason=committed.state_reason,
            labels=committed.labels,
        )
        if committed_stage != requested_stage:
            raise TrackerConflictError(
                current_stage=committed_stage,
                requested_stage=requested_stage,
            )
        return committed

    def append_decision(
        self,
        url: str,
        *,
        issue_id: str,
        decision: StructuredDecision,
    ) -> TrackerDecisionRecord:
        """Append one structured CEO decision comment to a bound Issue."""
        body = self._decision_comment_body(decision)
        committed_decision, record_id, record_url = self._append_structured_comment(
            query=_APPEND_DECISION_MUTATION,
            subject_id=issue_id,
            body=body,
            parser=self._decision_from_comment,
            expected=decision,
            record_kind="decision",
            requested_kind="decision",
            url=url,
        )
        return TrackerDecisionRecord(
            decision=committed_decision,
            tracker_record_id=record_id,
            tracker_record_url=record_url,
        )

    def list_decisions(self, url: str) -> list[TrackerDecisionRecord]:
        """Read structured CEO decisions from the complete Issue history."""
        return [
            TrackerDecisionRecord(
                decision=self._decision_from_comment(body),
                tracker_record_id=record_id,
                tracker_record_url=record_url,
            )
            for body, record_id, record_url in self._structured_comment_history(
                url,
                query=_DECISION_HISTORY_QUERY,
                marker_prefix=_DECISION_MARKER_PREFIX,
                record_kind="decision",
            )
        ]

    def append_approval_event(
        self,
        url: str,
        *,
        issue_id: str,
        event: ApprovalHistoryEvent,
    ) -> TrackerApprovalRecord:
        """Append one human- and machine-readable approval history event."""
        committed_event, record_id, record_url = self._append_structured_comment(
            query=_APPEND_APPROVAL_MUTATION,
            subject_id=issue_id,
            body=self._approval_comment_body(event),
            parser=self._approval_from_comment,
            expected=event,
            record_kind="approval",
            requested_kind="approval event",
            url=url,
        )
        return TrackerApprovalRecord(
            event=committed_event,
            tracker_record_id=record_id,
            tracker_record_url=record_url,
        )

    def list_approval_events(self, url: str) -> list[TrackerApprovalRecord]:
        """Read all structured approval events from Issue history."""
        return [
            TrackerApprovalRecord(
                event=self._approval_from_comment(body),
                tracker_record_id=record_id,
                tracker_record_url=record_url,
            )
            for body, record_id, record_url in self._structured_comment_history(
                url,
                query=_APPROVAL_HISTORY_QUERY,
                marker_prefix=_APPROVAL_MARKER_PREFIX,
                record_kind="approval",
            )
        ]

    def append_pm_report(
        self,
        url: str,
        *,
        issue_id: str,
        report: PMReport,
    ) -> TrackerPMReportRecord:
        """Append one executive PM report to a bound Map Issue."""
        committed_report, record_id, record_url = self._append_structured_comment(
            query=_APPEND_PM_REPORT_MUTATION,
            subject_id=issue_id,
            body=self._pm_report_comment_body(report),
            parser=self._pm_report_from_comment,
            expected=report,
            record_kind="PM report",
            requested_kind="PM report",
            url=url,
        )
        return TrackerPMReportRecord(
            report=committed_report,
            tracker_record_id=record_id,
            tracker_record_url=record_url,
        )

    def list_pm_reports(self, url: str) -> list[TrackerPMReportRecord]:
        """Read all structured PM reports from complete Issue history."""
        return [
            TrackerPMReportRecord(
                report=self._pm_report_from_comment(body),
                tracker_record_id=record_id,
                tracker_record_url=record_url,
            )
            for body, record_id, record_url in self._structured_comment_history(
                url,
                query=_PM_REPORT_HISTORY_QUERY,
                marker_prefix=_PM_REPORT_MARKER_PREFIX,
                record_kind="PM report",
            )
        ]

    def append_publication_record(
        self,
        url: str,
        *,
        issue_id: str,
        record: PublicationRecord,
    ) -> TrackerPublicationRecord:
        """Append and read back immutable publication evidence or an incident."""
        committed, record_id, record_url = self._append_structured_comment(
            query=_APPEND_PUBLICATION_MUTATION,
            subject_id=issue_id,
            body=self._publication_comment_body(record),
            parser=self._publication_from_comment,
            expected=record,
            record_kind="publication record",
            requested_kind="publication record",
            url=url,
        )
        return TrackerPublicationRecord(
            record=committed,
            tracker_record_id=record_id,
            tracker_record_url=record_url,
        )

    def list_publication_records(self, url: str) -> list[TrackerPublicationRecord]:
        """Read publication evidence and incidents from complete Issue history."""
        return [
            TrackerPublicationRecord(
                record=self._publication_from_comment(body),
                tracker_record_id=record_id,
                tracker_record_url=record_url,
            )
            for body, record_id, record_url in self._structured_comment_history(
                url,
                query=_PUBLICATION_HISTORY_QUERY,
                marker_prefix=_PUBLICATION_MARKER_PREFIX,
                record_kind="publication record",
            )
        ]

    def close_issue(
        self,
        url: str,
        *,
        issue_id: str,
        state_reason: str,
    ) -> TrackerIssue:
        """Close one exact Issue and verify its authoritative close reason."""
        if state_reason not in {"completed", "not_planned"}:
            raise ValueError("Issue close reason is not supported")
        current = self.get_issue(url)
        if current.id != issue_id:
            raise TrackerError("Bound GitHub Issue identity changed")
        if current.state == "closed":
            if current.state_reason == state_reason:
                return current
            raise TrackerError("GitHub Issue is already closed with another reason")
        payload = self._graphql(
            _CLOSE_ISSUE_MUTATION,
            issue=issue_id,
            stateReason=("COMPLETED" if state_reason == "completed" else "NOT_PLANNED"),
        )
        try:
            mutated = payload["data"]["updateIssue"]["issue"]
        except (KeyError, TypeError) as error:
            raise TrackerError("GitHub Issue close response is incomplete") from error
        if not isinstance(mutated, dict) or str(mutated.get("id")) != issue_id:
            raise TrackerError("GitHub did not close the requested Issue")
        committed = self.get_issue(url)
        if (
            committed.id != issue_id
            or committed.state != "closed"
            or committed.state_reason != state_reason
        ):
            raise TrackerError("GitHub Issue close was not confirmed by readback")
        return committed

    def append_delivery_lane_registry(
        self,
        url: str,
        *,
        issue_id: str,
        registry: DeliveryLaneRegistry,
    ) -> TrackerDeliveryLaneRegistryRecord:
        """Append and read back one delivery-pipeline lane checkpoint."""
        committed, record_id, record_url = self._append_structured_comment(
            query=_APPEND_DELIVERY_LANE_MUTATION,
            subject_id=issue_id,
            body=self._delivery_lane_registry_body(registry),
            parser=self._delivery_lane_registry_from_comment,
            expected=registry,
            record_kind="delivery lane registry",
            requested_kind="delivery lane registry",
            url=url,
        )
        return TrackerDeliveryLaneRegistryRecord(
            registry=committed,
            tracker_record_id=record_id,
            tracker_record_url=record_url,
        )

    def list_delivery_lane_registries(
        self, url: str
    ) -> list[TrackerDeliveryLaneRegistryRecord]:
        """Read delivery-pipeline lane checkpoints from ticket history."""
        return [
            TrackerDeliveryLaneRegistryRecord(
                registry=self._delivery_lane_registry_from_comment(body),
                tracker_record_id=record_id,
                tracker_record_url=record_url,
            )
            for body, record_id, record_url in self._structured_comment_history(
                url,
                query=_DELIVERY_LANE_HISTORY_QUERY,
                marker_prefix=_DELIVERY_LANE_MARKER,
                record_kind="delivery lane registry",
            )
        ]

    def _append_structured_comment(
        self,
        *,
        query: str,
        subject_id: str,
        body: str,
        parser: Callable[[str], StructuredRecord],
        expected: StructuredRecord,
        record_kind: str,
        requested_kind: str,
        url: str,
    ) -> tuple[StructuredRecord, str, str]:
        payload = self._graphql(query, subject=subject_id, body=body)
        try:
            node = payload["data"]["addComment"]["commentEdge"]["node"]
            record_id = str(node["id"])
            record_url = str(node["url"])
            committed_body = str(node["body"])
        except (KeyError, TypeError) as error:
            raise TrackerError(
                f"GitHub {record_kind} mutation response is incomplete"
            ) from error
        committed = parser(committed_body)
        if committed != expected:
            raise TrackerError(
                f"GitHub did not return the requested {requested_kind} comment: {url}"
            )
        return committed, record_id, record_url

    def _structured_comment_history(
        self,
        url: str,
        *,
        query: str,
        marker_prefix: str,
        record_kind: str,
    ) -> list[tuple[str, str, str]]:
        """Read one complete typed structured-comment stream."""
        records: list[tuple[str, str, str]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            variables = {"url": url}
            if cursor is not None:
                variables["after"] = cursor
            resource = self._resource(query, **variables)
            if resource.get("__typename") != "Issue":
                raise TrackerError(f"GitHub resource is not an Issue: {url}")
            comments = resource.get("comments")
            if not isinstance(comments, dict):
                raise TrackerError(f"GitHub Issue comments are incomplete: {url}")
            nodes = comments.get("nodes")
            page_info = comments.get("pageInfo")
            if not isinstance(nodes, list) or not isinstance(page_info, dict):
                raise TrackerError(f"GitHub Issue comments are incomplete: {url}")
            for node in nodes:
                if not isinstance(node, dict):
                    raise TrackerError(
                        f"GitHub Issue comment response is incomplete: {url}"
                    )
                body = node.get("body")
                if not isinstance(body, str) or marker_prefix not in body:
                    continue
                try:
                    records.append((body, str(node["id"]), str(node["url"])))
                except KeyError as error:
                    raise TrackerError(
                        f"GitHub {record_kind} comment response is incomplete: {url}"
                    ) from error
            if not page_info.get("hasNextPage"):
                return records
            next_cursor = page_info.get("endCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise TrackerError(
                    f"GitHub {record_kind} history pagination is incomplete"
                )
            if next_cursor in seen_cursors:
                raise TrackerError(
                    f"GitHub {record_kind} history pagination did not advance"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    @staticmethod
    def _decision_comment_body(decision: StructuredDecision) -> str:
        marker = json.dumps(
            decision.payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        lines = [
            f"{_DECISION_MARKER_PREFIX}{marker}{_DECISION_MARKER_SUFFIX}",
            f"## CEO decision · {decision.decision_id}",
            "",
            f"- Type: {decision.type}",
            f"- Authority: {decision.authority}",
            f"- Affected stage: {decision.affected_stage}",
            f"- Timestamp: {decision.timestamp}",
        ]
        if decision.authority_context is not None:
            lines.append(
                "- Authority context: `"
                + normalized_json(decision.authority_context)
                + "`"
            )
        lines.extend(("", decision.rationale))
        return "\n".join(lines)

    @staticmethod
    def _decision_from_comment(body: str) -> StructuredDecision:
        try:
            payload = GitHubTrackerAdapter._structured_marker_payload(
                body,
                prefix=_DECISION_MARKER_PREFIX,
                suffix=_DECISION_MARKER_SUFFIX,
                record_kind="decision",
            )
            return StructuredDecision(**payload)
        except (TypeError, ValueError) as error:
            raise TrackerError("GitHub decision comment marker is invalid") from error

    @staticmethod
    def _approval_comment_body(event: ApprovalHistoryEvent) -> str:
        marker = json.dumps(
            event.payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        title = {
            "requested": "Approval requested",
            "approved": "Chairman approved",
            "rejected": "Chairman rejected",
            "revision": "Chairman requested revision",
            "revoked": "Chairman revoked approval",
            "consumed": "Approved action consumed",
        }[event.event_type]
        details = event.details
        lines = [
            f"{_APPROVAL_MARKER_PREFIX}{marker}{_APPROVAL_MARKER_SUFFIX}",
            f"## {title} · {event.request_id}",
            "",
            f"- Event: {event.event_type}",
            f"- Payload hash: `{event.payload_hash}`",
            f"- Timestamp: {event.occurred_at}",
        ]
        if details.get("decision_class"):
            lines.append(f"- Decision class: {details['decision_class']}")
        if details.get("proposed_action"):
            lines.append(f"- Proposed action: {details['proposed_action']}")
        if details.get("expires_at"):
            lines.append(f"- Expires: {details['expires_at']}")
        if details.get("actor_id"):
            lines.append(f"- Actor: {details['actor_id']}")
        if details.get("note"):
            lines.extend(("", str(details["note"])))
        elif details.get("rationale"):
            lines.extend(("", str(details["rationale"])))
        return "\n".join(lines)

    @staticmethod
    def _approval_from_comment(body: str) -> ApprovalHistoryEvent:
        try:
            payload = GitHubTrackerAdapter._structured_marker_payload(
                body,
                prefix=_APPROVAL_MARKER_PREFIX,
                suffix=_APPROVAL_MARKER_SUFFIX,
                record_kind="approval",
            )
            return ApprovalHistoryEvent(**payload)
        except (TypeError, ValueError) as error:
            raise TrackerError("GitHub approval comment marker is invalid") from error

    @staticmethod
    def _pm_report_comment_body(report: PMReport) -> str:
        content = report.content
        marker = json.dumps(
            report.payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        lines = [
            f"{_PM_REPORT_MARKER_PREFIX}{marker}{_PM_REPORT_MARKER_SUFFIX}",
            f"## PM {content.report_type} · {content.record_id}",
            "",
            f"- Assigned Map: {report.assignment_map_id}",
            f"- Timestamp: {content.timestamp}",
        ]
        if content.blocking is not None:
            lines.append(f"- Blocking: {'yes' if content.blocking else 'no'}")
        if content.continuation_requirement is not None:
            lines.append(f"- Needed to continue: {content.continuation_requirement}")
        if content.correlation_id is not None:
            lines.extend(
                (
                    f"- Correlation: {content.correlation_id}",
                    f"- Decision class: {content.decision_class}",
                    "- Scope: `" + normalized_json(content.scope) + "`",
                )
            )
        if content.failure_code is not None:
            lines.append(f"- Failure code: {content.failure_code}")
        lines.extend(("", content.summary))
        if content.options:
            lines.extend(("", "### Options"))
            lines.extend(f"- {item}" for item in content.options)
        if content.evidence:
            lines.extend(("", "### Executive evidence"))
            lines.extend(f"- {item}" for item in content.evidence)
        return "\n".join(lines)

    @staticmethod
    def _pm_report_from_comment(body: str) -> PMReport:
        try:
            payload = GitHubTrackerAdapter._structured_marker_payload(
                body,
                prefix=_PM_REPORT_MARKER_PREFIX,
                suffix=_PM_REPORT_MARKER_SUFFIX,
                record_kind="PM report",
            )
            return PMReport.from_payload(payload)
        except (TypeError, ValueError) as error:
            raise TrackerError("GitHub PM report comment marker is invalid") from error

    @staticmethod
    def _publication_comment_body(record: PublicationRecord) -> str:
        marker = json.dumps(
            record.payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        title = (
            "Remote publication confirmed"
            if record.status == "succeeded"
            else "Publication repair required"
        )
        lines = [
            f"{_PUBLICATION_MARKER_PREFIX}{marker}{_PUBLICATION_MARKER_SUFFIX}",
            f"## {title} · {record.action_id}",
            "",
            f"- Status: {record.status}",
            f"- Revision: `{record.revision}`",
            f"- Action: {record.action}",
            "- Target: `" + normalized_json(record.target) + "`",
            f"- Approval: {record.approval_request_id}",
            f"- Timestamp: {record.occurred_at}",
        ]
        if record.evidence is not None:
            lines.extend(
                (
                    f"- Provider: {record.evidence.provider}",
                    f"- Remote evidence: {record.evidence.remote_url}",
                )
            )
        if record.reason is not None:
            lines.extend(("", record.reason))
        return "\n".join(lines)

    @staticmethod
    def _publication_from_comment(body: str) -> PublicationRecord:
        try:
            payload = GitHubTrackerAdapter._structured_marker_payload(
                body,
                prefix=_PUBLICATION_MARKER_PREFIX,
                suffix=_PUBLICATION_MARKER_SUFFIX,
                record_kind="publication record",
            )
            return PublicationRecord.from_payload(payload)
        except (TypeError, ValueError) as error:
            raise TrackerError(
                "GitHub publication comment marker is invalid"
            ) from error

    @staticmethod
    def _delivery_lane_registry_body(registry: DeliveryLaneRegistry) -> str:
        payload = json.dumps(registry.payload(), ensure_ascii=False, indent=2)
        return f"{_DELIVERY_LANE_MARKER}\n{payload}\n"

    @staticmethod
    def _delivery_lane_registry_from_comment(body: str) -> DeliveryLaneRegistry:
        lines = body.splitlines()
        try:
            marker_index = lines.index(_DELIVERY_LANE_MARKER)
            payload = yaml.safe_load("\n".join(lines[marker_index + 1 :]))
            if not isinstance(payload, dict):
                raise ValueError("registry payload is not an object")
            return DeliveryLaneRegistry.from_payload(payload)
        except (TypeError, ValueError, yaml.YAMLError) as error:
            raise TrackerError("GitHub delivery lane registry is invalid") from error

    @staticmethod
    def _structured_marker_payload(
        body: str,
        *,
        prefix: str,
        suffix: str,
        record_kind: str,
    ) -> dict[str, Any]:
        marker_line = next(
            (
                line
                for line in body.splitlines()
                if line.startswith(prefix) and line.endswith(suffix)
            ),
            None,
        )
        if marker_line is None:
            raise TrackerError(f"GitHub {record_kind} comment has no structured marker")
        encoded = marker_line[len(prefix) : -len(suffix)]
        try:
            payload = json.loads(encoded)
        except ValueError as error:
            raise TrackerError(
                f"GitHub {record_kind} comment marker is invalid"
            ) from error
        if not isinstance(payload, dict):
            raise TrackerError(f"GitHub {record_kind} comment marker is invalid")
        return payload

    def _resource(self, query: str, url: str, **variables: str) -> dict:
        payload = self._graphql(query, url=url, **variables)
        try:
            resource = payload["data"]["resource"]
        except (KeyError, TypeError) as error:
            raise TrackerError("GitHub GraphQL response is incomplete") from error
        if not isinstance(resource, dict):
            raise TrackerError(f"GitHub resource was not found: {url}")
        return resource

    def _graphql(
        self,
        query: str,
        **variables: str | Sequence[str],
    ) -> dict:
        arguments = [
            self._executable,
            "api",
            "graphql",
            "-f",
            f"query={query}",
        ]
        for name, value in variables.items():
            if isinstance(value, str):
                arguments.extend(("-F", f"{name}={value}"))
            else:
                for item in value:
                    arguments.extend(("-F", f"{name}[]={item}"))
        output = self._runner.run(arguments)
        try:
            payload = json.loads(output)
        except (TypeError, ValueError) as error:
            raise TrackerError("GitHub CLI returned invalid JSON") from error
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            messages = "; ".join(
                str(item.get("message", item)) if isinstance(item, dict) else str(item)
                for item in errors
            )
            raise TrackerError(f"GitHub GraphQL request failed: {messages}")
        if not isinstance(payload, dict):
            raise TrackerError("GitHub GraphQL response is incomplete")
        return payload

    @staticmethod
    def _stage_or_invalid(
        *,
        issue_state: str,
        state_reason: str | None,
        labels: tuple[str, ...],
    ) -> str:
        try:
            return executive_stage(
                issue_state=issue_state,
                state_reason=state_reason,
                labels=labels,
            )
        except ValueError:
            return "invalid"

    @classmethod
    def _resource_stage(cls, resource: dict) -> str:
        label_nodes = resource.get("labels", {}).get("nodes", [])
        return cls._stage_or_invalid(
            issue_state=str(resource.get("state", "")).lower(),
            state_reason=(
                str(resource["stateReason"]).lower()
                if resource.get("stateReason") is not None
                else None
            ),
            labels=tuple(
                str(node.get("name", ""))
                for node in label_nodes
                if isinstance(node, dict)
            ),
        )

    @staticmethod
    def _parse_project_url(url: str) -> tuple[str, str, int]:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if (
            parsed.scheme != "https"
            or parsed.netloc.casefold() != "github.com"
            or parsed.query
            or parsed.fragment
            or len(parts) != 4
            or parts[0] not in {"orgs", "users"}
            or parts[2] != "projects"
            or not parts[1]
        ):
            raise TrackerError(f"Invalid GitHub Project URL: {url}")
        try:
            number = int(parts[3])
        except ValueError as error:
            raise TrackerError(f"Invalid GitHub Project URL: {url}") from error
        if number < 1:
            raise TrackerError(f"Invalid GitHub Project URL: {url}")
        owner_type = "organization" if parts[0] == "orgs" else "user"
        return owner_type, parts[1], number
