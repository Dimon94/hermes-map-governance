"""Tracker boundary and GitHub implementation for Map Issues and Projects."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Protocol, Sequence
from urllib.parse import urlparse

from .stages import executive_stage


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


class CommandRunner(Protocol):
    def run(self, arguments: Sequence[str]) -> str: ...


class SubprocessCommandRunner:
    """Run one argv-safe command and return its standard output."""

    def run(self, arguments: Sequence[str]) -> str:
        try:
            completed = subprocess.run(
                list(arguments),
                capture_output=True,
                check=False,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TrackerError(f"GitHub CLI request failed: {error}") from error
        if completed.returncode != 0:
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
            raise TrackerError(f"GitHub Project response is incomplete: {url}") from error

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
            raise TrackerError("GitHub stage mutation response is incomplete") from error
        if not isinstance(mutated_issue, dict) or str(mutated_issue.get("id")) != issue_id:
            raise TrackerError("GitHub stage mutation did not return the requested Issue")

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
        output = self._runner.run(
            arguments
        )
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
