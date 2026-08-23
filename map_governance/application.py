"""The single application interface shared by every plugin adapter."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Callable, NoReturn

from .stages import (
    ALLOWED_TRANSITIONS,
    available_transitions,
    executive_stage,
    rejection_reason,
)
from .storage import PluginStorage
from .sessions import (
    CEOSessionRunner,
    CanonicalSession,
    canonical_session_identity,
    canonical_session_title,
)
from .tracker import (
    GitHubTrackerAdapter,
    TrackerAdapter,
    TrackerConflictError,
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
    ) -> None:
        self._plugin_root = plugin_root.resolve()
        self._storage = PluginStorage(storage_root)
        self._tracker = tracker or GitHubTrackerAdapter()
        self._session_runner = session_runner
        self._profile_name = profile_name
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
                card["approvals"] = {"count": 0, "items": []}
                card["delivery_summary"] = {"state": "not_reported"}
                return card
        raise MapBindingError(f"Map is not bound: {map_id}")

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
            "delivery_summary": detail["delivery_summary"],
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
        existing = self._decision_records_by_id(
            self._tracker.list_decisions(binding["issue_url"])
        ).get(decision.decision_id)
        idempotent = existing is not None
        if existing is None:
            self._tracker.append_decision(
                binding["issue_url"],
                issue_id=map_id,
                decision=decision,
            )
            confirmed = self._decision_records_by_id(
                self._tracker.list_decisions(binding["issue_url"])
            ).get(decision.decision_id)
            if confirmed is None:
                raise TrackerDecisionConfirmationError(
                    "Tracker did not confirm the structured decision in Issue history"
                )
        else:
            confirmed = existing
        if confirmed.decision != decision:
            raise StructuredDecisionConflict(
                decision_id=decision.decision_id,
            )
        projection = self._decision_projection(
            confirmed,
            confirmed_at=self._synchronized_at(),
        )
        self._storage.save_decision_projection(map_id=map_id, decision=projection)
        return {
            "map_id": map_id,
            "decision": projection,
            "idempotent": idempotent,
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
        session = self._session_runner.load_skill(
            session,
            content=skill_content,
            idempotency_key=f"{identity}:skill:{skill_hash}",
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

    def transition_map(
        self,
        *,
        map_id: str,
        expected_stage: str,
        requested_stage: str,
    ) -> dict[str, Any]:
        """Commit a governed stage transition to the tracker, then project it."""
        with _operation_lock("transition", map_id):
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
            raise MapBindingError(
                f"CEO project is not configured: {binding['project_id']}"
            )

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
            raise MapBindingError(
                "Bound GitHub Issue identity changed during transition"
            )
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
        return self._card_with_summary(
            card,
            project_url=project["project_url"],
        )

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
