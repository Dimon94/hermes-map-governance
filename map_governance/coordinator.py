"""Plugin-owned Herdr coordinator runtime for one Hermes PM per Map."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .storage import PluginStorage


@dataclass(frozen=True)
class CommissioningContext:
    """Doctor-confirmed, secret-free coordinates needed to commission a PM."""

    project_id: str
    project_url: str
    repository: str
    repository_path: str
    pm_profile: str
    routing_policy: str
    herdr_executable: str
    skills: tuple[str, ...]
    ceo_profile: str | None = None
    pm_storage_root: str | None = None


@dataclass(frozen=True)
class OwnedPaneCoordinates:
    """Opaque pane coordinates that must be proven together before mutation."""

    workspace_id: str
    window_id: str
    pane_id: str

    def matches(self, pane: Mapping[str, Any], *, repository_path: str) -> bool:
        return (
            pane.get("workspace_id") == self.workspace_id
            and pane.get("tab_id") == self.window_id
            and pane.get("pane_id") == self.pane_id
            and pane.get("cwd") == repository_path
        )


@dataclass(frozen=True)
class RootRuntimeRequest:
    """Stable Map identity passed to the Herdr runtime boundary."""

    map_id: str
    map_url: str
    context: CommissioningContext


class CommissioningPrerequisiteResolver(Protocol):
    def commissioning_context(
        self, *, project_id: str, repository: str
    ) -> CommissioningContext: ...


class CoordinatorRuntimeBoundary(Protocol):
    def ensure_root(self, request: RootRuntimeRequest) -> dict[str, Any]: ...

    def prompt_ready(
        self, *, map_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    def confirm_ready(self, *, map_id: str, record_id: str) -> dict[str, Any]: ...

    def activate(self, *, map_id: str, record_id: str) -> dict[str, Any]: ...

    def record_failure(
        self,
        *,
        map_id: str,
        reason: str,
        retryable: bool,
        repair_required: bool = False,
    ) -> dict[str, Any]: ...

    def status(self, *, map_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CoordinatorCommandResult:
    """Only the command evidence the runtime is allowed to inspect or persist."""

    returncode: int | None
    stdout: str
    timed_out: bool = False


class CoordinatorCommandRunner(Protocol):
    def run(
        self, arguments: Sequence[str], *, timeout: float
    ) -> CoordinatorCommandResult: ...

    def start(self, arguments: Sequence[str]) -> None: ...


class SubprocessCoordinatorCommandRunner:
    """Execute fixed argv without a shell and discard terminal/error content."""

    def run(
        self, arguments: Sequence[str], *, timeout: float
    ) -> CoordinatorCommandResult:
        try:
            completed = subprocess.run(
                [str(item) for item in arguments],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return CoordinatorCommandResult(None, "", timed_out=True)
        except OSError:
            return CoordinatorCommandResult(127, "")
        return CoordinatorCommandResult(completed.returncode, completed.stdout)

    def start(self, arguments: Sequence[str]) -> None:
        try:
            subprocess.Popen(
                [str(item) for item in arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as error:
            raise CoordinatorRuntimeError(
                reason="session_start_failed",
                retryable=True,
            ) from error


class CoordinatorRuntimeError(RuntimeError):
    """Executive-safe runtime evidence without argv, paths, or terminal output."""

    def __init__(
        self,
        *,
        reason: str,
        retryable: bool,
        repair_required: bool = False,
    ) -> None:
        self.reason = reason
        self.retryable = retryable
        self.repair_required = repair_required
        super().__init__(f"Coordinator runtime failed: {reason}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "coordinator_runtime_error",
            "reason": self.reason,
            "retryable": self.retryable,
            "repair_required": self.repair_required,
            "resource_disposition": (
                "no_cleanup_without_verified_ownership"
                if self.repair_required
                else "retained_verified_owned_runtime_for_retry"
            ),
        }

    def evidence(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "retryable": self.retryable,
            "repair_required": self.repair_required,
            "resource_disposition": (
                "no_cleanup_without_verified_ownership"
                if self.repair_required
                else "retained_verified_owned_runtime_for_retry"
            ),
        }


class CommissioningPrerequisiteError(PermissionError):
    """Fail-closed prerequisite evidence for an explicit commission request."""

    def __init__(self, *, reason: str, failed_checks: Sequence[str] = ()) -> None:
        self.reason = reason
        self.failed_checks = tuple(str(item) for item in failed_checks)
        super().__init__(f"Commissioning prerequisites are not ready: {reason}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "commissioning_prerequisites_not_ready",
            "reason": self.reason,
            "failed_checks": list(self.failed_checks),
            "retryable": True,
        }


class CommissioningAuthorizationError(PermissionError):
    """The Map lacks tracker-confirmed authority to start delivery."""

    def __init__(self, *, map_id: str, reason: str) -> None:
        self.map_id = map_id
        self.reason = reason
        super().__init__(f"Map commission denied: {reason}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "commissioning_authorization_denied",
            "map_id": self.map_id,
            "reason": self.reason,
            "retryable": False,
        }


_SENSITIVE_VALUE_RE = re.compile(
    r"(?:github_pat_[A-Za-z0-9_]{6,}|gh[pousr]_[A-Za-z0-9_]{6,}"
    r"|sk-[A-Za-z0-9_-]{8,}|bearer\s+[A-Za-z0-9._-]{8,}"
    r"|(?:token|secret|password|api[_ -]?key)\s*[:=]\s*\S{4,})",
    re.IGNORECASE,
)


class CoordinatorRuntime:
    """Discover, create, and resume one verified plugin-owned PM root runtime."""

    def __init__(
        self,
        *,
        storage: PluginStorage,
        runner: CoordinatorCommandRunner | None = None,
        clock: Callable[[], str],
        command_timeout: float = 35.0,
        startup_attempts: int = 20,
        startup_poll_seconds: float = 0.1,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._storage = storage
        self._runner = runner or SubprocessCoordinatorCommandRunner()
        self._clock = clock
        self._command_timeout = command_timeout
        self._startup_attempts = startup_attempts
        self._startup_poll_seconds = startup_poll_seconds
        self._sleeper = sleeper

    @staticmethod
    def _digest(value: str, length: int) -> str:
        return hashlib.sha256(value.encode()).hexdigest()[:length]

    @classmethod
    def session_namespace(cls, lifecycle_id: str) -> str:
        return f"mapgov-{cls._digest(lifecycle_id, 20)}"

    @classmethod
    def session_ownership_marker(cls, lifecycle_id: str) -> str:
        return f"map-governance:session:{cls._digest(lifecycle_id, 32)}"

    @classmethod
    def ownership_marker(cls, lifecycle_id: str, map_id: str) -> str:
        return f"map-governance:map:{cls._digest(lifecycle_id + ':' + map_id, 32)}"

    @classmethod
    def workspace_label(cls, lifecycle_id: str, map_id: str) -> str:
        return f"mapgov-map-{cls._digest(lifecycle_id + ':' + map_id, 20)}"

    @classmethod
    def agent_name(cls, lifecycle_id: str, map_id: str) -> str:
        return f"mapgov_pm_{cls._digest(lifecycle_id + ':' + map_id, 16)}"

    def ensure_root(self, request: RootRuntimeRequest) -> dict[str, Any]:
        """Create or resume the exact owned workspace and root Hermes PM."""
        self._validate_request(request)
        with self._storage.pm_runtime_lease(request.map_id):
            with self._storage.coordinator_session_lease():
                return self._ensure_root(request)

    def _ensure_root(self, request: RootRuntimeRequest) -> dict[str, Any]:
        now = self._clock()
        lifecycle = self._storage.coordinator_lifecycle(created_at=now)
        namespace = self.session_namespace(lifecycle)
        session_marker = self.session_ownership_marker(lifecycle)
        marker = self.ownership_marker(lifecycle, request.map_id)
        label = self.workspace_label(lifecycle, request.map_id)
        agent_name = self.agent_name(lifecycle, request.map_id)
        existing = self._storage.pm_runtime(request.map_id)

        sessions = self._sessions(request.context.herdr_executable)
        session_present = namespace in sessions
        session_proof = self._storage.coordinator_session(namespace)
        if session_present and session_proof is None:
            raise CoordinatorRuntimeError(
                reason="session_namespace_collision",
                retryable=False,
                repair_required=True,
            )
        if session_proof is not None and (
            session_proof["lifecycle_id"] != lifecycle
            or session_proof["ownership_marker"] != session_marker
        ):
            raise CoordinatorRuntimeError(
                reason="session_ownership_mismatch",
                retryable=False,
                repair_required=True,
            )
        try:
            self._storage.reserve_pm_runtime(
                map_id=request.map_id,
                session_namespace=namespace,
                workspace_label=label,
                agent_id=agent_name,
                ownership_marker=marker,
                lifecycle_id=lifecycle,
                context=request.context,
                updated_at=now,
                session_ownership_marker=session_marker,
            )
        except ValueError as error:
            raise CoordinatorRuntimeError(
                reason="runtime_identity_conflict",
                retryable=False,
                repair_required=True,
            ) from error
        try:
            if not session_present:
                self._runner.start(
                    [
                        request.context.herdr_executable,
                        "--session",
                        namespace,
                        "server",
                    ]
                )
                confirmed = False
                for attempt in range(self._startup_attempts):
                    if namespace in self._sessions(request.context.herdr_executable):
                        confirmed = True
                        break
                    if attempt + 1 < self._startup_attempts:
                        self._sleeper(self._startup_poll_seconds)
                if not confirmed:
                    raise CoordinatorRuntimeError(
                        reason="session_start_unconfirmed",
                        retryable=True,
                    )

            coordinates = self._ensure_workspace(
                request=request,
                namespace=namespace,
                label=label,
                marker=marker,
                existing=existing,
            )
            self._storage.update_pm_runtime(
                map_id=request.map_id,
                state="workspace_ready",
                workspace_id=coordinates.workspace_id,
                window_id=coordinates.window_id,
                pane_id=coordinates.pane_id,
                failure=None,
                updated_at=self._clock(),
            )
            agent_session_id = self._ensure_agent(
                request=request,
                namespace=namespace,
                coordinates=coordinates,
                agent_name=agent_name,
            )
            restored_state = (
                str(existing["state"])
                if existing is not None
                and existing.get("state") in {"ready_confirmed", "active"}
                else "pm_ready"
            )
            self._storage.update_pm_runtime(
                map_id=request.map_id,
                state=restored_state,
                agent_session_id=agent_session_id,
                failure=None,
                updated_at=self._clock(),
            )
        except CoordinatorRuntimeError as error:
            self._record_failure(request.map_id, error)
            raise
        record = self._storage.pm_runtime(request.map_id)
        if record is None:  # pragma: no cover - guarded by durable reservation
            raise RuntimeError("PM runtime registry record disappeared")
        return record

    def prompt_ready(
        self,
        *,
        map_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Prompt the verified PM root with one structured commissioning packet."""
        self._validate_payload(payload)
        with self._storage.pm_runtime_lease(map_id):
            record = self._storage.pm_runtime(map_id)
            if record is None:
                raise CoordinatorRuntimeError(
                    reason="runtime_not_reserved",
                    retryable=False,
                    repair_required=True,
                )
            self._validate_owned_record(record)
            try:
                agent = self._validate_live_runtime(record)
                result = self._command_json(
                    [
                        record["herdr_executable"]
                        if "herdr_executable" in record
                        else str(payload.get("herdr_executable") or "herdr"),
                        "--session",
                        record["session_namespace"],
                        "agent",
                        "prompt",
                        record["agent_id"],
                        json.dumps(
                            dict(payload),
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        "--wait",
                        "--timeout",
                        "30000",
                    ]
                )
                prompted = self._agent_from(result)
                self._assert_agent_coordinates(record, prompted)
                self._assert_agent_coordinates(record, agent)
            except CoordinatorRuntimeError as error:
                self._storage.update_pm_runtime(
                    map_id=map_id,
                    state=(
                        "repair_required" if error.repair_required else "awaiting_ready"
                    ),
                    failure=error.evidence(),
                    updated_at=self._clock(),
                )
                status = self._storage.pm_runtime(map_id)
                if status is None:  # pragma: no cover
                    raise RuntimeError("PM runtime registry record disappeared")
                return status
            self._storage.update_pm_runtime(
                map_id=map_id,
                state="awaiting_ready",
                failure=None,
                updated_at=self._clock(),
            )
            status = self._storage.pm_runtime(map_id)
            if status is None:  # pragma: no cover
                raise RuntimeError("PM runtime registry record disappeared")
            return status

    def confirm_ready(self, *, map_id: str, record_id: str) -> dict[str, Any]:
        return self._advance(
            map_id=map_id, state="ready_confirmed", record_id=record_id
        )

    def activate(self, *, map_id: str, record_id: str) -> dict[str, Any]:
        return self._advance(map_id=map_id, state="active", record_id=record_id)

    def record_failure(
        self,
        *,
        map_id: str,
        reason: str,
        retryable: bool,
        repair_required: bool = False,
    ) -> dict[str, Any]:
        error = CoordinatorRuntimeError(
            reason=reason,
            retryable=retryable,
            repair_required=repair_required,
        )
        self._record_failure(map_id, error)
        status = self._storage.pm_runtime(map_id)
        if status is None:  # pragma: no cover
            raise RuntimeError("PM runtime registry record disappeared")
        return status

    def status(self, *, map_id: str) -> dict[str, Any]:
        record = self._storage.pm_runtime(map_id)
        return record or {"map_id": map_id, "state": "not_commissioned"}

    def _advance(
        self,
        *,
        map_id: str,
        state: str,
        record_id: str,
    ) -> dict[str, Any]:
        self._storage.update_pm_runtime(
            map_id=map_id,
            state=state,
            ready_record_id=record_id,
            failure=None,
            updated_at=self._clock(),
        )
        status = self._storage.pm_runtime(map_id)
        if status is None:  # pragma: no cover
            raise RuntimeError("PM runtime registry record disappeared")
        return status

    def _ensure_workspace(
        self,
        *,
        request: RootRuntimeRequest,
        namespace: str,
        label: str,
        marker: str,
        existing: Mapping[str, Any] | None,
    ) -> OwnedPaneCoordinates:
        prefix = [request.context.herdr_executable, "--session", namespace]
        payload = self._command_json([*prefix, "workspace", "list"])
        result = self._result(payload)
        workspaces = result.get("workspaces")
        if not isinstance(workspaces, list):
            raise self._malformed("partial_coordinates")
        matching = [
            item
            for item in workspaces
            if isinstance(item, Mapping) and item.get("label") == label
        ]
        if len(matching) > 1:
            raise CoordinatorRuntimeError(
                reason="workspace_marker_collision",
                retryable=False,
                repair_required=True,
            )
        if existing is not None and existing.get("workspace_id"):
            existing_id = existing["workspace_id"]
            if len(matching) != 1 or matching[0].get("workspace_id") != existing_id:
                raise CoordinatorRuntimeError(
                    reason="workspace_ownership_mismatch",
                    retryable=False,
                    repair_required=True,
                )
            info = self._command_json([*prefix, "workspace", "get", existing_id])
            workspace = self._workspace_from(info)
            if (
                workspace.get("workspace_id") != existing_id
                or workspace.get("label") != label
                or workspace.get("active_tab_id") != existing.get("window_id")
            ):
                raise CoordinatorRuntimeError(
                    reason="workspace_ownership_mismatch",
                    retryable=False,
                    repair_required=True,
                )
            window_id = str(existing.get("window_id") or "")
            pane_id = str(existing.get("pane_id") or "")
            if not window_id or not pane_id:
                raise self._malformed("partial_coordinates")
            coordinates = OwnedPaneCoordinates(str(existing_id), window_id, pane_id)
            self._validate_pane(
                prefix=prefix,
                coordinates=coordinates,
                repository_path=request.context.repository_path,
            )
            return coordinates
        if matching:
            if existing is None or existing.get("ownership_marker") != marker:
                raise CoordinatorRuntimeError(
                    reason="workspace_marker_collision",
                    retryable=False,
                    repair_required=True,
                )
            workspace_id = self._opaque(matching[0].get("workspace_id"))
            info = self._workspace_from(
                self._command_json([*prefix, "workspace", "get", workspace_id])
            )
            window_id = self._opaque(info.get("active_tab_id"))
            pane_result = self._result(
                self._command_json(
                    [*prefix, "pane", "list", "--workspace", workspace_id]
                )
            )
            panes = pane_result.get("panes")
            if not isinstance(panes, list) or len(panes) != 1:
                raise CoordinatorRuntimeError(
                    reason="workspace_recovery_ambiguous",
                    retryable=False,
                    repair_required=True,
                )
            if not isinstance(panes[0], Mapping):
                raise self._workspace_recovery_error()
            coordinates = OwnedPaneCoordinates(
                workspace_id,
                window_id,
                self._opaque(panes[0].get("pane_id")),
            )
            if not coordinates.matches(
                panes[0], repository_path=request.context.repository_path
            ):
                raise self._workspace_recovery_error()
            return coordinates
        created = self._command_json(
            [
                *prefix,
                "workspace",
                "create",
                "--cwd",
                request.context.repository_path,
                "--label",
                label,
                "--env",
                f"MAP_GOVERNANCE_OWNERSHIP={marker}",
                "--no-focus",
            ]
        )
        result = self._result(created)
        if result.get("type") != "workspace_created":
            raise self._malformed("partial_coordinates")
        workspace = result.get("workspace")
        tab = result.get("tab")
        pane = result.get("root_pane")
        if not all(isinstance(item, Mapping) for item in (workspace, tab, pane)):
            raise self._malformed("partial_coordinates")
        assert isinstance(workspace, Mapping)
        assert isinstance(tab, Mapping)
        assert isinstance(pane, Mapping)
        if workspace.get("label") not in {None, label}:
            raise CoordinatorRuntimeError(
                reason="workspace_ownership_mismatch",
                retryable=False,
                repair_required=True,
            )
        coordinates = OwnedPaneCoordinates(
            self._opaque(workspace.get("workspace_id")),
            self._opaque(tab.get("tab_id")),
            self._opaque(pane.get("pane_id")),
        )
        self._validate_pane(
            prefix=prefix,
            coordinates=coordinates,
            repository_path=request.context.repository_path,
        )
        return coordinates

    def _validate_pane(
        self,
        *,
        prefix: Sequence[str],
        coordinates: OwnedPaneCoordinates,
        repository_path: str,
    ) -> None:
        result = self._result(
            self._command_json(
                [*prefix, "pane", "list", "--workspace", coordinates.workspace_id]
            )
        )
        panes = result.get("panes")
        matching = (
            [
                pane
                for pane in panes
                if isinstance(pane, Mapping)
                and pane.get("pane_id") == coordinates.pane_id
            ]
            if isinstance(panes, list)
            else []
        )
        if len(matching) != 1 or not coordinates.matches(
            matching[0], repository_path=repository_path
        ):
            raise CoordinatorRuntimeError(
                reason="pane_ownership_mismatch",
                retryable=False,
                repair_required=True,
            )

    @staticmethod
    def _workspace_recovery_error() -> CoordinatorRuntimeError:
        return CoordinatorRuntimeError(
            reason="workspace_recovery_ambiguous",
            retryable=False,
            repair_required=True,
        )

    def _ensure_agent(
        self,
        *,
        request: RootRuntimeRequest,
        namespace: str,
        coordinates: OwnedPaneCoordinates,
        agent_name: str,
    ) -> str:
        prefix = [request.context.herdr_executable, "--session", namespace]
        agent = self._get_agent([*prefix, "agent", "get", agent_name])
        if agent is None:
            payload = self._command_json(
                [
                    *prefix,
                    "agent",
                    "start",
                    agent_name,
                    "--kind",
                    "hermes",
                    "--pane",
                    coordinates.pane_id,
                    "--timeout",
                    "30000",
                    "--",
                    "--profile",
                    request.context.pm_profile,
                    "--tui",
                    "--skills",
                    ",".join(request.context.skills),
                ]
            )
            agent = self._agent_from(payload)
        expected = {
            "workspace_id": coordinates.workspace_id,
            "tab_id": coordinates.window_id,
            "pane_id": coordinates.pane_id,
            "name": agent_name,
            "agent": "hermes",
        }
        if any(agent.get(key) != value for key, value in expected.items()):
            raise CoordinatorRuntimeError(
                reason="opaque_coordinate_mismatch",
                retryable=False,
                repair_required=True,
            )
        session = agent.get("agent_session")
        if (
            not isinstance(session, Mapping)
            or session.get("agent") != "hermes"
            or session.get("kind") != "id"
        ):
            refreshed = self._get_agent([*prefix, "agent", "get", agent_name])
            if refreshed is None:
                raise self._malformed("agent_session_missing")
            if any(refreshed.get(key) != value for key, value in expected.items()):
                raise CoordinatorRuntimeError(
                    reason="opaque_coordinate_mismatch",
                    retryable=False,
                    repair_required=True,
                )
            agent = refreshed
            session = agent.get("agent_session")
            if (
                not isinstance(session, Mapping)
                or session.get("agent") != "hermes"
                or session.get("kind") != "id"
            ):
                if isinstance(session, Mapping) and session.get("kind") == "path":
                    raise CoordinatorRuntimeError(
                        reason="agent_session_identity_unsafe",
                        retryable=False,
                        repair_required=True,
                    )
                raise self._malformed("agent_session_missing")
        return self._opaque(session.get("value"))

    def _validate_live_runtime(self, record: Mapping[str, Any]) -> Mapping[str, Any]:
        executable = str(record.get("herdr_executable") or "")
        if not executable:
            executable = str(record.get("_herdr_executable") or "herdr")
        namespace = str(record["session_namespace"])
        if namespace not in self._sessions(executable):
            raise CoordinatorRuntimeError(
                reason="owned_session_unavailable",
                retryable=True,
            )
        prefix = [executable, "--session", namespace]
        result = self._result(self._command_json([*prefix, "workspace", "list"]))
        workspaces = result.get("workspaces")
        if not isinstance(workspaces, list) or not any(
            isinstance(item, Mapping)
            and item.get("workspace_id") == record["workspace_id"]
            and item.get("label") == record["workspace_label"]
            for item in workspaces
        ):
            raise CoordinatorRuntimeError(
                reason="workspace_ownership_mismatch",
                retryable=False,
                repair_required=True,
            )
        workspace = self._workspace_from(
            self._command_json(
                [*prefix, "workspace", "get", str(record["workspace_id"])]
            )
        )
        if (
            workspace.get("workspace_id") != record["workspace_id"]
            or workspace.get("label") != record["workspace_label"]
            or workspace.get("active_tab_id") != record["window_id"]
        ):
            raise CoordinatorRuntimeError(
                reason="workspace_ownership_mismatch",
                retryable=False,
                repair_required=True,
            )
        self._validate_pane(
            prefix=prefix,
            coordinates=OwnedPaneCoordinates(
                str(record["workspace_id"]),
                str(record["window_id"]),
                str(record["pane_id"]),
            ),
            repository_path=str(record["repository_path"]),
        )
        agent = self._get_agent([*prefix, "agent", "get", str(record["agent_id"])])
        if agent is None:
            raise CoordinatorRuntimeError(
                reason="owned_agent_unavailable",
                retryable=True,
            )
        self._assert_agent_coordinates(record, agent)
        return agent

    def _assert_agent_coordinates(
        self, record: Mapping[str, Any], agent: Mapping[str, Any]
    ) -> None:
        expected = {
            "workspace_id": record["workspace_id"],
            "tab_id": record["window_id"],
            "pane_id": record["pane_id"],
            "name": record["agent_id"],
            "agent": "hermes",
        }
        if any(agent.get(key) != value for key, value in expected.items()):
            raise CoordinatorRuntimeError(
                reason="opaque_coordinate_mismatch",
                retryable=False,
                repair_required=True,
            )
        session = agent.get("agent_session")
        if (
            not isinstance(session, Mapping)
            or session.get("agent") != "hermes"
            or session.get("kind") != "id"
            or session.get("value") != record.get("agent_session_id")
        ):
            raise CoordinatorRuntimeError(
                reason="agent_session_mismatch",
                retryable=False,
                repair_required=True,
            )

    def _sessions(self, executable: str) -> set[str]:
        payload = self._command_json([executable, "session", "list", "--json"])
        sessions = payload.get("sessions")
        if not isinstance(sessions, list):
            raise self._malformed("partial_coordinates")
        names: set[str] = set()
        for item in sessions:
            if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
                raise self._malformed("partial_coordinates")
            names.add(str(item["name"]))
        return names

    def _get_agent(self, arguments: Sequence[str]) -> Mapping[str, Any] | None:
        result = self._runner.run(arguments, timeout=self._command_timeout)
        if result.timed_out:
            raise CoordinatorRuntimeError(reason="command_timeout", retryable=True)
        if result.returncode == 1:
            return None
        return self._agent_from(self._decode_result(result))

    def _command_json(self, arguments: Sequence[str]) -> dict[str, Any]:
        return self._decode_result(
            self._runner.run(arguments, timeout=self._command_timeout)
        )

    def _decode_result(self, result: CoordinatorCommandResult) -> dict[str, Any]:
        if result.timed_out:
            raise CoordinatorRuntimeError(reason="command_timeout", retryable=True)
        if result.returncode != 0:
            raise CoordinatorRuntimeError(reason="command_failed", retryable=True)
        try:
            payload = json.loads(result.stdout)
        except (TypeError, ValueError) as error:
            raise CoordinatorRuntimeError(
                reason="malformed_json", retryable=True
            ) from error
        if not isinstance(payload, dict):
            raise self._malformed("malformed_json")
        return payload

    @staticmethod
    def _result(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise CoordinatorRuntime._malformed("partial_coordinates")
        return result

    @classmethod
    def _agent_from(cls, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        agent = cls._result(payload).get("agent")
        if not isinstance(agent, Mapping):
            raise cls._malformed("partial_coordinates")
        return agent

    @classmethod
    def _workspace_from(cls, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        workspace = cls._result(payload).get("workspace")
        if not isinstance(workspace, Mapping):
            raise cls._malformed("partial_coordinates")
        return workspace

    @staticmethod
    def _opaque(value: Any) -> str:
        if not isinstance(value, str) or not value or len(value) > 512:
            raise CoordinatorRuntime._malformed("partial_coordinates")
        return value

    @staticmethod
    def _malformed(reason: str) -> CoordinatorRuntimeError:
        return CoordinatorRuntimeError(reason=reason, retryable=True)

    @staticmethod
    def _validate_request(request: RootRuntimeRequest) -> None:
        values = (
            request.map_id,
            request.map_url,
            request.context.project_id,
            request.context.project_url,
            request.context.repository,
            request.context.repository_path,
            request.context.pm_profile,
            request.context.routing_policy,
            request.context.herdr_executable,
        )
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("Commissioning request has an empty identity")
        path = Path(request.context.repository_path)
        if not path.is_absolute() or not path.is_dir():
            raise ValueError(
                "Primary repository coordinate must be an absolute directory"
            )
        if request.context.skills != (
            "map-governance:pm",
            "delivery-pipeline",
            "herdr",
        ):
            raise ValueError("Commissioning request has an unsafe PM Skill set")

    @staticmethod
    def _validate_payload(payload: Mapping[str, Any]) -> None:
        serialized = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
        if CoordinatorRuntime._contains_sensitive(payload):
            raise ValueError("Commissioning payload contains sensitive material")
        if len(serialized.encode("utf-8")) > 32_768:
            raise ValueError("Commissioning payload is too large")

    @staticmethod
    def _contains_sensitive(value: Any) -> bool:
        if isinstance(value, Mapping):
            for key, item in value.items():
                normalized = re.sub(r"[^a-z0-9]+", "", str(key).lower())
                if any(
                    marker in normalized
                    for marker in (
                        "token",
                        "secret",
                        "password",
                        "apikey",
                        "privatekey",
                        "credential",
                    )
                ):
                    return True
                if CoordinatorRuntime._contains_sensitive(item):
                    return True
            return False
        if isinstance(value, (list, tuple)):
            return any(CoordinatorRuntime._contains_sensitive(item) for item in value)
        return isinstance(value, str) and _SENSITIVE_VALUE_RE.search(value) is not None

    def _validate_owned_record(self, record: Mapping[str, Any]) -> None:
        required = (
            "map_id",
            "session_namespace",
            "workspace_label",
            "workspace_id",
            "window_id",
            "pane_id",
            "agent_id",
            "agent_session_id",
            "ownership_marker",
            "lifecycle_id",
        )
        if any(not record.get(name) for name in required):
            raise CoordinatorRuntimeError(
                reason="partial_coordinates",
                retryable=False,
                repair_required=True,
            )
        lifecycle_id = str(record["lifecycle_id"])
        map_id = str(record["map_id"])
        namespace = str(record["session_namespace"])
        session = self._storage.coordinator_session(namespace)
        if (
            namespace != self.session_namespace(lifecycle_id)
            or record["workspace_label"] != self.workspace_label(lifecycle_id, map_id)
            or record["agent_id"] != self.agent_name(lifecycle_id, map_id)
            or record["ownership_marker"] != self.ownership_marker(lifecycle_id, map_id)
            or session is None
            or session["lifecycle_id"] != lifecycle_id
            or session["ownership_marker"]
            != self.session_ownership_marker(lifecycle_id)
        ):
            raise CoordinatorRuntimeError(
                reason="runtime_ownership_mismatch",
                retryable=False,
                repair_required=True,
            )

    def _record_failure(self, map_id: str, error: CoordinatorRuntimeError) -> None:
        if self._storage.pm_runtime(map_id) is None:
            return
        current = self._storage.pm_runtime(map_id)
        state = (
            "repair_required"
            if error.repair_required
            else str(current["state"] if current is not None else "reserved")
        )
        self._storage.update_pm_runtime(
            map_id=map_id,
            state=state,
            failure=error.evidence(),
            updated_at=self._clock(),
        )
