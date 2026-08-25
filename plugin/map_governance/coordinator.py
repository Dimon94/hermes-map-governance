"""Plugin-owned Herdr coordinator runtime for one Hermes PM per Map."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn, Protocol, Sequence

from .storage import PluginStorage


DELIVERY_TRANSPORT_RECOVERY_LIMITATION = (
    "The terminal Herdr transport cache was unavailable; tracker registry and Git "
    "evidence were used."
)
DELIVERY_BOOTSTRAP_AUTHORITY = "none"


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
    supported_worker_kinds: tuple[str, ...] = ()
    implement_skill_path: str | None = None
    routing_default_worker: str = "codex"
    routing_attribute_workers: tuple[tuple[str, str], ...] = ()
    routing_unavailable_behavior: str = "blocked"


@dataclass(frozen=True)
class DeliveryLaneSpec:
    """One delivery-pipeline-owned implementation lane handed to Herdr."""

    protocol: str
    lane_id: str
    ticket_id: str
    ticket_title: str
    ticket_url: str
    parent_spec_url: str
    integration_worktree: str
    integration_branch: str
    execution_worktree: str
    execution_branch: str
    base_commit: str
    owner_skill_name: str
    owner_skill_path: str
    owner_invocation_label: str
    worker_kind: str
    validation_argv: tuple[str, ...]
    completion_contract: str
    known_limitations: tuple[str, ...] = ()
    integration_order: int = 1
    integration_total: int = 1
    integration_predecessor_ticket_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeliveryLaneRegistry:
    """Tracker-authoritative delivery-pipeline lane registry readback."""

    work_item: str
    role: str
    lane_id: str
    runtime: str
    state: str
    workspace_id: str
    tab_id: str
    pane_id: str
    herdr_session_name: str
    herdr_session_owned: bool
    bootstrap_authority: str
    agent_permission_mode: str
    worktree: str
    branch: str
    base_commit: str
    head_commit: str | None
    integrated_commit: str | None
    updated_at: str
    dispatch_id: str | None = None
    evidence_source: str | None = None
    final_report_digest: str | None = None
    blocker_summary: str | None = None

    def payload(self) -> dict[str, Any]:
        payload = {
            "work_item": self.work_item,
            "role": self.role,
            "lane_id": self.lane_id,
            "runtime": self.runtime,
            "state": self.state,
            "workspace_id": self.workspace_id,
            "tab_id": self.tab_id,
            "pane_id": self.pane_id,
            "herdr_session_name": self.herdr_session_name,
            "herdr_session_owned": self.herdr_session_owned,
            "bootstrap_authority": self.bootstrap_authority,
            "agent_permission_mode": self.agent_permission_mode,
            "worktree": self.worktree,
            "branch": self.branch,
            "base_commit": self.base_commit,
            "head_commit": self.head_commit,
            "integrated_commit": self.integrated_commit,
            "updated_at": self.updated_at,
            "evidence_source": self.evidence_source,
            "final_report_digest": self.final_report_digest,
            "blocker_summary": self.blocker_summary,
        }
        if self.dispatch_id is not None:
            payload["dispatch_id"] = self.dispatch_id
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DeliveryLaneRegistry":
        return cls(**dict(payload))


@dataclass(frozen=True)
class DeliveryRuntimeRequest:
    """Request-scoped Map authority plus one declared delivery lane."""

    map_id: str
    map_url: str
    context: CommissioningContext
    lane: DeliveryLaneSpec
    registry_timestamp: str = "1970-01-01T00:00:00Z"
    registry: DeliveryLaneRegistry | None = None
    integration_expected_head: str | None = None
    integration_predecessor_commits: tuple[str, ...] = ()


def delivery_lane_packet(request: DeliveryRuntimeRequest) -> dict[str, Any]:
    """Build the byte-stable worker packet owned by delivery-pipeline."""
    lane = request.lane
    return {
        "protocol": lane.protocol,
        "map": {"id": request.map_id, "url": request.map_url},
        "ticket": {
            "id": lane.ticket_id,
            "title": lane.ticket_title,
            "url": lane.ticket_url,
            "parent_spec_url": lane.parent_spec_url,
        },
        "lane": {"id": lane.lane_id, "runtime": f"herdr-{lane.worker_kind}-pane"},
        "owner": {
            "name": lane.owner_skill_name,
            "skill_path": lane.owner_skill_path,
            "invocation_label": lane.owner_invocation_label,
        },
        "integration": {
            "worktree": lane.integration_worktree,
            "branch": lane.integration_branch,
            "base_commit": lane.base_commit,
            "order": lane.integration_order,
            "total": lane.integration_total,
            "predecessor_ticket_urls": list(lane.integration_predecessor_ticket_urls),
        },
        "execution": {
            "working_directory": lane.execution_worktree,
            "branch": lane.execution_branch,
        },
        "validation": {"argv": list(lane.validation_argv)},
        "known_limitations": list(lane.known_limitations),
        "completion": {
            "contract": lane.completion_contract,
            "required_evidence": [
                "one_local_commit",
                "clean_execution_worktree",
                "focused_checks",
                "review",
            ],
            "integration_owner": "hermes-pm",
            "remote_actions": "forbidden",
        },
        "constraints": [
            "完整读取已声明的 implement owner Skill 后再执行。",
            "只处理这一张 implementation ticket，不领取 sibling ticket。",
            "只在声明的 Execution Worktree 中写入并创建一个本地 commit。",
            "保留 tracker、cherry-pick、integration、push、PR、merge、release 与 Issue closure 给协调者。",
            "最终输出完整的 FINAL_REPORT_BEGIN 与 FINAL_REPORT_END marker。",
            "完成时 Checks 与 Review 字段必须分别使用精确值 passed。",
        ],
    }


def delivery_worker_prompt(
    request: DeliveryRuntimeRequest,
    registry: DeliveryLaneRegistry,
) -> dict[str, Any]:
    """Augment stable lane identity with verified Herdr/runtime report context."""
    lane = request.lane
    packet = (
        _legacy_delivery_lane_packet(request)
        if registry.dispatch_id is None
        else delivery_lane_packet(request)
    )
    return {
        **packet,
        "runtime_context": {
            "repository": {
                "coordinate": request.context.repository,
                "root": request.context.repository_path,
            },
            "herdr": {
                "session": registry.herdr_session_name,
                "workspace_id": registry.workspace_id,
                "tab_id": registry.tab_id,
                "pane_id": registry.pane_id,
            },
        },
        "final_report_contract": {
            "begin_marker": "FINAL_REPORT_BEGIN",
            "end_marker": "FINAL_REPORT_END",
            "required_field_order": [
                "Ticket",
                "状态",
                "Pane/worktree/branch",
                "Commit",
                "Checks",
                "Review",
                "Dirty state",
                "Touched files",
                "Blocker",
            ],
            "completed_values": {
                "Ticket": f"{lane.ticket_id} {lane.ticket_title} {lane.ticket_url}",
                "状态": "completed",
                "Pane/worktree/branch": (
                    f"{registry.pane_id} {lane.execution_worktree} "
                    f"{lane.execution_branch}"
                ),
                "Commit": "<exact local HEAD SHA and subject>",
                "Checks": "passed",
                "Review": "passed",
                "Dirty state": "clean",
                "Touched files": "<ticket-owned paths>",
                "Blocker": "none",
            },
            "blocked_values": {
                "状态": "blocked",
                "Commit": "none",
                "Checks": "blocked",
                "Review": "blocked",
                "Dirty state": "clean",
                "Touched files": "none",
                "Blocker": "<concise exact blocker>",
            },
        },
    }


def delivery_dispatch_id(request: DeliveryRuntimeRequest) -> str:
    """Return the stable identity that binds dispatch and collection payloads."""
    serialized = json.dumps(
        delivery_lane_packet(request), ensure_ascii=False, sort_keys=True
    )
    return "delivery-dispatch:" + hashlib.sha256(serialized.encode()).hexdigest()[:32]


def _legacy_delivery_lane_packet(request: DeliveryRuntimeRequest) -> dict[str, Any]:
    """Rebuild the #14 packet only when tracker truth proves a legacy lane."""
    packet = delivery_lane_packet(request)
    integration = dict(packet["integration"])
    for field in ("order", "total", "predecessor_ticket_urls"):
        integration.pop(field)
    return {**packet, "integration": integration}


def delivery_confirmed_dispatch_id(request: DeliveryRuntimeRequest) -> str:
    """Use tracker-bound identity, including the exact pre-#17 packet shape."""
    registry = request.registry
    if registry is None or registry.dispatch_id is not None:
        return delivery_dispatch_id(request)
    serialized = json.dumps(
        _legacy_delivery_lane_packet(request), ensure_ascii=False, sort_keys=True
    )
    return "delivery-dispatch:" + hashlib.sha256(serialized.encode()).hexdigest()[:32]


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

    def prepare_lane(self, request: DeliveryRuntimeRequest) -> dict[str, Any]: ...

    def dispatch_lane(self, request: DeliveryRuntimeRequest) -> dict[str, Any]: ...

    def collect_lane(self, request: DeliveryRuntimeRequest) -> dict[str, Any]: ...

    def readback(self, *, map_id: str, turn_id: str) -> Mapping[str, Any] | None: ...

    def resume(
        self,
        *,
        map_id: str,
        profile_name: str,
        session_id: str,
        coordinator_id: str,
        turn_id: str,
        content: str = "",
    ) -> None: ...


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


def validate_delivery_lane_registry(
    *,
    lane: DeliveryLaneSpec,
    registry: DeliveryLaneRegistry,
    allowed_states: set[str],
) -> None:
    """Validate one tracker/runtime registry against the public lane contract."""
    expected = {
        "work_item": lane.ticket_url,
        "role": "implementation",
        "lane_id": lane.lane_id,
        "runtime": f"herdr-{lane.worker_kind}-pane",
        "worktree": lane.execution_worktree,
        "branch": lane.execution_branch,
        "base_commit": lane.base_commit,
        "bootstrap_authority": DELIVERY_BOOTSTRAP_AUTHORITY,
        "agent_permission_mode": (
            "dangerously-skip-permissions"
            if lane.worker_kind == "claude"
            else "default"
        ),
    }
    if (
        registry.state not in allowed_states
        or registry.herdr_session_owned is not True
        or any(getattr(registry, name) != value for name, value in expected.items())
        or any(
            not isinstance(value, str) or not value
            for value in (
                registry.workspace_id,
                registry.tab_id,
                registry.pane_id,
                registry.herdr_session_name,
                registry.updated_at,
            )
        )
    ):
        raise ValueError("Delivery lane registry conflicts with the lane contract")
    if (
        registry.dispatch_id is not None
        and re.fullmatch(r"delivery-dispatch:[0-9a-f]{32}", registry.dispatch_id)
        is None
    ):
        raise ValueError("Delivery lane registry conflicts with the lane contract")
    if registry.state in {"created", "running", "blocked"} and (
        registry.head_commit is not None or registry.integrated_commit is not None
    ):
        raise ValueError("Delivery lane registry conflicts with the lane contract")
    if registry.state in {"terminal", "integrated"} and (
        not isinstance(registry.head_commit, str)
        or not re.fullmatch(r"[0-9a-f]{40}", registry.head_commit)
    ):
        raise ValueError("Delivery lane registry conflicts with the lane contract")
    if registry.state == "integrated" and (
        not isinstance(registry.integrated_commit, str)
        or not re.fullmatch(r"[0-9a-f]{40}", registry.integrated_commit)
    ):
        raise ValueError("Delivery lane registry conflicts with the lane contract")
    if registry.state != "integrated" and registry.integrated_commit is not None:
        raise ValueError("Delivery lane registry conflicts with the lane contract")
    if registry.state in {"created", "running"} and (
        registry.evidence_source is not None
        or registry.final_report_digest is not None
        or registry.blocker_summary is not None
    ):
        raise ValueError("Delivery lane registry conflicts with the lane contract")
    if registry.state == "blocked" and (
        registry.evidence_source != "herdr-final-report"
        or not isinstance(registry.final_report_digest, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", registry.final_report_digest)
        or not isinstance(registry.blocker_summary, str)
        or not registry.blocker_summary
        or len(registry.blocker_summary) > 500
        or _SENSITIVE_VALUE_RE.search(registry.blocker_summary) is not None
    ):
        raise ValueError("Delivery lane registry conflicts with the lane contract")
    if registry.state in {"terminal", "integrated"} and (
        registry.evidence_source not in {"herdr-final-report", "registry-git-recovery"}
        or (
            registry.evidence_source == "herdr-final-report"
            and (
                not isinstance(registry.final_report_digest, str)
                or not re.fullmatch(
                    r"sha256:[0-9a-f]{64}", registry.final_report_digest
                )
            )
        )
        or (
            registry.evidence_source == "registry-git-recovery"
            and registry.final_report_digest is not None
        )
        or registry.blocker_summary is not None
    ):
        raise ValueError("Delivery lane registry conflicts with the lane contract")


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

    def preview_repair(self, *, map_id: str) -> dict[str, Any]:
        """Read one safe opaque-coordinate repair without mutating Herdr."""
        with self._storage.pm_runtime_lease(map_id):
            record = self._storage.pm_runtime(map_id)
            if record is None or record.get("state") != "repair_required":
                raise CoordinatorRuntimeError(
                    reason="runtime_repair_not_required",
                    retryable=False,
                    repair_required=True,
                )
            self._validate_recorded_ownership_identity(record)
            executable = str(record.get("herdr_executable") or "herdr")
            namespace = str(record["session_namespace"])
            if namespace not in self._sessions(executable):
                raise CoordinatorRuntimeError(
                    reason="owned_session_unavailable",
                    retryable=True,
                )
            prefix = [executable, "--session", namespace]
            workspaces = self._result(
                self._command_json([*prefix, "workspace", "list"])
            ).get("workspaces")
            matching_workspaces = (
                [
                    workspace
                    for workspace in workspaces
                    if isinstance(workspace, Mapping)
                    and workspace.get("label") == record["workspace_label"]
                ]
                if isinstance(workspaces, list)
                else []
            )
            if len(matching_workspaces) != 1:
                raise CoordinatorRuntimeError(
                    reason=(
                        "owned_workspace_unavailable"
                        if not matching_workspaces
                        else "workspace_recovery_ambiguous"
                    ),
                    retryable=not matching_workspaces,
                    repair_required=True,
                )
            workspace_id = self._opaque(matching_workspaces[0].get("workspace_id"))
            workspace = self._workspace_from(
                self._command_json([*prefix, "workspace", "get", workspace_id])
            )
            if (
                workspace.get("workspace_id") != workspace_id
                or workspace.get("label") != record["workspace_label"]
            ):
                raise CoordinatorRuntimeError(
                    reason="workspace_ownership_mismatch",
                    retryable=False,
                    repair_required=True,
                )
            window_id = self._opaque(workspace.get("active_tab_id"))
            panes = self._result(
                self._command_json(
                    [*prefix, "pane", "list", "--workspace", workspace_id]
                )
            ).get("panes")
            matching_panes = (
                [
                    pane
                    for pane in panes
                    if isinstance(pane, Mapping)
                    and pane.get("workspace_id") == workspace_id
                    and pane.get("tab_id") == window_id
                    and pane.get("cwd") == record["repository_path"]
                ]
                if isinstance(panes, list)
                else []
            )
            if len(matching_panes) != 1:
                raise CoordinatorRuntimeError(
                    reason=(
                        "owned_pane_unavailable"
                        if not matching_panes
                        else "workspace_recovery_ambiguous"
                    ),
                    retryable=not matching_panes,
                    repair_required=True,
                )
            pane_id = self._opaque(matching_panes[0].get("pane_id"))
            agent = self._get_agent([*prefix, "agent", "get", str(record["agent_id"])])
            if agent is None:
                raise CoordinatorRuntimeError(
                    reason="owned_agent_unavailable",
                    retryable=True,
                    repair_required=True,
                )
            expected_agent = {
                "name": record["agent_id"],
                "agent": "hermes",
                "workspace_id": workspace_id,
                "tab_id": window_id,
                "pane_id": pane_id,
            }
            if any(agent.get(name) != value for name, value in expected_agent.items()):
                raise CoordinatorRuntimeError(
                    reason="opaque_coordinate_mismatch",
                    retryable=False,
                    repair_required=True,
                )
            agent_session = agent.get("agent_session")
            if (
                not isinstance(agent_session, Mapping)
                or agent_session.get("agent") != "hermes"
                or agent_session.get("kind") != "id"
            ):
                raise CoordinatorRuntimeError(
                    reason="agent_session_identity_unsafe",
                    retryable=False,
                    repair_required=True,
                )
            failure = record.get("failure") or {}
            return {
                "before": {
                    "workspace_id": record.get("workspace_id"),
                    "window_id": record.get("window_id"),
                    "pane_id": record.get("pane_id"),
                    "agent_session_id": record.get("agent_session_id"),
                    "state": record["state"],
                    "repair_reason": failure.get("reason"),
                },
                "after": {
                    "workspace_id": workspace_id,
                    "window_id": window_id,
                    "pane_id": pane_id,
                    "agent_session_id": self._opaque(agent_session.get("value")),
                    "state": "pm_ready",
                },
                "evidence": {
                    "session_namespace": namespace,
                    "workspace_label": record["workspace_label"],
                    "ownership_marker": record["ownership_marker"],
                    "exact_workspace_count": 1,
                    "exact_pane_count": 1,
                    "exact_agent_count": 1,
                },
            }

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
        turn_id = f"commission-ready:{map_id}"
        content = json.dumps(
            dict(payload),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._storage.pm_runtime_lease(map_id):
            record = self._storage.pm_runtime(map_id)
            assignment = self._storage.pm_assignment(map_id)
            if record is None or assignment is None:
                raise CoordinatorRuntimeError(
                    reason="runtime_not_reserved",
                    retryable=False,
                    repair_required=True,
                )
            self._validate_owned_record(record)
            try:
                self._validate_resume_identity(
                    assignment=assignment,
                    record=record,
                    profile_name=str(assignment["profile_name"]),
                    session_id=str(assignment["session_id"]),
                    coordinator_id=str(assignment["coordinator_id"]),
                    require_idle=False,
                )
                if (
                    assignment["state"] != "active"
                    or assignment.get("active_turn_id") != turn_id
                ):
                    raise CoordinatorRuntimeError(
                        reason="pm_ready_turn_mismatch",
                        retryable=False,
                        repair_required=True,
                    )
                content_hash = self.resume_content_hash(content)
                turn_marker = self.resume_turn_marker(
                    map_id=map_id,
                    profile_name=str(assignment["profile_name"]),
                    session_id=str(assignment["session_id"]),
                    coordinator_id=str(assignment["coordinator_id"]),
                    turn_id=turn_id,
                    content=content,
                )
                existing = self._storage.pm_resume_receipt(
                    map_id=map_id,
                    turn_id=turn_id,
                )
                agent: Mapping[str, Any] | None = None
                should_prompt = existing is None
                if existing is not None:
                    if any(
                        existing[name] != expected
                        for name, expected in (
                            ("profile_name", assignment["profile_name"]),
                            ("session_id", assignment["session_id"]),
                            ("coordinator_id", assignment["coordinator_id"]),
                            ("content_hash", content_hash),
                            ("turn_marker", turn_marker),
                        )
                    ):
                        raise CoordinatorRuntimeError(
                            reason="pm_resume_identity_mismatch",
                            retryable=False,
                            repair_required=True,
                        )
                    if existing["state"] == "dispatching":
                        agent = self._validate_live_runtime(record)
                        marker_visible = self._pm_turn_marker_visible(
                            record=record,
                            turn_marker=turn_marker,
                        )
                        if marker_visible:
                            self._storage.confirm_pm_resume_receipt(
                                map_id=map_id,
                                turn_id=turn_id,
                                content_hash=content_hash,
                                prompted_at=self._clock(),
                            )
                        elif (
                            existing.get("delivery_rejected") == 1
                            and agent.get("agent_status") == "idle"
                        ):
                            self._storage.allow_pm_resume_retry(
                                map_id=map_id,
                                turn_id=turn_id,
                                content_hash=content_hash,
                            )
                            should_prompt = True
                        else:
                            raise CoordinatorRuntimeError(
                                reason="pm_ready_delivery_unconfirmed",
                                retryable=True,
                            )
                if should_prompt:
                    if agent is None:
                        agent = self._validate_live_runtime(record)
                    if agent.get("agent_status") != "idle":
                        raise CoordinatorRuntimeError(
                            reason="pm_resume_requires_idle_agent",
                            retryable=True,
                        )
                    self._storage.prepare_pm_resume_receipt(
                        map_id=map_id,
                        turn_id=turn_id,
                        profile_name=str(assignment["profile_name"]),
                        session_id=str(assignment["session_id"]),
                        coordinator_id=str(assignment["coordinator_id"]),
                        content_hash=content_hash,
                        turn_marker=turn_marker,
                        prepared_at=self._clock(),
                    )
                    prompt_payload = {
                        **dict(payload),
                        "coordinator_turn_marker": turn_marker,
                    }
                    try:
                        result = self._command_json(
                            [
                                str(record.get("herdr_executable") or "herdr"),
                                "--session",
                                str(record["session_namespace"]),
                                "agent",
                                "prompt",
                                str(record["agent_id"]),
                                json.dumps(
                                    prompt_payload,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                    sort_keys=True,
                                ),
                                "--wait",
                                "--timeout",
                                "30000",
                            ]
                        )
                    except CoordinatorRuntimeError as error:
                        if error.reason == "command_failed":
                            self._storage.mark_pm_resume_delivery_rejected(
                                map_id=map_id,
                                turn_id=turn_id,
                                content_hash=content_hash,
                            )
                        raise
                    prompted = self._agent_from(result)
                    self._assert_agent_coordinates(record, prompted)
                    self._assert_agent_coordinates(record, agent)
                    if prompted.get("agent_status") not in {"idle", "done"}:
                        raise CoordinatorRuntimeError(
                            reason="pm_ready_prompt_unconfirmed",
                            retryable=True,
                        )
                    self._storage.confirm_pm_resume_receipt(
                        map_id=map_id,
                        turn_id=turn_id,
                        content_hash=content_hash,
                        prompted_at=self._clock(),
                    )
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

    @staticmethod
    def resume_content_hash(content: str) -> str:
        return "sha256:" + hashlib.sha256(content.encode()).hexdigest()

    @classmethod
    def resume_turn_marker(
        cls,
        *,
        map_id: str,
        profile_name: str,
        session_id: str,
        coordinator_id: str,
        turn_id: str,
        content: str,
    ) -> str:
        identity = json.dumps(
            {
                "map_id": map_id,
                "profile_name": profile_name,
                "session_id": session_id,
                "coordinator_id": coordinator_id,
                "turn_id": turn_id,
                "content_hash": cls.resume_content_hash(content),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()
        return f"[map-governance:coordinator-turn:v1:{digest}]"

    @classmethod
    def resume_prompt_content(
        cls,
        *,
        map_id: str,
        profile_name: str,
        session_id: str,
        coordinator_id: str,
        turn_id: str,
        content: str,
    ) -> str:
        marker = cls.resume_turn_marker(
            map_id=map_id,
            profile_name=profile_name,
            session_id=session_id,
            coordinator_id=coordinator_id,
            turn_id=turn_id,
            content=content,
        )
        return f"{content}\n\n{marker}"

    def readback(self, *, map_id: str, turn_id: str) -> Mapping[str, Any] | None:
        receipt = self._storage.pm_resume_receipt(map_id=map_id, turn_id=turn_id)
        if receipt is None:
            return None
        with self._storage.pm_runtime_lease(map_id):
            receipt = self._storage.pm_resume_receipt(
                map_id=map_id,
                turn_id=turn_id,
            )
            if receipt is None:
                return None
            if receipt["state"] == "dispatching":
                assignment = self._storage.pm_assignment(map_id)
                record = self._storage.pm_runtime(map_id)
                if assignment is None or record is None:
                    raise CoordinatorRuntimeError(
                        reason="pm_resume_binding_missing",
                        retryable=False,
                        repair_required=True,
                    )
                self._validate_resume_identity(
                    assignment=assignment,
                    record=record,
                    profile_name=str(receipt["profile_name"]),
                    session_id=str(receipt["session_id"]),
                    coordinator_id=str(receipt["coordinator_id"]),
                    require_idle=True,
                )
                self._validate_owned_record(record)
                agent = self._validate_live_runtime(record)
                turn_marker = receipt.get("turn_marker")
                if not isinstance(turn_marker, str) or not turn_marker:
                    raise CoordinatorRuntimeError(
                        reason="pm_resume_marker_missing",
                        retryable=False,
                        repair_required=True,
                    )
                if not self._pm_turn_marker_visible(
                    record=record,
                    turn_marker=turn_marker,
                ):
                    if (
                        receipt.get("delivery_rejected") == 1
                        and agent.get("agent_status") == "idle"
                    ):
                        self._storage.allow_pm_resume_retry(
                            map_id=map_id,
                            turn_id=turn_id,
                            content_hash=str(receipt["content_hash"]),
                        )
                    return None
                self._storage.confirm_pm_resume_receipt(
                    map_id=map_id,
                    turn_id=turn_id,
                    content_hash=str(receipt["content_hash"]),
                    prompted_at=self._clock(),
                )
                receipt = self._storage.pm_resume_receipt(
                    map_id=map_id,
                    turn_id=turn_id,
                )
                if receipt is None:  # pragma: no cover
                    raise RuntimeError("PM resume receipt disappeared")
        return {
            "map_id": map_id,
            "turn_id": turn_id,
            "content_hash": receipt["content_hash"],
            "prompted_at": receipt["prompted_at"],
        }

    def resume(
        self,
        *,
        map_id: str,
        profile_name: str,
        session_id: str,
        coordinator_id: str,
        turn_id: str,
        content: str = "",
    ) -> None:
        """Prompt one verified idle PM root and persist a retry-stable receipt."""
        values = (profile_name, session_id, coordinator_id, turn_id, content)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("PM resume requires complete request-scoped identity")
        if len(content.encode()) > 8_192 or self._contains_sensitive(content):
            raise ValueError("PM resume content is too large or sensitive")
        content_hash = self.resume_content_hash(content)
        turn_marker = self.resume_turn_marker(
            map_id=map_id,
            profile_name=profile_name,
            session_id=session_id,
            coordinator_id=coordinator_id,
            turn_id=turn_id,
            content=content,
        )
        with self._storage.pm_runtime_lease(map_id):
            existing = self._storage.pm_resume_receipt(
                map_id=map_id,
                turn_id=turn_id,
            )
            if existing is not None:
                if any(
                    existing[name] != expected
                    for name, expected in (
                        ("profile_name", profile_name),
                        ("session_id", session_id),
                        ("coordinator_id", coordinator_id),
                        ("content_hash", content_hash),
                    )
                ):
                    raise CoordinatorRuntimeError(
                        reason="pm_resume_identity_mismatch",
                        retryable=False,
                        repair_required=True,
                    )
                if existing["state"] == "prompted":
                    return
                if not existing.get("retry_allowed"):
                    raise CoordinatorRuntimeError(
                        reason="pm_resume_delivery_unconfirmed",
                        retryable=True,
                    )
            assignment = self._storage.pm_assignment(map_id)
            record = self._storage.pm_runtime(map_id)
            if assignment is None or record is None:
                raise CoordinatorRuntimeError(
                    reason="pm_resume_binding_missing",
                    retryable=False,
                    repair_required=True,
                )
            self._validate_resume_identity(
                assignment=assignment,
                record=record,
                profile_name=profile_name,
                session_id=session_id,
                coordinator_id=coordinator_id,
                require_idle=True,
            )
            self._validate_owned_record(record)
            agent = self._validate_live_runtime(record)
            if agent.get("agent_status") != "idle":
                raise CoordinatorRuntimeError(
                    reason="pm_resume_requires_idle_agent",
                    retryable=True,
                )
            prepared_existing = self._storage.prepare_pm_resume_receipt(
                map_id=map_id,
                turn_id=turn_id,
                profile_name=profile_name,
                session_id=session_id,
                coordinator_id=coordinator_id,
                content_hash=content_hash,
                turn_marker=turn_marker,
                prepared_at=self._clock(),
            )
            if prepared_existing != (existing is not None):
                latest = self._storage.pm_resume_receipt(
                    map_id=map_id,
                    turn_id=turn_id,
                )
                if latest is not None and latest["state"] == "prompted":
                    return
                raise CoordinatorRuntimeError(
                    reason="pm_resume_delivery_unconfirmed",
                    retryable=True,
                )
            try:
                result = self._command_json(
                    [
                        str(record["herdr_executable"]),
                        "--session",
                        str(record["session_namespace"]),
                        "agent",
                        "prompt",
                        str(record["agent_id"]),
                        self.resume_prompt_content(
                            map_id=map_id,
                            profile_name=profile_name,
                            session_id=session_id,
                            coordinator_id=coordinator_id,
                            turn_id=turn_id,
                            content=content,
                        ),
                        "--wait",
                        "--until",
                        "working",
                        "--timeout",
                        "30000",
                    ]
                )
            except CoordinatorRuntimeError as error:
                if error.reason == "command_failed":
                    self._storage.mark_pm_resume_delivery_rejected(
                        map_id=map_id,
                        turn_id=turn_id,
                        content_hash=content_hash,
                    )
                raise
            prompted = self._agent_from(result)
            self._assert_agent_coordinates(record, agent)
            self._assert_agent_coordinates(record, prompted)
            if prompted.get("agent_status") != "working":
                raise CoordinatorRuntimeError(
                    reason="pm_resume_confirmation_not_working",
                    retryable=True,
                )
            self._storage.confirm_pm_resume_receipt(
                map_id=map_id,
                turn_id=turn_id,
                content_hash=content_hash,
                prompted_at=self._clock(),
            )

    def _pm_turn_marker_visible(
        self,
        *,
        record: Mapping[str, Any],
        turn_marker: str,
    ) -> bool:
        result = self._result(
            self._command_json(
                [
                    str(record.get("herdr_executable") or "herdr"),
                    "--session",
                    str(record["session_namespace"]),
                    "agent",
                    "read",
                    str(record["agent_id"]),
                    "--source",
                    "recent-unwrapped",
                    "--lines",
                    "400",
                    "--format",
                    "text",
                ]
            )
        )
        read = result.get("read")
        if result.get("type") != "pane_read" or not isinstance(read, Mapping):
            raise self._malformed("pm_resume_readback_malformed")
        if any(
            read.get(name) != expected
            for name, expected in (
                ("workspace_id", record["workspace_id"]),
                ("tab_id", record["window_id"]),
                ("pane_id", record["pane_id"]),
            )
        ):
            raise CoordinatorRuntimeError(
                reason="pm_resume_readback_coordinate_mismatch",
                retryable=False,
                repair_required=True,
            )
        text = read.get("text")
        if (
            read.get("source") != "recent_unwrapped"
            or read.get("format") != "text"
            or not isinstance(text, str)
            or len(text) > 65_536
        ):
            raise self._malformed("pm_resume_readback_malformed")
        if turn_marker in text:
            return True
        if read.get("truncated") is not False:
            raise CoordinatorRuntimeError(
                reason="pm_resume_readback_incomplete",
                retryable=True,
            )
        return False

    @staticmethod
    def _validate_resume_identity(
        *,
        assignment: Mapping[str, Any],
        record: Mapping[str, Any],
        profile_name: str,
        session_id: str,
        coordinator_id: str,
        require_idle: bool,
    ) -> None:
        if (
            assignment["profile_name"] != profile_name
            or assignment["session_id"] != session_id
            or assignment["coordinator_id"] != coordinator_id
            or record["pm_profile"] != profile_name
        ):
            raise CoordinatorRuntimeError(
                reason="pm_resume_identity_mismatch",
                retryable=False,
                repair_required=True,
            )
        if require_idle and assignment["state"] != "idle":
            raise CoordinatorRuntimeError(
                reason="pm_resume_requires_idle_assignment",
                retryable=True,
            )

    def prepare_lane(self, request: DeliveryRuntimeRequest) -> dict[str, Any]:
        """Reserve and verify one pane before any coding worker is started."""
        self._validate_delivery_request(request)
        with self._storage.pm_runtime_lease(request.map_id):
            record = self._delivery_runtime_record(request)
            self._validate_owned_record(record)
            self._validate_live_runtime(record)
            self._validate_delivery_git(request, collecting=False)
            prefix = [
                request.context.herdr_executable,
                "--session",
                str(record["session_namespace"]),
            ]
            pane = self._delivery_pane(
                prefix=prefix,
                record=record,
                execution_worktree=request.lane.execution_worktree,
            )
            occupants = self._delivery_pane_occupants(prefix=prefix, pane=pane)
            if occupants:
                raise CoordinatorRuntimeError(
                    reason="overlapping_lane_ownership",
                    retryable=False,
                    repair_required=True,
                )
            registry = self._delivery_registry(
                request=request,
                record=record,
                pane=pane,
                state="created",
            )
            return {
                "map_id": request.map_id,
                "state": "prepared",
                "dispatch_id": delivery_dispatch_id(request),
                "worker_kind": request.lane.worker_kind,
                "registry": registry.payload(),
                "coordinate_receipt": self._registry_digest(registry),
                "remote_actions": "forbidden",
            }

    def dispatch_lane(self, request: DeliveryRuntimeRequest) -> dict[str, Any]:
        """Dispatch only after tracker readback of the prepared lane registry."""
        self._validate_delivery_request(request)
        registry = self._validate_delivery_registry(
            request,
            allowed_states={"created", "running", "blocked"},
        )
        with self._storage.pm_runtime_lease(request.map_id):
            record = self._delivery_runtime_record(request)
            self._validate_owned_record(record)
            self._validate_live_runtime(record)
            if (
                registry.herdr_session_name != record.get("session_namespace")
                or registry.workspace_id != record.get("workspace_id")
                or registry.tab_id != record.get("window_id")
            ):
                raise CoordinatorRuntimeError(
                    reason="lane_registry_readback_mismatch",
                    retryable=False,
                    repair_required=True,
                )
            self._validate_delivery_git(
                request,
                collecting=True,
                allow_active_execution=True,
            )
            prefix = [
                request.context.herdr_executable,
                "--session",
                registry.herdr_session_name,
            ]
            pane = self._registered_delivery_pane(
                prefix=prefix,
                registry=registry,
            )
            integration_head = self._local_result(
                [
                    "git",
                    "-C",
                    request.lane.integration_worktree,
                    "rev-parse",
                    "HEAD",
                ]
            )
            if integration_head != (
                request.integration_expected_head or request.lane.base_commit
            ):
                self._raise_integration_head_mismatch(
                    request,
                    actual_head=integration_head,
                    fallback_reason="overlapping_integration_ownership",
                )
            worker_name = self.delivery_agent_name(
                str(record["lifecycle_id"]),
                delivery_confirmed_dispatch_id(request),
                request.lane.worker_kind,
            )
            occupants = self._delivery_pane_occupants(prefix=prefix, pane=pane)
            if len(occupants) > 1 or any(
                agent.get("name") != worker_name for agent in occupants
            ):
                raise CoordinatorRuntimeError(
                    reason="overlapping_lane_ownership",
                    retryable=False,
                    repair_required=True,
                )
            packet = delivery_worker_prompt(request, registry)
            if registry.state == "blocked":
                packet = {
                    **packet,
                    "resume": {
                        "authority": "map-returned-to-delivery",
                        "requirements": [
                            "重新读取 implementation ticket 与当前 Git evidence。",
                            "只解决已记录 blocker，保持同一 lane、worktree 与单 commit contract。",
                            "重新运行 checks/review 并输出新的完整 final report。",
                        ],
                    },
                }
            self._validate_payload(packet)
            worker = self._get_agent([*prefix, "agent", "get", worker_name])
            if worker is None:
                if registry.state in {"running", "blocked"}:
                    raise CoordinatorRuntimeError(
                        reason="worker_registry_readback_mismatch",
                        retryable=False,
                        repair_required=True,
                    )
                self._validate_delivery_git(request, collecting=False)
                arguments = [
                    *prefix,
                    "agent",
                    "start",
                    worker_name,
                    "--kind",
                    request.lane.worker_kind,
                    "--pane",
                    str(pane["pane_id"]),
                    "--timeout",
                    "30000",
                ]
                if request.lane.worker_kind == "claude":
                    arguments.extend(["--", "--dangerously-skip-permissions"])
                payload = self._command_json(arguments)
                if self._result(payload).get("type") != "agent_started":
                    raise self._malformed("worker_start_unconfirmed")
                worker = self._agent_from(payload)
            self._validate_worker_agent(
                worker,
                worker_name=worker_name,
                worker_kind=request.lane.worker_kind,
                workspace_id=str(record["workspace_id"]),
                window_id=str(record["window_id"]),
                pane_id=str(pane["pane_id"]),
                execution_worktree=request.lane.execution_worktree,
            )
            worker_status = worker.get("agent_status")
            if worker_status == "working" or (
                worker_status == "done" and registry.state != "blocked"
            ):
                return self._dispatch_projection(
                    request,
                    record=record,
                    pane=pane,
                    registry=registry,
                    idempotent=True,
                )
            if worker_status == "blocked":
                raise CoordinatorRuntimeError(
                    reason="worker_requires_input",
                    retryable=True,
                )
            prompt = self._command_json(
                [
                    *prefix,
                    "agent",
                    "prompt",
                    worker_name,
                    json.dumps(
                        packet,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    "--wait",
                    "--until",
                    "working",
                    "--timeout",
                    "30000",
                ]
            )
            if self._result(prompt).get("type") != "agent_prompted":
                raise self._malformed("worker_prompt_unconfirmed")
            prompted = self._agent_from(prompt)
            self._validate_worker_agent(
                prompted,
                worker_name=worker_name,
                worker_kind=request.lane.worker_kind,
                workspace_id=str(record["workspace_id"]),
                window_id=str(record["window_id"]),
                pane_id=str(pane["pane_id"]),
                execution_worktree=request.lane.execution_worktree,
                expected_status="working",
            )
            return self._dispatch_projection(
                request,
                record=record,
                pane=pane,
                registry=registry,
                idempotent=False,
            )

    @staticmethod
    def _dispatch_projection(
        request: DeliveryRuntimeRequest,
        *,
        record: Mapping[str, Any],
        pane: Mapping[str, Any],
        registry: DeliveryLaneRegistry,
        idempotent: bool,
    ) -> dict[str, Any]:
        running_registry = replace(
            registry,
            state="running",
            head_commit=None,
            integrated_commit=None,
            evidence_source=None,
            final_report_digest=None,
            blocker_summary=None,
            updated_at=(
                registry.updated_at
                if registry.state == "running"
                else request.registry_timestamp
            ),
        )
        return {
            "map_id": request.map_id,
            "state": "dispatched",
            "dispatch_id": delivery_confirmed_dispatch_id(request),
            "worker_kind": request.lane.worker_kind,
            "completion_contract": request.lane.completion_contract,
            "checkpoint": "dispatch_handoff",
            "registry": running_registry.payload(),
            "coordinate_receipt": CoordinatorRuntime._registry_digest(running_registry),
            "remote_actions": "forbidden",
            "idempotent": idempotent,
        }

    def collect_lane(self, request: DeliveryRuntimeRequest) -> dict[str, Any]:
        """Collect durable lane evidence, integrate once, and validate locally."""
        self._validate_delivery_request(request)
        if len(request.integration_predecessor_commits) != (
            request.lane.integration_order - 1
        ):
            raise CoordinatorRuntimeError(
                reason="integration_order_not_ready",
                retryable=True,
            )
        registry = self._validate_delivery_registry(
            request,
            allowed_states={"running", "blocked", "terminal", "integrated"},
        )
        with self._storage.pm_runtime_lease(request.map_id):
            record = self._delivery_runtime_record(request)
            self._validate_owned_record(record)
            if (
                registry.herdr_session_name != record.get("session_namespace")
                or registry.workspace_id != record.get("workspace_id")
                or registry.tab_id != record.get("window_id")
            ):
                raise CoordinatorRuntimeError(
                    reason="lane_registry_readback_mismatch",
                    retryable=False,
                    repair_required=True,
                )
            self._validate_delivery_git(
                request,
                collecting=True,
                allow_active_execution=True,
            )
            lane = request.lane
            if registry.state == "blocked":
                return self._blocked_delivery_projection(request, registry)
            final_report, transport_limitations = self._collect_worker_report(
                request=request,
                record=record,
                registry=registry,
            )
            if final_report.get("status") == "blocked":
                blocked_registry = replace(
                    registry,
                    state="blocked",
                    updated_at=request.registry_timestamp,
                    evidence_source="herdr-final-report",
                    final_report_digest=str(final_report["digest"]),
                    blocker_summary=str(final_report["blocker"]),
                )
                return self._blocked_delivery_projection(
                    request,
                    blocked_registry,
                )
            execution_commit = self._execution_commit(lane)
            self._corroborate_worker_commit(final_report, execution_commit)
            if registry.head_commit not in {None, execution_commit}:
                raise CoordinatorRuntimeError(
                    reason="worker_commit_registry_mismatch",
                    retryable=False,
                    repair_required=True,
                )
            integration_head = self._local_result(
                ["git", "-C", lane.integration_worktree, "rev-parse", "HEAD"]
            )
            expected_parent = request.integration_expected_head or lane.base_commit
            already_integrated = False
            if integration_head != expected_parent:
                already_integrated = self._patch_is_integrated(
                    lane=lane,
                    integration_head=integration_head,
                    execution_commit=execution_commit,
                )
                if not already_integrated:
                    raise CoordinatorRuntimeError(
                        reason="overlapping_integration_ownership",
                        retryable=False,
                        repair_required=True,
                    )
            if registry.state == "integrated" and (
                registry.integrated_commit is None
                or registry.integrated_commit != integration_head
                or not already_integrated
            ):
                raise CoordinatorRuntimeError(
                    reason="overlapping_integration_ownership",
                    retryable=False,
                    repair_required=True,
                )
            if not already_integrated:
                self._require_integration_ready_for_cherry_pick(
                    lane,
                    expected_head=expected_parent,
                )
                try:
                    self._local_result(
                        [
                            "git",
                            "-C",
                            lane.integration_worktree,
                            "cherry-pick",
                            execution_commit,
                        ]
                    )
                except CoordinatorRuntimeError as error:
                    self._handle_failed_cherry_pick(
                        lane=lane,
                        expected_parent=integration_head,
                        cause=error,
                    )
            integration_commit = self._local_result(
                ["git", "-C", lane.integration_worktree, "rev-parse", "HEAD"]
            )
            self._require_owned_integration_patch(
                lane=lane,
                integration_commit=integration_commit,
                execution_commit=execution_commit,
                expected_parent=expected_parent,
            )
            self._require_clean_integration(lane.integration_worktree)
            try:
                self._local_result(
                    list(lane.validation_argv), cwd=lane.integration_worktree
                )
            except CoordinatorRuntimeError as error:
                raise CoordinatorRuntimeError(
                    reason="focused_validation_failed",
                    retryable=False,
                ) from error
            self._require_integration_head(
                lane.integration_worktree,
                integration_commit,
            )
            self._require_integration_branch(lane)
            self._require_clean_integration(lane.integration_worktree)
            if self._execution_commit(lane) != execution_commit:
                raise CoordinatorRuntimeError(
                    reason="worker_completion_contract_unmet",
                    retryable=False,
                    repair_required=True,
                )
            terminal_registry = replace(
                registry,
                state="terminal",
                head_commit=execution_commit,
                integrated_commit=None,
                updated_at=request.registry_timestamp,
                evidence_source=(
                    "registry-git-recovery"
                    if final_report["status"] == "recovered_from_git"
                    else registry.evidence_source or "herdr-final-report"
                ),
                final_report_digest=(
                    None
                    if final_report["status"] == "recovered_from_git"
                    else final_report.get("digest") or registry.final_report_digest
                ),
            )
            integrated_registry = replace(
                terminal_registry,
                state="integrated",
                integrated_commit=integration_commit,
            )
            limitations = list(
                dict.fromkeys([*lane.known_limitations, *transport_limitations])
            )
            remote_limitation = (
                "Push, PR, merge, release, and Issue closure were not performed."
            )
            if remote_limitation not in limitations:
                limitations.append(remote_limitation)
            return {
                "map_id": request.map_id,
                "state": "locally_validated",
                "dispatch_id": delivery_confirmed_dispatch_id(request),
                "worker_kind": lane.worker_kind,
                "terminal_registry": terminal_registry.payload(),
                "integrated_registry": integrated_registry.payload(),
                "registry_receipt": self._registry_digest(integrated_registry),
                "evidence": {
                    "execution_commit": execution_commit,
                    "integration_commit": integration_commit,
                    "final_report": final_report,
                    "validation": "passed",
                    "completion_contract": "satisfied",
                },
                "limitations": limitations,
                "acceptance_recommendation": "accept",
                "remote_actions": "forbidden",
                "idempotent": already_integrated,
            }

    @staticmethod
    def _blocked_delivery_projection(
        request: DeliveryRuntimeRequest,
        registry: DeliveryLaneRegistry,
    ) -> dict[str, Any]:
        return {
            "map_id": request.map_id,
            "state": "blocked",
            "dispatch_id": delivery_confirmed_dispatch_id(request),
            "worker_kind": request.lane.worker_kind,
            "blocked_registry": registry.payload(),
            "blocker": {
                "reason": "worker_reported_blocker",
                "retryable": True,
                "summary": registry.blocker_summary,
            },
            "remote_actions": "forbidden",
        }

    @staticmethod
    def _delivery_registry(
        *,
        request: DeliveryRuntimeRequest,
        record: Mapping[str, Any],
        pane: Mapping[str, Any],
        state: str,
    ) -> DeliveryLaneRegistry:
        return DeliveryLaneRegistry(
            work_item=request.lane.ticket_url,
            role="implementation",
            lane_id=request.lane.lane_id,
            runtime=f"herdr-{request.lane.worker_kind}-pane",
            state=state,
            workspace_id=str(pane["workspace_id"]),
            tab_id=str(pane["tab_id"]),
            pane_id=str(pane["pane_id"]),
            herdr_session_name=str(record["session_namespace"]),
            herdr_session_owned=True,
            bootstrap_authority=DELIVERY_BOOTSTRAP_AUTHORITY,
            agent_permission_mode=(
                "dangerously-skip-permissions"
                if request.lane.worker_kind == "claude"
                else "default"
            ),
            worktree=request.lane.execution_worktree,
            branch=request.lane.execution_branch,
            base_commit=request.lane.base_commit,
            head_commit=None,
            integrated_commit=None,
            updated_at=request.registry_timestamp,
            dispatch_id=delivery_dispatch_id(request),
        )

    @staticmethod
    def _registry_digest(registry: DeliveryLaneRegistry) -> str:
        payload = json.dumps(registry.payload(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()

    @classmethod
    def _validate_delivery_registry(
        cls,
        request: DeliveryRuntimeRequest,
        *,
        allowed_states: set[str],
    ) -> DeliveryLaneRegistry:
        registry = request.registry
        if not isinstance(registry, DeliveryLaneRegistry):
            raise CoordinatorRuntimeError(
                reason="lane_registry_readback_missing",
                retryable=True,
            )
        try:
            validate_delivery_lane_registry(
                lane=request.lane,
                registry=registry,
                allowed_states=allowed_states,
            )
        except ValueError as error:
            raise CoordinatorRuntimeError(
                reason="lane_registry_readback_mismatch",
                retryable=False,
                repair_required=True,
            ) from error
        return registry

    @classmethod
    def _execution_commit(cls, lane: DeliveryLaneSpec) -> str:
        execution_commit = cls._local_result(
            ["git", "-C", lane.execution_worktree, "rev-parse", "HEAD"]
        )
        if (
            cls._local_result(
                ["git", "-C", lane.execution_worktree, "branch", "--show-current"]
            )
            != lane.execution_branch
            or cls._local_result(
                ["git", "-C", lane.execution_worktree, "status", "--porcelain=v1"]
            )
            or cls._local_result(
                [
                    "git",
                    "-C",
                    lane.execution_worktree,
                    "merge-base",
                    lane.base_commit,
                    execution_commit,
                ]
            )
            != lane.base_commit
            or cls._local_result(
                [
                    "git",
                    "-C",
                    lane.execution_worktree,
                    "rev-list",
                    "--count",
                    f"{lane.base_commit}..{execution_commit}",
                ]
            )
            != "1"
        ):
            raise CoordinatorRuntimeError(
                reason="worker_completion_contract_unmet",
                retryable=False,
                repair_required=True,
            )
        return execution_commit

    @classmethod
    def _patch_is_integrated(
        cls,
        *,
        lane: DeliveryLaneSpec,
        integration_head: str,
        execution_commit: str,
    ) -> bool:
        if integration_head == execution_commit:
            return True
        evidence = cls._local_result(
            [
                "git",
                "-C",
                lane.execution_worktree,
                "cherry",
                integration_head,
                execution_commit,
                lane.base_commit,
            ]
        )
        return evidence.strip() == f"- {execution_commit}"

    @classmethod
    def _require_owned_integration_patch(
        cls,
        *,
        lane: DeliveryLaneSpec,
        integration_commit: str,
        execution_commit: str,
        expected_parent: str | None = None,
    ) -> None:
        actual_head = cls._local_result(
            ["git", "-C", lane.integration_worktree, "rev-parse", "HEAD"]
        )
        try:
            merge_base = cls._local_result(
                [
                    "git",
                    "-C",
                    lane.integration_worktree,
                    "merge-base",
                    lane.base_commit,
                    actual_head,
                ]
            )
        except CoordinatorRuntimeError as error:
            raise CoordinatorRuntimeError(
                reason="overlapping_integration_ownership",
                retryable=False,
                repair_required=True,
            ) from error
        commit_count = cls._local_result(
            [
                "git",
                "-C",
                lane.integration_worktree,
                "rev-list",
                "--count",
                f"{expected_parent or lane.base_commit}..{actual_head}",
            ]
        )
        if (
            cls._local_result(
                ["git", "-C", lane.integration_worktree, "branch", "--show-current"]
            )
            != lane.integration_branch
            or actual_head != integration_commit
            or merge_base != lane.base_commit
            or commit_count != "1"
            or not cls._patch_is_integrated(
                lane=lane,
                integration_head=actual_head,
                execution_commit=execution_commit,
            )
        ):
            raise CoordinatorRuntimeError(
                reason="overlapping_integration_ownership",
                retryable=False,
                repair_required=True,
            )

    @classmethod
    def _require_clean_integration(cls, integration_worktree: str) -> None:
        if cls._local_result(
            ["git", "-C", integration_worktree, "status", "--porcelain=v1"]
        ):
            raise CoordinatorRuntimeError(
                reason="integration_not_clean",
                retryable=False,
                repair_required=True,
            )

    @classmethod
    def _require_integration_head(
        cls,
        integration_worktree: str,
        expected_commit: str,
    ) -> None:
        if (
            cls._local_result(["git", "-C", integration_worktree, "rev-parse", "HEAD"])
            != expected_commit
        ):
            raise CoordinatorRuntimeError(
                reason="validation_changed_integration_history",
                retryable=False,
                repair_required=True,
            )

    @classmethod
    def _require_integration_branch(cls, lane: DeliveryLaneSpec) -> None:
        if (
            cls._local_result(
                ["git", "-C", lane.integration_worktree, "branch", "--show-current"]
            )
            != lane.integration_branch
        ):
            raise CoordinatorRuntimeError(
                reason="overlapping_integration_ownership",
                retryable=False,
                repair_required=True,
            )

    @classmethod
    def _require_integration_ready_for_cherry_pick(
        cls,
        lane: DeliveryLaneSpec,
        *,
        expected_head: str,
    ) -> None:
        cls._require_integration_branch(lane)
        if cls._local_result(
            ["git", "-C", lane.integration_worktree, "rev-parse", "HEAD"]
        ) != expected_head or cls._local_result(
            [
                "git",
                "-C",
                lane.integration_worktree,
                "status",
                "--porcelain=v1",
            ]
        ):
            raise CoordinatorRuntimeError(
                reason="overlapping_integration_ownership",
                retryable=False,
                repair_required=True,
            )
        for operation_head in ("CHERRY_PICK_HEAD", "MERGE_HEAD", "REVERT_HEAD"):
            try:
                cls._local_result(
                    [
                        "git",
                        "-C",
                        lane.integration_worktree,
                        "rev-parse",
                        "--verify",
                        operation_head,
                    ]
                )
            except CoordinatorRuntimeError:
                continue
            raise CoordinatorRuntimeError(
                reason="overlapping_integration_ownership",
                retryable=False,
                repair_required=True,
            )

    def _collect_worker_report(
        self,
        *,
        request: DeliveryRuntimeRequest,
        record: Mapping[str, Any],
        registry: DeliveryLaneRegistry,
    ) -> tuple[dict[str, Any], list[str]]:
        if registry.state in {"terminal", "integrated"}:
            limitations = (
                [DELIVERY_TRANSPORT_RECOVERY_LIMITATION]
                if registry.evidence_source == "registry-git-recovery"
                else []
            )
            return {
                "status": (
                    "recovered_from_git"
                    if registry.evidence_source == "registry-git-recovery"
                    else "recovered_from_terminal_registry"
                ),
                "digest": registry.final_report_digest,
            }, limitations
        try:
            self._validate_live_runtime(record)
        except CoordinatorRuntimeError as error:
            if error.reason in {
                "owned_session_unavailable",
                "owned_workspace_unavailable",
            }:
                return self._git_recovery_report()
            if error.reason not in {
                "owned_pane_unavailable",
                "owned_agent_unavailable",
            }:
                raise
        prefix = [
            request.context.herdr_executable,
            "--session",
            registry.herdr_session_name,
        ]
        worker_name = self.delivery_agent_name(
            str(record["lifecycle_id"]),
            delivery_confirmed_dispatch_id(request),
            request.lane.worker_kind,
        )
        pane = self._registered_delivery_pane_or_none(
            prefix=prefix,
            registry=registry,
        )
        occupants = self._delivery_pane_occupants(
            prefix=prefix,
            pane=pane or {"pane_id": registry.pane_id},
        )
        worker = self._get_agent([*prefix, "agent", "get", worker_name])
        if pane is None or worker is None:
            if worker is None and not occupants:
                return self._git_recovery_report()
            raise CoordinatorRuntimeError(
                reason="overlapping_lane_ownership",
                retryable=False,
                repair_required=True,
            )
        if len(occupants) != 1 or occupants[0].get("name") != worker_name:
            raise CoordinatorRuntimeError(
                reason="overlapping_lane_ownership",
                retryable=False,
                repair_required=True,
            )
        self._validate_worker_agent(
            worker,
            worker_name=worker_name,
            worker_kind=request.lane.worker_kind,
            workspace_id=registry.workspace_id,
            window_id=registry.tab_id,
            pane_id=registry.pane_id,
            execution_worktree=request.lane.execution_worktree,
        )
        if worker.get("agent_status") != "done":
            raise CoordinatorRuntimeError(
                reason=(
                    "worker_requires_input"
                    if worker.get("agent_status") == "blocked"
                    else "worker_not_terminal"
                ),
                retryable=True,
            )
        result = self._result(
            self._command_json(
                [
                    *prefix,
                    "agent",
                    "read",
                    worker_name,
                    "--source",
                    "recent-unwrapped",
                    "--lines",
                    "400",
                    "--format",
                    "text",
                ]
            )
        )
        read = result.get("read")
        if result.get("type") != "pane_read" or not isinstance(read, Mapping):
            return self._git_recovery_report()
        text = read.get("text")
        if (
            read.get("workspace_id") != registry.workspace_id
            or read.get("tab_id") != registry.tab_id
            or read.get("pane_id") != registry.pane_id
        ):
            raise CoordinatorRuntimeError(
                reason="worker_final_report_invalid",
                retryable=False,
                repair_required=True,
            )
        if (
            read.get("truncated") is not False
            or not isinstance(text, str)
            or len(text) > 65_536
        ):
            return self._git_recovery_report()
        report = self._latest_complete_worker_report(text)
        if report is None:
            return self._git_recovery_report()
        lane = request.lane
        expected_ticket = f"{lane.ticket_id} {lane.ticket_title} {lane.ticket_url}"
        expected_pane = (
            f"{registry.pane_id} {lane.execution_worktree} {lane.execution_branch}"
        )
        parsed = self._parse_worker_final_report(
            report,
            expected_ticket=expected_ticket,
            expected_pane=expected_pane,
        )
        self._corroborate_worker_coordinates(
            parsed,
            request=request,
            registry=registry,
        )
        return {
            **parsed,
            "digest": "sha256:" + hashlib.sha256(report.encode()).hexdigest(),
            "revision": read.get("revision"),
        }, []

    @staticmethod
    def _latest_complete_worker_report(text: str) -> str | None:
        marker_pattern = re.compile(
            r"(?m)^[\t ]*(FINAL_REPORT_BEGIN|FINAL_REPORT_END)[\t ]*\r?$"
        )
        pending_begin: re.Match[str] | None = None
        complete: tuple[re.Match[str], re.Match[str]] | None = None
        for marker in marker_pattern.finditer(text):
            if marker.group(1) == "FINAL_REPORT_BEGIN":
                pending_begin = marker
            elif pending_begin is not None:
                complete = (pending_begin, marker)
                pending_begin = None
        if pending_begin is not None:
            return (
                "FINAL_REPORT_BEGIN"
                + text[pending_begin.end() :]
                + "\nFINAL_REPORT_END"
            )
        if complete is None:
            return None
        begin, end = complete
        return (
            "FINAL_REPORT_BEGIN" + text[begin.end() : end.start()] + "FINAL_REPORT_END"
        )

    @staticmethod
    def _git_recovery_report() -> tuple[dict[str, Any], list[str]]:
        return (
            {"status": "recovered_from_git", "digest": None},
            [DELIVERY_TRANSPORT_RECOVERY_LIMITATION],
        )

    @classmethod
    def _parse_worker_final_report(
        cls,
        report: str,
        *,
        expected_ticket: str | None = None,
        expected_pane: str | None = None,
    ) -> dict[str, Any]:
        report = report.strip()
        aliases = {
            "ticket": "ticket",
            "状态": "status",
            "status": "status",
            "pane/worktree/branch": "pane",
            "commit": "commit",
            "checks": "checks",
            "review": "review",
            "dirty state": "dirty_state",
            "touched files": "touched_files",
            "blocker": "blocker",
        }
        canonical_order = (
            "ticket",
            "status",
            "pane",
            "commit",
            "checks",
            "review",
            "dirty_state",
            "touched_files",
            "blocker",
        )
        fields: dict[str, str] = {}
        order: list[str] = []
        if not (
            report.startswith("FINAL_REPORT_BEGIN")
            and report.endswith("FINAL_REPORT_END")
        ):
            raise cls._incomplete_worker_report()
        body = report.removeprefix("FINAL_REPORT_BEGIN").removesuffix(
            "FINAL_REPORT_END"
        )
        for line in body.splitlines():
            match = re.fullmatch(r"\s*([^:：]+?)\s*[:：]\s*(.*?)\s*", line)
            if match is None:
                continue
            name = aliases.get(match.group(1).strip().lower())
            if name is None:
                name = aliases.get(match.group(1).strip())
            if name is not None:
                if name in fields:
                    raise cls._negative_worker_report()
                fields[name] = match.group(2).strip()
                order.append(name)
        for name, expected in (
            ("ticket", expected_ticket),
            ("pane", expected_pane),
        ):
            value = fields.get(name)
            if (
                expected is not None
                and not cls._empty_report_field(value)
                and value != expected
            ):
                raise cls._invalid_worker_report()
        status_field = fields.get("status")
        status = (
            None
            if cls._empty_report_field(status_field)
            else str(status_field).strip().lower()
        )
        if status not in {None, "blocked", "completed"}:
            raise cls._negative_worker_report()
        success_values = {"pass", "passed", "通过", "成功"}
        if status == "completed":
            for name in ("checks", "review"):
                value = fields.get(name)
                if (
                    not cls._empty_report_field(value)
                    and str(value).strip().lower() not in success_values
                ):
                    raise cls._negative_worker_report()
            dirty_state = fields.get("dirty_state")
            if not cls._empty_report_field(dirty_state) and str(
                dirty_state
            ).strip().lower() not in {"clean", "干净"}:
                raise cls._negative_worker_report()
            blocker = fields.get("blocker")
            if (
                not cls._empty_report_field(blocker)
                and str(blocker).strip().lower() != "none"
            ):
                raise cls._negative_worker_report()
        if any(name not in fields for name in canonical_order):
            if status == "blocked":
                raise cls._negative_worker_report()
            raise cls._incomplete_worker_report()
        if tuple(order) != canonical_order:
            raise cls._negative_worker_report()
        if any(
            cls._empty_report_field(fields[name])
            for name in ("ticket", "status", "pane")
        ):
            raise cls._incomplete_worker_report()
        if status == "blocked":
            blocker = fields["blocker"]
            if any(
                cls._empty_report_field(fields[name])
                for name in ("checks", "review", "dirty_state", "blocker")
            ):
                raise cls._incomplete_worker_report()
            if (
                len(blocker) > 500
                or cls._contains_sensitive(blocker)
                or fields["commit"].strip().lower() != "none"
                or fields["checks"].strip().lower() != "blocked"
                or fields["review"].strip().lower() != "blocked"
                or fields["dirty_state"].strip().lower() != "clean"
                or fields["touched_files"].strip().lower() != "none"
            ):
                raise cls._negative_worker_report()
            return {
                "status": "blocked",
                "blocker": blocker,
                "reported_ticket": fields["ticket"],
                "reported_pane": fields["pane"],
            }
        if status != "completed":
            raise cls._negative_worker_report()
        required_nonempty = (
            "commit",
            "checks",
            "review",
            "dirty_state",
            "touched_files",
        )
        if any(cls._empty_report_field(fields[name]) for name in required_nonempty):
            raise cls._incomplete_worker_report()
        if (
            cls._empty_report_field(fields["blocker"])
            and fields["blocker"].strip().lower() != "none"
        ):
            raise cls._incomplete_worker_report()
        if (
            fields["checks"].strip().lower() not in success_values
            or fields["review"].strip().lower() not in success_values
            or fields["dirty_state"].strip().lower() not in {"clean", "干净"}
            or fields["blocker"].strip().lower() != "none"
        ):
            raise cls._negative_worker_report()
        return {
            "status": "confirmed",
            "reported_commit": fields["commit"].split(maxsplit=1)[0],
            "reported_ticket": fields["ticket"],
            "reported_pane": fields["pane"],
        }

    @staticmethod
    def _empty_report_field(value: str | None) -> bool:
        return not value or value.strip().lower() in {"none", "n/a", "unknown"}

    @staticmethod
    def _invalid_worker_report() -> CoordinatorRuntimeError:
        return CoordinatorRuntimeError(
            reason="worker_final_report_invalid",
            retryable=False,
            repair_required=True,
        )

    @staticmethod
    def _incomplete_worker_report() -> CoordinatorRuntimeError:
        return CoordinatorRuntimeError(
            reason="worker_final_report_incomplete",
            retryable=False,
        )

    @staticmethod
    def _negative_worker_report() -> CoordinatorRuntimeError:
        return CoordinatorRuntimeError(
            reason="worker_final_report_negative",
            retryable=False,
            repair_required=True,
        )

    @classmethod
    def _corroborate_worker_commit(
        cls,
        final_report: Mapping[str, Any],
        execution_commit: str,
    ) -> None:
        if final_report.get("status") != "confirmed":
            return
        reported = final_report.get("reported_commit")
        if (
            not isinstance(reported, str)
            or not re.fullmatch(r"[0-9a-f]{7,40}", reported)
            or not execution_commit.startswith(reported)
        ):
            raise cls._invalid_worker_report()

    @classmethod
    def _corroborate_worker_coordinates(
        cls,
        final_report: Mapping[str, Any],
        *,
        request: DeliveryRuntimeRequest,
        registry: DeliveryLaneRegistry,
    ) -> None:
        lane = request.lane
        expected_ticket = f"{lane.ticket_id} {lane.ticket_title} {lane.ticket_url}"
        expected_pane = (
            f"{registry.pane_id} {lane.execution_worktree} {lane.execution_branch}"
        )
        if (
            final_report.get("reported_ticket") != expected_ticket
            or final_report.get("reported_pane") != expected_pane
        ):
            raise cls._invalid_worker_report()

    @classmethod
    def _handle_failed_cherry_pick(
        cls,
        *,
        lane: DeliveryLaneSpec,
        expected_parent: str,
        cause: CoordinatorRuntimeError,
    ) -> NoReturn:
        current_head = cls._local_result(
            ["git", "-C", lane.integration_worktree, "rev-parse", "HEAD"]
        )
        try:
            cherry_pick_head = cls._local_result(
                [
                    "git",
                    "-C",
                    lane.integration_worktree,
                    "rev-parse",
                    "--verify",
                    "CHERRY_PICK_HEAD",
                ]
            )
        except CoordinatorRuntimeError:
            cherry_pick_head = None
        if current_head != expected_parent or cherry_pick_head is not None:
            raise CoordinatorRuntimeError(
                reason="overlapping_integration_ownership",
                retryable=False,
                repair_required=True,
            ) from cause
        raise CoordinatorRuntimeError(
            reason="integration_cherry_pick_failed",
            retryable=False,
            repair_required=True,
        ) from cause

    @classmethod
    def delivery_agent_name(
        cls, lifecycle_id: str, dispatch_id: str, worker_kind: str
    ) -> str:
        return (
            f"mapgov_{worker_kind}_{cls._digest(lifecycle_id + ':' + dispatch_id, 16)}"
        )

    def _delivery_runtime_record(
        self, request: DeliveryRuntimeRequest
    ) -> dict[str, Any]:
        record = self._storage.pm_runtime(request.map_id)
        if record is None or record.get("state") != "active":
            raise CoordinatorRuntimeError(
                reason="pm_runtime_not_active",
                retryable=True,
            )
        expected = {
            "project_id": request.context.project_id,
            "project_url": request.context.project_url,
            "repository": request.context.repository,
            "repository_path": request.context.repository_path,
            "pm_profile": request.context.pm_profile,
            "herdr_executable": request.context.herdr_executable,
        }
        if any(
            str(record.get(name) or "") != value for name, value in expected.items()
        ):
            raise CoordinatorRuntimeError(
                reason="delivery_context_mismatch",
                retryable=False,
                repair_required=True,
            )
        return record

    def _delivery_pane(
        self,
        *,
        prefix: Sequence[str],
        record: Mapping[str, Any],
        execution_worktree: str,
    ) -> Mapping[str, Any]:
        panes_result = self._result(self._command_json([*prefix, "pane", "list"]))
        panes = panes_result.get("panes")
        if panes_result.get("type") != "pane_list" or not isinstance(panes, list):
            raise self._malformed("partial_coordinates")
        if any(
            not isinstance(pane, Mapping)
            or any(
                not isinstance(pane.get(field), str) or not pane.get(field)
                for field in ("workspace_id", "tab_id", "pane_id", "cwd")
            )
            for pane in panes
        ):
            raise self._malformed("partial_coordinates")
        cwd_panes = [pane for pane in panes if pane.get("cwd") == execution_worktree]
        if any(
            pane.get("workspace_id") != record["workspace_id"]
            or pane.get("tab_id") != record["window_id"]
            for pane in cwd_panes
        ):
            raise CoordinatorRuntimeError(
                reason="overlapping_lane_ownership",
                retryable=False,
                repair_required=True,
            )
        matching = [
            pane
            for pane in cwd_panes
            if pane.get("workspace_id") == record["workspace_id"]
            and pane.get("tab_id") == record["window_id"]
        ]
        if len(matching) > 1:
            raise CoordinatorRuntimeError(
                reason="overlapping_lane_ownership",
                retryable=False,
                repair_required=True,
            )
        if matching:
            return matching[0]
        result = self._result(
            self._command_json(
                [
                    *prefix,
                    "pane",
                    "split",
                    "--pane",
                    str(record["pane_id"]),
                    "--direction",
                    "right",
                    "--cwd",
                    execution_worktree,
                    "--no-focus",
                ]
            )
        )
        pane = result.get("pane")
        if result.get("type") != "pane_info" or not isinstance(pane, Mapping):
            raise self._malformed("partial_coordinates")
        if (
            pane.get("workspace_id") != record["workspace_id"]
            or pane.get("tab_id") != record["window_id"]
            or pane.get("cwd") != execution_worktree
            or not pane.get("pane_id")
        ):
            raise CoordinatorRuntimeError(
                reason="opaque_coordinate_mismatch",
                retryable=False,
                repair_required=True,
            )
        return pane

    def _registered_delivery_pane(
        self,
        *,
        prefix: Sequence[str],
        registry: DeliveryLaneRegistry,
    ) -> Mapping[str, Any]:
        pane = self._registered_delivery_pane_or_none(
            prefix=prefix,
            registry=registry,
        )
        if pane is None:
            raise CoordinatorRuntimeError(
                reason="lane_registry_readback_mismatch",
                retryable=False,
                repair_required=True,
            )
        return pane

    def _registered_delivery_pane_or_none(
        self,
        *,
        prefix: Sequence[str],
        registry: DeliveryLaneRegistry,
    ) -> Mapping[str, Any] | None:
        panes_result = self._result(self._command_json([*prefix, "pane", "list"]))
        panes = panes_result.get("panes")
        if panes_result.get("type") != "pane_list" or not isinstance(panes, list):
            raise self._malformed("partial_coordinates")
        if any(
            not isinstance(pane, Mapping)
            or any(
                not isinstance(pane.get(field), str) or not pane.get(field)
                for field in ("workspace_id", "tab_id", "pane_id", "cwd")
            )
            for pane in panes
        ):
            raise self._malformed("partial_coordinates")
        matching = [pane for pane in panes if pane.get("pane_id") == registry.pane_id]
        if any(
            pane.get("cwd") == registry.worktree
            and pane.get("pane_id") != registry.pane_id
            for pane in panes
        ):
            raise CoordinatorRuntimeError(
                reason="overlapping_lane_ownership",
                retryable=False,
                repair_required=True,
            )
        if not matching:
            return None
        if len(matching) != 1 or any(
            matching[0].get(name) != value
            for name, value in {
                "workspace_id": registry.workspace_id,
                "tab_id": registry.tab_id,
                "cwd": registry.worktree,
            }.items()
        ):
            raise CoordinatorRuntimeError(
                reason="lane_registry_readback_mismatch",
                retryable=False,
                repair_required=True,
            )
        return matching[0]

    def _delivery_pane_occupants(
        self,
        *,
        prefix: Sequence[str],
        pane: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        agents_result = self._result(self._command_json([*prefix, "agent", "list"]))
        agents = agents_result.get("agents")
        if agents_result.get("type") != "agent_list" or not isinstance(agents, list):
            raise self._malformed("partial_coordinates")
        if any(
            not isinstance(agent, Mapping)
            or any(
                not isinstance(agent.get(field), str) or not agent.get(field)
                for field in (
                    "name",
                    "agent",
                    "agent_status",
                    "workspace_id",
                    "tab_id",
                    "pane_id",
                    "cwd",
                )
            )
            for agent in agents
        ):
            raise self._malformed("partial_coordinates")
        return [
            agent for agent in agents if agent.get("pane_id") == pane.get("pane_id")
        ]

    @staticmethod
    def _validate_worker_agent(
        agent: Mapping[str, Any],
        *,
        worker_name: str,
        worker_kind: str,
        workspace_id: str,
        window_id: str,
        pane_id: str,
        execution_worktree: str,
        expected_status: str | None = None,
    ) -> None:
        expected = {
            "name": worker_name,
            "agent": worker_kind,
            "workspace_id": workspace_id,
            "tab_id": window_id,
            "pane_id": pane_id,
            "cwd": execution_worktree,
        }
        if any(agent.get(name) != value for name, value in expected.items()):
            raise CoordinatorRuntimeError(
                reason="worker_coordinate_mismatch",
                retryable=False,
                repair_required=True,
            )
        if expected_status is not None and agent.get("agent_status") != expected_status:
            raise CoordinatorRuntimeError(
                reason="worker_handoff_unconfirmed",
                retryable=True,
            )

    @staticmethod
    def _local_result(arguments: Sequence[str], *, cwd: str | None = None) -> str:
        try:
            completed = subprocess.run(
                [str(item) for item in arguments],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=35,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CoordinatorRuntimeError(
                reason="local_evidence_unavailable", retryable=True
            ) from error
        if completed.returncode != 0:
            raise CoordinatorRuntimeError(
                reason="local_evidence_unavailable", retryable=True
            )
        return completed.stdout.strip()

    @classmethod
    def _validate_delivery_git(
        cls,
        request: DeliveryRuntimeRequest,
        *,
        collecting: bool,
        allow_active_execution: bool = False,
    ) -> None:
        lane = request.lane
        cls._validate_integration_frontier(request)
        integration = str(Path(lane.integration_worktree).resolve())
        execution = str(Path(lane.execution_worktree).resolve())
        repository = str(Path(request.context.repository_path).resolve())
        common_directories = {
            cls._local_result(
                [
                    "git",
                    "-C",
                    path,
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-common-dir",
                ]
            )
            for path in (repository, integration, execution)
        }
        if len(common_directories) != 1:
            raise CoordinatorRuntimeError(
                reason="worktree_repository_mismatch",
                retryable=False,
                repair_required=True,
            )
        registered = {
            str(Path(line.removeprefix("worktree ")).resolve())
            for line in cls._local_result(
                ["git", "-C", repository, "worktree", "list", "--porcelain"]
            ).splitlines()
            if line.startswith("worktree ")
        }
        if not {integration, execution}.issubset(registered):
            raise CoordinatorRuntimeError(
                reason="worktree_registration_mismatch",
                retryable=False,
                repair_required=True,
            )
        expected_branches = (
            (integration, lane.integration_branch),
            (execution, lane.execution_branch),
        )
        for path, branch in expected_branches:
            if (
                cls._local_result(["git", "-C", path, "branch", "--show-current"])
                != branch
            ):
                raise CoordinatorRuntimeError(
                    reason="worktree_branch_mismatch",
                    retryable=False,
                    repair_required=True,
                )
            if not (allow_active_execution and path == execution) and cls._local_result(
                ["git", "-C", path, "status", "--porcelain=v1"]
            ):
                raise CoordinatorRuntimeError(
                    reason="worktree_not_clean",
                    retryable=False,
                    repair_required=True,
                )
        if not collecting:
            expected_heads = (
                (integration, request.integration_expected_head or lane.base_commit),
                (execution, lane.base_commit),
            )
            for path, expected_head in expected_heads:
                actual_head = cls._local_result(
                    ["git", "-C", path, "rev-parse", "HEAD"]
                )
                if actual_head != expected_head:
                    if path == integration:
                        cls._raise_integration_head_mismatch(
                            request,
                            actual_head=actual_head,
                            fallback_reason="delivery_base_mismatch",
                        )
                    raise CoordinatorRuntimeError(
                        reason="delivery_base_mismatch",
                        retryable=False,
                        repair_required=True,
                    )

    @classmethod
    def _raise_integration_head_mismatch(
        cls,
        request: DeliveryRuntimeRequest,
        *,
        actual_head: str,
        fallback_reason: str,
    ) -> NoReturn:
        """Distinguish one serialized integration awaiting tracker confirmation."""
        lane = request.lane
        expected_head = request.integration_expected_head or lane.base_commit
        unconfirmed_predecessors = (
            lane.integration_order - 1 - len(request.integration_predecessor_commits)
        )
        if unconfirmed_predecessors > 0:
            try:
                merge_base = cls._local_result(
                    [
                        "git",
                        "-C",
                        lane.integration_worktree,
                        "merge-base",
                        expected_head,
                        actual_head,
                    ]
                )
                commit_count = cls._local_result(
                    [
                        "git",
                        "-C",
                        lane.integration_worktree,
                        "rev-list",
                        "--count",
                        f"{expected_head}..{actual_head}",
                    ]
                )
            except CoordinatorRuntimeError:
                pass
            else:
                if merge_base == expected_head and commit_count == "1":
                    raise CoordinatorRuntimeError(
                        reason="integration_frontier_pending",
                        retryable=True,
                    )
        raise CoordinatorRuntimeError(
            reason=fallback_reason,
            retryable=False,
            repair_required=True,
        )

    @classmethod
    def _validate_integration_frontier(
        cls,
        request: DeliveryRuntimeRequest,
    ) -> None:
        """Prove every tracker-confirmed predecessor commit is one linear chain."""
        lane = request.lane
        expected_head = request.integration_expected_head or lane.base_commit
        commits = request.integration_predecessor_commits
        if (
            len(commits) > lane.integration_order - 1
            or any(re.fullmatch(r"[0-9a-f]{40}", commit) is None for commit in commits)
            or re.fullmatch(r"[0-9a-f]{40}", expected_head) is None
            or (commits and commits[-1] != expected_head)
            or (not commits and expected_head != lane.base_commit)
        ):
            raise CoordinatorRuntimeError(
                reason="integration_order_not_ready",
                retryable=True,
            )
        previous = lane.base_commit
        for commit in commits:
            try:
                merge_base = cls._local_result(
                    [
                        "git",
                        "-C",
                        lane.integration_worktree,
                        "merge-base",
                        previous,
                        commit,
                    ]
                )
                commit_count = cls._local_result(
                    [
                        "git",
                        "-C",
                        lane.integration_worktree,
                        "rev-list",
                        "--count",
                        f"{previous}..{commit}",
                    ]
                )
            except CoordinatorRuntimeError as error:
                raise CoordinatorRuntimeError(
                    reason="integration_order_not_ready",
                    retryable=True,
                ) from error
            if merge_base != previous or commit_count != "1":
                raise CoordinatorRuntimeError(
                    reason="integration_order_not_ready",
                    retryable=True,
                )
            previous = commit

    @classmethod
    def _validate_delivery_request(cls, request: DeliveryRuntimeRequest) -> None:
        if not isinstance(request, DeliveryRuntimeRequest):
            raise TypeError("Delivery runtime request is required")
        lane = request.lane
        required_text = (
            request.map_id,
            request.map_url,
            lane.lane_id,
            lane.ticket_id,
            lane.ticket_title,
            lane.ticket_url,
            lane.parent_spec_url,
            lane.integration_worktree,
            lane.integration_branch,
            lane.execution_worktree,
            lane.execution_branch,
            lane.base_commit,
            lane.owner_skill_path,
        )
        if any(
            not isinstance(value, str) or not value.strip() for value in required_text
        ):
            raise ValueError("Delivery lane has an empty required field")
        if lane.protocol != "delivery-pipeline/herdr-implementation-v1":
            raise ValueError("Delivery lane protocol is not supported")
        if lane.ticket_url == request.map_url:
            raise ValueError("Delivery ticket must be distinct from the Map Issue")
        repository_issue_prefix = (
            f"https://github.com/{request.context.repository}/issues/"
        )
        map_match = re.fullmatch(
            re.escape(repository_issue_prefix) + r"([1-9][0-9]*)", request.map_url
        )
        ticket_match = re.fullmatch(
            re.escape(repository_issue_prefix) + r"([1-9][0-9]*)", lane.ticket_url
        )
        spec_match = re.fullmatch(
            re.escape(repository_issue_prefix) + r"([1-9][0-9]*)",
            lane.parent_spec_url,
        )
        if map_match is None or ticket_match is None:
            raise ValueError("Delivery ticket is outside the Map repository")
        if spec_match is None or lane.parent_spec_url in {
            request.map_url,
            lane.ticket_url,
        }:
            raise ValueError("Delivery parent Spec relationship is invalid")
        if lane.completion_contract != "one-local-commit-integrated-and-validated":
            raise ValueError("Delivery completion contract is not supported")
        if (
            isinstance(lane.integration_order, bool)
            or not isinstance(lane.integration_order, int)
            or isinstance(lane.integration_total, bool)
            or not isinstance(lane.integration_total, int)
            or lane.integration_order < 1
            or lane.integration_total < lane.integration_order
            or lane.integration_total > 1000
            or len(lane.integration_predecessor_ticket_urls)
            != lane.integration_order - 1
            or len(set(lane.integration_predecessor_ticket_urls))
            != len(lane.integration_predecessor_ticket_urls)
            or lane.ticket_url in lane.integration_predecessor_ticket_urls
        ):
            raise ValueError("Delivery integration order is invalid")
        if any(
            re.fullmatch(
                re.escape(repository_issue_prefix) + r"[1-9][0-9]*",
                predecessor_url,
            )
            is None
            for predecessor_url in lane.integration_predecessor_ticket_urls
        ):
            raise ValueError("Delivery integration predecessor is invalid")
        if (
            not isinstance(request.integration_predecessor_commits, tuple)
            or len(request.integration_predecessor_commits)
            > len(lane.integration_predecessor_ticket_urls)
            or any(
                not isinstance(commit, str)
                or re.fullmatch(r"[0-9a-f]{40}", commit) is None
                for commit in request.integration_predecessor_commits
            )
            or (
                request.integration_expected_head is not None
                and (
                    not isinstance(request.integration_expected_head, str)
                    or re.fullmatch(r"[0-9a-f]{40}", request.integration_expected_head)
                    is None
                )
            )
        ):
            raise ValueError("Delivery integration frontier is invalid")
        if (
            lane.owner_skill_name != "implement"
            or lane.owner_invocation_label != "$implement"
        ):
            raise ValueError("Delivery lane must declare the implement owner")
        owner = Path(lane.owner_skill_path)
        configured_owner = request.context.implement_skill_path
        resolved_owner = owner.resolve()
        if (
            not owner.is_absolute()
            or not owner.is_file()
            or owner.is_symlink()
            or str(owner) != str(resolved_owner)
            or (
                configured_owner is not None
                and lane.owner_skill_path != str(Path(configured_owner).resolve())
            )
        ):
            raise ValueError("Delivery lane implement owner path is unavailable")
        try:
            owner_text = owner.read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError(
                "Delivery lane implement owner path is unavailable"
            ) from error
        if not re.search(r"(?m)^name:\s*[\"']?implement[\"']?\s*$", owner_text):
            raise ValueError("Delivery lane owner frontmatter does not match implement")
        if (
            request.registry is None
            and lane.worker_kind not in request.context.supported_worker_kinds
        ):
            raise CommissioningPrerequisiteError(
                reason="supported_worker_integration_missing",
                failed_checks=(f"herdr.integration.{lane.worker_kind}",),
            )
        ticket_number = ticket_match.group(1)
        map_number = map_match.group(1)
        if lane.lane_id != f"implementation-{ticket_number}":
            raise ValueError("Delivery lane identity does not match the ticket")
        if lane.execution_branch != f"{lane.worker_kind}/issue-{ticket_number}":
            raise ValueError("Delivery execution branch does not match the ticket")
        if lane.integration_branch != f"feature/map-{map_number}":
            raise ValueError("Delivery integration branch does not match the Map")
        if not re.fullmatch(r"[0-9a-f]{40}", lane.base_commit):
            raise ValueError("Delivery base commit must be a full Git SHA")
        paths = (Path(lane.integration_worktree), Path(lane.execution_worktree))
        if any(
            not path.is_absolute()
            or not path.is_dir()
            or path.is_symlink()
            or str(path) != str(path.resolve())
            for path in paths
        ):
            raise ValueError("Delivery worktrees must be existing absolute directories")
        if paths[0].resolve() == paths[1].resolve():
            raise ValueError("Integration and execution worktrees must be distinct")
        if (
            not isinstance(lane.validation_argv, tuple)
            or not lane.validation_argv
            or len(lane.validation_argv) > 64
            or any(
                not isinstance(item, str) or not item or "\x00" in item or "\n" in item
                for item in lane.validation_argv
            )
        ):
            raise ValueError("Delivery validation must be one fixed argv command")
        if any(
            not isinstance(item, str) or not item.strip() or len(item) > 1000
            for item in lane.known_limitations
        ):
            raise ValueError("Delivery limitations must be concise non-empty strings")
        executable = lane.validation_argv[0]
        allowed_executables = {
            "cargo",
            "git",
            "go",
            "node",
            "npm",
            "pytest",
            "python",
            "python3",
            "ruff",
            "swift",
            "ty",
            "xcodebuild",
        }
        if (
            executable not in allowed_executables
            or Path(executable).name != executable
            or shutil.which(executable) is None
        ):
            raise ValueError("Delivery validation command is not safely bounded")
        command = lane.validation_argv[1] if len(lane.validation_argv) > 1 else ""
        unsafe_arguments = {
            "--fix",
            "--unsafe-fixes",
            "--write",
            "-w",
            "--ext-diff",
            "--no-index",
        }
        if any(
            item in unsafe_arguments
            or item.startswith("--fix")
            or item.startswith("--unsafe-fixes")
            or item.startswith("--output")
            or Path(item).is_absolute()
            or ".." in Path(item).parts
            for item in lane.validation_argv[1:]
        ):
            raise ValueError("Delivery validation command is not safely bounded")
        if executable == "git" and command not in {
            "diff",
            "status",
            "rev-parse",
            "show",
            "log",
            "grep",
        }:
            raise ValueError(
                "Delivery validation cannot perform remote or merge actions"
            )
        if executable in {"python", "python3"}:
            if len(lane.validation_argv) < 3 or lane.validation_argv[1] != "-m":
                raise ValueError("Delivery Python validation is not safely bounded")
            if lane.validation_argv[2] not in {"compileall", "pytest", "unittest"}:
                raise ValueError("Delivery Python validation is not safely bounded")
        if executable == "node" and command not in {"--check", "--test"}:
            raise ValueError("Delivery Node validation is not safely bounded")
        if executable == "npm" and command != "test":
            raise ValueError("Delivery npm validation is not safely bounded")
        safe_subcommands = {
            "cargo": {"check", "test"},
            "go": {"test"},
            "ruff": {"check"},
            "swift": {"build", "test"},
            "ty": {"check"},
            "xcodebuild": {"build", "test"},
        }
        if (
            executable in safe_subcommands
            and command not in safe_subcommands[executable]
        ):
            raise ValueError("Delivery validation command is not safely bounded")
        packet = delivery_lane_packet(request)
        serialized = json.dumps(packet, ensure_ascii=False, sort_keys=True)
        if len(serialized.encode()) > 32_768 or cls._contains_sensitive(packet):
            raise ValueError("Delivery lane packet is too large or sensitive")

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
        missing_reason: str | None = None,
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
        if not matching and missing_reason is not None:
            raise CoordinatorRuntimeError(
                reason=missing_reason,
                retryable=True,
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
        if not isinstance(workspaces, list):
            raise self._malformed("partial_coordinates")
        registered_workspaces = [
            item
            for item in workspaces
            if isinstance(item, Mapping)
            and item.get("workspace_id") == record["workspace_id"]
        ]
        if not registered_workspaces:
            raise CoordinatorRuntimeError(
                reason="owned_workspace_unavailable",
                retryable=True,
            )
        if len(registered_workspaces) != 1 or (
            registered_workspaces[0].get("label") != record["workspace_label"]
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
            missing_reason="owned_pane_unavailable",
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
            try:
                failure = json.loads(result.stdout)
            except (TypeError, ValueError):
                failure = None
            error = failure.get("error") if isinstance(failure, Mapping) else None
            code = error.get("code") if isinstance(error, Mapping) else None
            transient_reasons = {
                "capacity_saturated": "provider_capacity_saturated",
                "provider_capacity_saturated": "provider_capacity_saturated",
                "provider_rate_limited": "provider_rate_limited",
            }
            if (
                isinstance(error, Mapping)
                and code in transient_reasons
                and error.get("retryable") is True
            ):
                raise CoordinatorRuntimeError(
                    reason=transient_reasons[str(code)],
                    retryable=True,
                )
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
        self._validate_recorded_ownership_identity(record)
        opaque_coordinates = (
            "workspace_id",
            "window_id",
            "pane_id",
            "agent_session_id",
        )
        if any(not record.get(name) for name in opaque_coordinates):
            raise CoordinatorRuntimeError(
                reason="partial_coordinates",
                retryable=False,
                repair_required=True,
            )

    def _validate_recorded_ownership_identity(
        self,
        record: Mapping[str, Any],
    ) -> None:
        stable_identity = (
            "map_id",
            "session_namespace",
            "workspace_label",
            "agent_id",
            "ownership_marker",
            "lifecycle_id",
        )
        if any(not record.get(name) for name in stable_identity):
            raise CoordinatorRuntimeError(
                reason="runtime_ownership_identity_missing",
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
