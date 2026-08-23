"""The single application interface shared by every plugin adapter."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Callable

from .stages import (
    ALLOWED_TRANSITIONS,
    available_transitions,
    executive_stage,
    rejection_reason,
)
from .storage import PluginStorage
from .tracker import (
    GitHubTrackerAdapter,
    TrackerAdapter,
    TrackerConflictError,
    TrackerIssue,
    TrackerProject,
)


class MapBindingError(ValueError):
    """Raised when a requested binding violates governance identity rules."""


class MapTransitionError(ValueError):
    """Raised when the governance policy rejects a requested stage change."""

    def __init__(self, *, current_stage: str, requested_stage: str, reason: str) -> None:
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


_TRANSITION_LOCKS: dict[str, RLock] = {}
_TRANSITION_LOCKS_GUARD = Lock()


def _transition_lock(map_id: str) -> RLock:
    with _TRANSITION_LOCKS_GUARD:
        return _TRANSITION_LOCKS.setdefault(map_id, RLock())


class MapGovernanceApplication:
    """Coordinate Map Governance operations behind one public seam."""

    def __init__(
        self,
        *,
        plugin_root: Path,
        storage_root: Path,
        tracker: TrackerAdapter | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._plugin_root = plugin_root.resolve()
        self._storage = PluginStorage(storage_root)
        self._tracker = tracker or GitHubTrackerAdapter()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

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
            "status": "ready" if all(path.is_file() for path in required_files) else "not_ready",
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
            card = self._card_projection(row, project_url=project["tracker"]["url"])
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
        return self._card_projection(card, project_url=project_binding["project_url"])

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
        return self.board()

    def transition_map(
        self,
        *,
        map_id: str,
        expected_stage: str,
        requested_stage: str,
    ) -> dict[str, Any]:
        """Commit a governed stage transition to the tracker, then project it."""
        with _transition_lock(map_id):
            return self._transition_map(
                map_id=map_id,
                expected_stage=expected_stage,
                requested_stage=requested_stage,
            )

    def _transition_map(
        self,
        *,
        map_id: str,
        expected_stage: str,
        requested_stage: str,
    ) -> dict[str, Any]:
        binding = self._storage.map_binding(map_id)
        if binding is None:
            raise MapBindingError(f"Map is not bound: {map_id}")
        project = self._storage.project_binding(binding["project_id"])
        if project is None:
            raise MapBindingError(f'CEO project is not configured: {binding["project_id"]}')

        current_issue = self._tracker.get_issue(binding["issue_url"])
        if current_issue.id != map_id:
            raise MapBindingError("Bound GitHub Issue identity changed")
        current_stage = self._executive_stage(current_issue)
        if current_stage != expected_stage:
            raise MapTransitionConflict(
                current_stage=current_stage,
                requested_stage=requested_stage,
                reason="tracker stage changed; refresh and retry",
            )
        if requested_stage not in ALLOWED_TRANSITIONS.get(current_stage, ()):
            raise MapTransitionError(
                current_stage=current_stage,
                requested_stage=requested_stage,
                reason=rejection_reason(current_stage, requested_stage),
            )

        try:
            committed_issue = self._tracker.transition_issue_stage(
                binding["issue_url"],
                expected_stage=current_stage,
                requested_stage=requested_stage,
            )
        except TrackerConflictError as error:
            raise MapTransitionConflict(
                current_stage=error.current_stage,
                requested_stage=error.requested_stage,
                reason="tracker stage changed; refresh and retry",
            ) from error
        if committed_issue.id != map_id:
            raise MapBindingError("Bound GitHub Issue identity changed during transition")
        try:
            committed_stage = self._executive_stage(committed_issue)
        except MapBindingError as error:
            raise MapTransitionConflict(
                current_stage="invalid",
                requested_stage=requested_stage,
                reason="tracker returned an invalid stage set; refresh and retry",
            ) from error
        if committed_stage != requested_stage:
            raise MapTransitionConflict(
                current_stage=committed_stage,
                requested_stage=requested_stage,
                reason="tracker stage changed; refresh and retry",
            )
        synchronized_at = self._synchronized_at()
        card = self._stored_card(
            committed_issue,
            project_id=binding["project_id"],
            synchronized_at=synchronized_at,
        )
        competing_stage = self._storage.compare_and_save_map_projection(
            card,
            expected_stage=expected_stage,
        )
        if competing_stage is not None:
            raise MapTransitionConflict(
                current_stage=competing_stage,
                requested_stage=requested_stage,
                reason=(
                    "local projection changed after tracker commit; refresh to reconcile"
                ),
            )
        return self._card_projection(card, project_url=project["project_url"])

    def _synchronized_at(self) -> str:
        value = self._clock()
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
                "identity": f'{row["repository"]}#{row["issue_number"]}',
                "url": row["issue_url"],
            },
            "title": row["title"],
            "stage": row["stage"],
            "available_transitions": list(available_transitions(row["stage"])),
            "ceo_session": {"state": row["ceo_session_state"]},
            "last_synchronized_at": row["synchronized_at"],
        }
