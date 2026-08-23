from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from map_governance import GovernanceRequestIdentity, MapGovernanceApplication
from map_governance.coordinator import (
    CommissioningContext,
    CommissioningPrerequisiteError,
    CoordinatorCommandResult,
    CoordinatorRuntime,
    CoordinatorRuntimeError,
    DeliveryLaneRegistry,
    DeliveryLaneSpec,
    DeliveryRuntimeRequest,
    delivery_dispatch_id,
    delivery_worker_prompt,
)
from map_governance.storage import PluginStorage
from map_governance.reports import PMReport, PMReportDraft, TrackerPMReportRecord
from map_governance.tracker import (
    TrackerDeliveryLaneRegistryRecord,
    TrackerError,
    TrackerIssue,
    TrackerProject,
)


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MAP_ID = "I_atlas_41"
ISSUE_URL = "https://github.com/acme/atlas/issues/41"
TICKET_URL = "https://github.com/acme/atlas/issues/42"
SPEC_URL = "https://github.com/acme/atlas/issues/40"
PROJECT_URL = "https://github.com/orgs/acme/projects/7"
PM_IDENTITY = GovernanceRequestIdentity("pm", "pm-session-atlas")


def _worker_final_report(
    *,
    worktree: str,
    commit: str = "none",
    status: str = "completed",
    checks: str = "passed",
    review: str = "passed",
    dirty_state: str = "clean",
    touched_files: str = "delivery.txt",
    blocker: str = "none",
    ticket: str = (
        "I_atlas_42 Add the deterministic delivery proof "
        "https://github.com/acme/atlas/issues/42"
    ),
    pane_id: str = "wA:p2",
    branch: str = "codex/issue-42",
) -> str:
    return (
        "FINAL_REPORT_BEGIN\n"
        f"Ticket：{ticket}\n"
        f"状态：{status}\n"
        f"Pane/worktree/branch：{pane_id} {worktree} {branch}\n"
        f"Commit：{commit}\n"
        f"Checks：{checks}\n"
        f"Review：{review}\n"
        f"Dirty state：{dirty_state}\n"
        f"Touched files：{touched_files}\n"
        f"Blocker：{blocker}\n"
        "FINAL_REPORT_END\n"
    )


class DeliveryTracker:
    def __init__(self) -> None:
        self.issue = TrackerIssue(
            id=MAP_ID,
            repository="acme/atlas",
            number=41,
            title="Map the Atlas launch",
            url=ISSUE_URL,
            state="open",
            state_reason=None,
            labels=("map", "map-stage/delivery"),
        )
        self.reports: list[TrackerPMReportRecord] = []
        self.ticket = TrackerIssue(
            id="I_atlas_42",
            repository="acme/atlas",
            number=42,
            title="Add the deterministic delivery proof",
            url=TICKET_URL,
            state="open",
            state_reason=None,
            labels=("implementation",),
            body=(
                f"## Parent\n\n{SPEC_URL}\n\n"
                "## Blocked by\n\nNone - can start immediately\n"
            ),
        )
        self.registries: list[TrackerDeliveryLaneRegistryRecord] = []
        self.dependencies: dict[str, TrackerIssue] = {}
        self.spec = TrackerIssue(
            id="I_atlas_40",
            repository="acme/atlas",
            number=40,
            title="Atlas delivery Spec",
            url=SPEC_URL,
            state="open",
            state_reason=None,
            labels=("spec",),
        )
        self.fail_transitions = False

    def get_project(self, url: str) -> TrackerProject:
        return TrackerProject(
            id="PVT_acme_7",
            owner="acme",
            owner_type="organization",
            number=7,
            title="Acme CEO portfolio",
            url=url,
        )

    def get_issue(self, url: str) -> TrackerIssue:
        if url == ISSUE_URL:
            return self.issue
        if url == SPEC_URL:
            return self.spec
        if url in self.dependencies:
            return self.dependencies[url]
        assert url == TICKET_URL
        return self.ticket

    def list_decisions(self, _url: str) -> list:
        return []

    def list_pm_reports(self, _url: str) -> list:
        return list(self.reports)

    def append_pm_report(
        self,
        url: str,
        *,
        issue_id: str,
        report: PMReport,
    ) -> TrackerPMReportRecord:
        assert url == ISSUE_URL
        assert issue_id == MAP_ID
        record = TrackerPMReportRecord(
            report=report,
            tracker_record_id=f"IC_{report.content.record_id}",
            tracker_record_url=f"{ISSUE_URL}#issuecomment-{report.content.record_id}",
        )
        self.reports.append(record)
        return record

    def list_delivery_lane_registries(self, url: str):
        assert url == TICKET_URL
        return list(self.registries)

    def append_delivery_lane_registry(self, url: str, *, issue_id: str, registry):
        assert url == TICKET_URL
        assert issue_id == self.ticket.id
        record = TrackerDeliveryLaneRegistryRecord(
            registry=registry,
            tracker_record_id=f"IC_registry_{len(self.registries) + 1}",
            tracker_record_url=f"{TICKET_URL}#issuecomment-registry-{len(self.registries) + 1}",
        )
        self.registries.append(record)
        return record

    def transition_issue_stage(
        self,
        url: str,
        *,
        expected_stage: str,
        requested_stage: str,
    ) -> TrackerIssue:
        assert url == ISSUE_URL
        assert expected_stage == "delivery"
        assert requested_stage in {"acceptance", "decision"}
        if self.fail_transitions:
            raise TrackerError("simulated delivery stage transition failure")
        self.issue = replace(
            self.issue,
            labels=("map", f"map-stage/{requested_stage}"),
        )
        return self.issue


class StaticDeliveryPrerequisites:
    def __init__(self, context: CommissioningContext) -> None:
        self.context = context

    def commissioning_context(self, *, project_id: str, repository: str):
        assert project_id == self.context.project_id
        assert repository == self.context.repository
        return self.context


def _registry_for_request(
    request: DeliveryRuntimeRequest,
    *,
    state: str,
    head_commit: str | None = None,
    integrated_commit: str | None = None,
) -> DeliveryLaneRegistry:
    terminal = state in {"terminal", "integrated"}
    return DeliveryLaneRegistry(
        work_item=request.lane.ticket_url,
        role="implementation",
        lane_id=request.lane.lane_id,
        runtime=f"herdr-{request.lane.worker_kind}-pane",
        state=state,
        workspace_id="wA",
        tab_id="wA:t1",
        pane_id="wA:p2",
        herdr_session_name="mapgov-test",
        herdr_session_owned=True,
        bootstrap_authority="none",
        agent_permission_mode="default",
        worktree=request.lane.execution_worktree,
        branch=request.lane.execution_branch,
        base_commit=request.lane.base_commit,
        head_commit=head_commit,
        integrated_commit=integrated_commit,
        updated_at=request.registry_timestamp,
        evidence_source="herdr-final-report" if terminal else None,
        final_report_digest="sha256:" + "d" * 64 if terminal else None,
    )


class DeliveryRuntimeProbe:
    def __init__(self, *, recovered: bool = False) -> None:
        self.requests: list[DeliveryRuntimeRequest] = []
        self.recovered = recovered

    def status(self, *, map_id: str):
        return {"map_id": map_id, "state": "active", "ready_record_id": "ready-1"}

    def prepare_lane(self, request: DeliveryRuntimeRequest):
        self.requests.append(request)
        registry = _registry_for_request(request, state="created")
        return {
            "map_id": request.map_id,
            "state": "prepared",
            "dispatch_id": delivery_dispatch_id(request),
            "worker_kind": request.lane.worker_kind,
            "registry": registry.payload(),
            "remote_actions": "forbidden",
        }

    def dispatch_lane(self, request: DeliveryRuntimeRequest):
        self.requests.append(request)
        assert request.registry is not None
        registry = _registry_for_request(request, state="running")
        return {
            "map_id": request.map_id,
            "state": "dispatched",
            "dispatch_id": delivery_dispatch_id(request),
            "worker_kind": request.lane.worker_kind,
            "completion_contract": request.lane.completion_contract,
            "registry": registry.payload(),
            "remote_actions": "forbidden",
        }

    def collect_lane(self, request: DeliveryRuntimeRequest):
        self.requests.append(request)
        terminal = _registry_for_request(
            request,
            state="terminal",
            head_commit="b" * 40,
        )
        integrated = _registry_for_request(
            request,
            state="integrated",
            head_commit="b" * 40,
            integrated_commit="c" * 40,
        )
        if self.recovered:
            terminal = replace(
                terminal,
                evidence_source="registry-git-recovery",
                final_report_digest=None,
            )
            integrated = replace(
                integrated,
                evidence_source="registry-git-recovery",
                final_report_digest=None,
            )
        return {
            "map_id": request.map_id,
            "state": "locally_validated",
            "dispatch_id": delivery_dispatch_id(request),
            "worker_kind": request.lane.worker_kind,
            "terminal_registry": terminal.payload(),
            "integrated_registry": integrated.payload(),
            "evidence": {
                "execution_commit": "b" * 40,
                "integration_commit": "c" * 40,
                "final_report": {
                    "status": ("recovered_from_git" if self.recovered else "confirmed"),
                    "digest": None if self.recovered else "sha256:" + "d" * 64,
                },
                "validation": "passed",
                "completion_contract": "satisfied",
            },
            "limitations": [
                *request.lane.known_limitations,
                *(
                    [
                        "The terminal Herdr transport cache was unavailable; tracker registry and Git evidence were used."
                    ]
                    if self.recovered
                    else []
                ),
                "Push, PR, merge, release, and Issue closure were not performed.",
            ],
            "acceptance_recommendation": "accept",
            "remote_actions": "forbidden",
            "idempotent": False,
        }


class MissingWorkerRuntime(DeliveryRuntimeProbe):
    def prepare_lane(self, request: DeliveryRuntimeRequest):
        self.requests.append(request)
        raise CommissioningPrerequisiteError(
            reason="supported_worker_integration_missing",
            failed_checks=("herdr.integration.codex",),
        )


class MissingLimitationsRecoveryRuntime(DeliveryRuntimeProbe):
    def __init__(self) -> None:
        super().__init__(recovered=True)

    def collect_lane(self, request: DeliveryRuntimeRequest):
        outcome = super().collect_lane(request)
        return {**outcome, "limitations": []}


class WorkerBlockedRuntime(DeliveryRuntimeProbe):
    def __init__(self) -> None:
        super().__init__()
        self.blocked = True
        self.blocker_summary = "dependency unavailable"

    def collect_lane(self, request: DeliveryRuntimeRequest):
        if not self.blocked:
            return super().collect_lane(request)
        self.requests.append(request)
        blocked = replace(
            _registry_for_request(request, state="blocked"),
            evidence_source="herdr-final-report",
            final_report_digest=(
                "sha256:" + hashlib.sha256(self.blocker_summary.encode()).hexdigest()
            ),
            blocker_summary=self.blocker_summary,
        )
        return {
            "map_id": request.map_id,
            "state": "blocked",
            "dispatch_id": delivery_dispatch_id(request),
            "worker_kind": request.lane.worker_kind,
            "blocked_registry": blocked.payload(),
            "blocker": {
                "reason": "worker_reported_blocker",
                "retryable": True,
                "summary": self.blocker_summary,
            },
            "remote_actions": "forbidden",
        }


class ForgedDispatchRuntime(DeliveryRuntimeProbe):
    def dispatch_lane(self, request: DeliveryRuntimeRequest):
        outcome = super().dispatch_lane(request)
        return {**outcome, "dispatch_id": "delivery-dispatch:forged"}


class ForgedDispatchRegistryRuntime(DeliveryRuntimeProbe):
    def dispatch_lane(self, request: DeliveryRuntimeRequest):
        outcome = super().dispatch_lane(request)
        return {
            **outcome,
            "registry": {**outcome["registry"], "pane_id": "wA:p99"},
        }


class ForgedPrepareRuntime(DeliveryRuntimeProbe):
    def prepare_lane(self, request: DeliveryRuntimeRequest):
        outcome = super().prepare_lane(request)
        return {**outcome, "map_id": "I_other_99"}


class ForgedAcceptanceRuntime(DeliveryRuntimeProbe):
    def collect_lane(self, request: DeliveryRuntimeRequest):
        outcome = super().collect_lane(request)
        return {
            **outcome,
            "evidence": {**outcome["evidence"], "validation": "failed"},
        }


class ForgedBlockedRegistryRuntime(WorkerBlockedRuntime):
    def collect_lane(self, request: DeliveryRuntimeRequest):
        outcome = super().collect_lane(request)
        return {
            **outcome,
            "blocked_registry": {
                **outcome["blocked_registry"],
                "bootstrap_authority": "untrusted_worker_claim",
            },
        }


class ForgedBlockedOutcomeRuntime(WorkerBlockedRuntime):
    def collect_lane(self, request: DeliveryRuntimeRequest):
        outcome = super().collect_lane(request)
        return {**outcome, "remote_actions": "allowed"}


class ForgedAcceptanceRegistryRuntime(DeliveryRuntimeProbe):
    def collect_lane(self, request: DeliveryRuntimeRequest):
        outcome = super().collect_lane(request)
        return {
            **outcome,
            "integrated_registry": {
                **outcome["integrated_registry"],
                "final_report_digest": None,
            },
        }


class ForgedAcceptanceDigestRuntime(DeliveryRuntimeProbe):
    def collect_lane(self, request: DeliveryRuntimeRequest):
        outcome = super().collect_lane(request)
        forged_digest = "sha256:" + "e" * 64
        return {
            **outcome,
            "terminal_registry": {
                **outcome["terminal_registry"],
                "final_report_digest": forged_digest,
            },
            "integrated_registry": {
                **outcome["integrated_registry"],
                "final_report_digest": forged_digest,
            },
        }


class MismatchedIdempotentRegistryRuntime(DeliveryRuntimeProbe):
    def __init__(self) -> None:
        super().__init__()
        self.collect_count = 0

    def collect_lane(self, request: DeliveryRuntimeRequest):
        self.collect_count += 1
        outcome = super().collect_lane(request)
        if self.collect_count == 1:
            return outcome
        changed_at = "2026-08-24T00:00:59Z"
        return {
            **outcome,
            "terminal_registry": {
                **outcome["terminal_registry"],
                "updated_at": changed_at,
            },
            "integrated_registry": {
                **outcome["integrated_registry"],
                "updated_at": changed_at,
            },
        }


class FailedDeliveryPrerequisites:
    def commissioning_context(self, *, project_id: str, repository: str):
        raise CommissioningPrerequisiteError(
            reason="github_authority_unavailable",
            failed_checks=("github.project.read",),
        )


def _lane(tmp_path: Path) -> DeliveryLaneSpec:
    integration = tmp_path / "atlas-map-1"
    execution = tmp_path / "atlas-map-1-issue-41"
    integration.mkdir()
    execution.mkdir()
    owner = tmp_path / "implement" / "SKILL.md"
    owner.parent.mkdir()
    owner.write_text("---\nname: implement\n---\n", encoding="utf-8")
    return DeliveryLaneSpec(
        protocol="delivery-pipeline/herdr-implementation-v1",
        lane_id="implementation-42",
        ticket_id="I_atlas_42",
        ticket_title="Add the deterministic delivery proof",
        ticket_url=TICKET_URL,
        parent_spec_url=SPEC_URL,
        integration_worktree=str(integration),
        integration_branch="feature/map-41",
        execution_worktree=str(execution),
        execution_branch="codex/issue-42",
        base_commit="a" * 40,
        owner_skill_name="implement",
        owner_skill_path=str(owner),
        owner_invocation_label="$implement",
        worker_kind="codex",
        validation_argv=("python3", "-m", "pytest", "-q", "tests/test_delivery.py"),
        completion_contract="one-local-commit-integrated-and-validated",
    )


def _delivery_application(tmp_path, *, runtime, prerequisites=None):
    tracker = DeliveryTracker()
    context = CommissioningContext(
        project_id="PVT_acme_7",
        project_url=PROJECT_URL,
        repository="acme/atlas",
        repository_path=str(tmp_path),
        pm_profile="pm",
        routing_policy="codex",
        herdr_executable="herdr-test",
        skills=("map-governance:pm", "delivery-pipeline", "herdr"),
        supported_worker_kinds=("codex",),
    )
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data",
        tracker=tracker,
        profile_name="pm",
        clock=lambda: datetime(2026, 8, 24, tzinfo=timezone.utc),
        commissioning_prerequisites=(
            prerequisites or StaticDeliveryPrerequisites(context)
        ),
        coordinator_runtime=runtime,
    )
    project = application.configure_project(project_url=PROJECT_URL)
    application.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    application.assign_pm(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
    )
    return application, tracker, _lane(tmp_path)


def test_public_dispatch_seam_hands_off_one_declared_lane_then_leaves_pm_idle(
    tmp_path,
):
    tracker = DeliveryTracker()
    runtime = DeliveryRuntimeProbe()
    context = CommissioningContext(
        project_id="PVT_acme_7",
        project_url=PROJECT_URL,
        repository="acme/atlas",
        repository_path=str(tmp_path),
        pm_profile="pm",
        routing_policy="codex",
        herdr_executable="herdr-test",
        skills=("map-governance:pm", "delivery-pipeline", "herdr"),
        supported_worker_kinds=("codex",),
    )
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data",
        tracker=tracker,
        profile_name="pm",
        clock=lambda: datetime(2026, 8, 24, tzinfo=timezone.utc),
        commissioning_prerequisites=StaticDeliveryPrerequisites(context),
        coordinator_runtime=runtime,
    )
    project = application.configure_project(project_url=PROJECT_URL)
    application.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    application.assign_pm(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="dispatch-turn-41",
    )
    lane = _lane(tmp_path)

    result = application.dispatch_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )

    assert result["state"] == "dispatched"
    assert result["coordinator"] == {
        "map_id": MAP_ID,
        "state": "idle",
        "active_turn_id": None,
        "last_turn_id": "dispatch-turn-41",
        "last_outcome": "dispatch",
        "last_outcome_id": delivery_dispatch_id(runtime.requests[0]),
    }
    assert len(runtime.requests) == 2
    assert runtime.requests[0].map_id == MAP_ID
    assert runtime.requests[0].map_url == ISSUE_URL
    assert runtime.requests[0].context == context
    assert runtime.requests[0].lane == lane
    assert runtime.requests[0].registry is None
    assert runtime.requests[1].registry is not None
    assert runtime.requests[1].registry.state == "created"
    board_text = json.dumps(application.board(), sort_keys=True)
    assert len(application.board()["maps"]) == 1
    for execution_detail in (
        lane.lane_id,
        lane.ticket_title,
        lane.execution_worktree,
        lane.execution_branch,
    ):
        assert execution_detail not in board_text

    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="dispatch-retry-42",
    )
    retry = application.dispatch_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )
    assert retry["state"] == "dispatched"
    assert len(runtime.requests) == 3
    assert runtime.requests[-1].registry is not None
    assert runtime.requests[-1].registry.state == "running"


@pytest.mark.parametrize(
    ("runtime_type", "message"),
    [
        (ForgedDispatchRuntime, "did not confirm the delivery dispatch boundary"),
        (
            ForgedDispatchRegistryRuntime,
            "registry transition conflicts with tracker truth",
        ),
    ],
)
def test_dispatch_rejects_forged_runtime_handoff_before_finishing_turn(
    tmp_path,
    runtime_type,
    message,
):
    application, tracker, lane = _delivery_application(
        tmp_path,
        runtime=runtime_type(),
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="forged-dispatch-42",
    )

    with pytest.raises(
        RuntimeError,
        match=message,
    ):
        application.dispatch_pm_delivery_lane(
            request_identity=PM_IDENTITY,
            lane=lane,
        )

    assert [record.registry.state for record in tracker.registries] == ["created"]
    assert tracker.reports == []
    coordinator = application.pm_state(request_identity=PM_IDENTITY)["assignment"][
        "coordinator"
    ]
    assert coordinator["state"] == "active"
    assert coordinator["last_outcome"] is None


def test_prepare_rejects_forged_runtime_coordinate_before_tracker_write(tmp_path):
    application, tracker, lane = _delivery_application(
        tmp_path,
        runtime=ForgedPrepareRuntime(),
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="forged-prepare-42",
    )

    with pytest.raises(RuntimeError, match="did not confirm lane preparation"):
        application.dispatch_pm_delivery_lane(
            request_identity=PM_IDENTITY,
            lane=lane,
        )

    assert tracker.registries == []
    assert tracker.reports == []
    coordinator = application.pm_state(request_identity=PM_IDENTITY)["assignment"][
        "coordinator"
    ]
    assert coordinator["state"] == "active"


def test_public_collect_seam_records_acceptance_without_projecting_lane_details(
    tmp_path,
):
    tracker = DeliveryTracker()
    runtime = MissingLimitationsRecoveryRuntime()
    clock_tick = 0

    def advancing_clock():
        nonlocal clock_tick
        clock_tick += 1
        return datetime(2026, 8, 24, 0, 0, clock_tick, tzinfo=timezone.utc)

    context = CommissioningContext(
        project_id="PVT_acme_7",
        project_url=PROJECT_URL,
        repository="acme/atlas",
        repository_path=str(tmp_path),
        pm_profile="pm",
        routing_policy="codex",
        herdr_executable="herdr-test",
        skills=("map-governance:pm", "delivery-pipeline", "herdr"),
        supported_worker_kinds=("codex",),
    )
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data",
        tracker=tracker,
        profile_name="pm",
        clock=advancing_clock,
        commissioning_prerequisites=StaticDeliveryPrerequisites(context),
        coordinator_runtime=runtime,
    )
    project = application.configure_project(project_url=PROJECT_URL)
    application.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    application.assign_pm(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="dispatch-turn-41",
    )
    lane = replace(
        _lane(tmp_path),
        known_limitations=("Windows validation was not run.",),
    )
    application.dispatch_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="collect-turn-41",
    )

    tracker.fail_transitions = True
    with pytest.raises(TrackerError, match="stage transition failure"):
        application.collect_pm_delivery_lane(
            request_identity=PM_IDENTITY,
            lane=lane,
        )
    tracker.fail_transitions = False
    application.reconcile_project(project_id="PVT_acme_7")

    result = application.collect_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )

    assert result["state"] == "locally_validated"
    assert result["acceptance_recommendation"] == "accept"
    assert result["report"]["type"] == "acceptance"
    assert result["coordinator"]["state"] == "idle"
    assert result["coordinator"]["last_outcome"] == "report"
    assert [record.registry.state for record in tracker.registries] == [
        "created",
        "running",
        "terminal",
        "integrated",
    ]
    assert len(tracker.reports) == 1
    assert "Known limitation: Windows validation was not run." in (
        tracker.reports[0].report.content.evidence
    )
    assert (
        "Known limitation: The terminal Herdr transport cache was unavailable; "
        "tracker registry and Git evidence were used."
        in tracker.reports[0].report.content.evidence
    )
    card = application.board()["maps"][0]
    assert card["stage"] == "acceptance"
    assert card["delivery_summary"]["badges"] == [
        {"type": "acceptance_request", "count": 1}
    ]
    assert card["delivery_summary"]["latest"]["summary"] == (
        "Local delivery is validated and ready for acceptance review."
    )
    board_text = json.dumps(card, sort_keys=True)
    for lane_detail in (
        lane.lane_id,
        lane.ticket_title,
        lane.execution_worktree,
        "b" * 40,
        "c" * 40,
    ):
        assert lane_detail not in board_text


@pytest.mark.parametrize(
    ("runtime_type", "message"),
    [
        (
            ForgedAcceptanceRuntime,
            "did not confirm local acceptance readiness",
        ),
        (
            ForgedAcceptanceRegistryRuntime,
            "registry conflicts with the lane contract",
        ),
        (
            ForgedAcceptanceDigestRuntime,
            "did not confirm local acceptance readiness",
        ),
    ],
)
def test_collect_rejects_forged_acceptance_before_terminal_tracker_evidence(
    tmp_path,
    runtime_type,
    message,
):
    application, tracker, lane = _delivery_application(
        tmp_path,
        runtime=runtime_type(),
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="dispatch-before-forged-acceptance-42",
    )
    application.dispatch_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="forged-acceptance-42",
    )

    with pytest.raises(
        RuntimeError,
        match=message,
    ):
        application.collect_pm_delivery_lane(
            request_identity=PM_IDENTITY,
            lane=lane,
        )

    assert [record.registry.state for record in tracker.registries] == [
        "created",
        "running",
    ]
    assert tracker.reports == []
    assert application.board()["maps"][0]["stage"] == "delivery"
    coordinator = application.pm_state(request_identity=PM_IDENTITY)["assignment"][
        "coordinator"
    ]
    assert coordinator["state"] == "active"


def test_collect_retry_rejects_registry_that_differs_from_tracker_truth(tmp_path):
    runtime = MismatchedIdempotentRegistryRuntime()
    application, tracker, lane = _delivery_application(tmp_path, runtime=runtime)
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="dispatch-before-mismatched-retry-42",
    )
    application.dispatch_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="mismatched-retry-42",
    )
    tracker.fail_transitions = True
    with pytest.raises(TrackerError, match="stage transition failure"):
        application.collect_pm_delivery_lane(
            request_identity=PM_IDENTITY,
            lane=lane,
        )
    tracker.fail_transitions = False
    application.reconcile_project(project_id="PVT_acme_7")
    registry_count = len(tracker.registries)
    report_count = len(tracker.reports)

    with pytest.raises(
        RuntimeError,
        match="did not confirm local acceptance readiness",
    ):
        application.collect_pm_delivery_lane(
            request_identity=PM_IDENTITY,
            lane=lane,
        )

    assert registry_count == 4
    assert report_count == 1
    assert len(tracker.registries) == registry_count
    assert len(tracker.reports) == report_count


def test_missing_supported_worker_records_a_clear_whole_map_blocker(tmp_path):
    tracker = DeliveryTracker()
    runtime = MissingWorkerRuntime()
    clock_tick = 0

    def advancing_clock():
        nonlocal clock_tick
        clock_tick += 1
        return datetime(2026, 8, 24, 0, 1, clock_tick, tzinfo=timezone.utc)

    context = CommissioningContext(
        project_id="PVT_acme_7",
        project_url=PROJECT_URL,
        repository="acme/atlas",
        repository_path=str(tmp_path),
        pm_profile="pm",
        routing_policy="codex",
        herdr_executable="herdr-test",
        skills=("map-governance:pm", "delivery-pipeline", "herdr"),
        supported_worker_kinds=(),
    )
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data",
        tracker=tracker,
        profile_name="pm",
        clock=advancing_clock,
        commissioning_prerequisites=StaticDeliveryPrerequisites(context),
        coordinator_runtime=runtime,
    )
    project = application.configure_project(project_url=PROJECT_URL)
    application.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    application.assign_pm(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="dispatch-turn-41",
    )

    lane = _lane(tmp_path)
    tracker.fail_transitions = True
    with pytest.raises(TrackerError, match="stage transition failure"):
        application.dispatch_pm_delivery_lane(
            request_identity=PM_IDENTITY,
            lane=lane,
        )
    tracker.fail_transitions = False
    application.reconcile_project(project_id="PVT_acme_7")

    result = application.dispatch_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )

    assert result["state"] == "blocked"
    assert result["blocker"] == {
        "type": "commissioning_prerequisites_not_ready",
        "reason": "supported_worker_integration_missing",
        "failed_checks": ["herdr.integration.codex"],
        "retryable": True,
    }
    assert result["coordinator"]["state"] == "idle"
    assert len(tracker.reports) == 1
    card = application.board()["maps"][0]
    assert card["stage"] == "decision"
    assert card["delivery_summary"]["badges"] == [
        {"type": "whole_map_blocker", "count": 1}
    ]
    assert card["delivery_summary"]["latest"]["summary"] == (
        "Delivery is blocked because no plugin-supported Codex Herdr route is "
        "configured and verified."
    )
    assert card["delivery_summary"]["latest"]["continuation_requirement"] == (
        "Select Codex or mixed routing and verify the Codex Herdr integration."
    )


def test_non_worker_commissioning_failure_records_accurate_remediation(tmp_path):
    application, tracker, lane = _delivery_application(
        tmp_path,
        runtime=DeliveryRuntimeProbe(),
        prerequisites=FailedDeliveryPrerequisites(),
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="dispatch-prerequisite-42",
    )

    result = application.dispatch_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )

    assert result["state"] == "blocked"
    assert result["blocker"]["reason"] == "github_authority_unavailable"
    assert tracker.reports[0].report.content.summary == (
        "Delivery is blocked by an unmet commissioning prerequisite."
    )
    assert tracker.reports[0].report.content.continuation_requirement == (
        "Repair the failed commissioning prerequisite and rerun verification."
    )
    first_record_id = tracker.reports[0].report.content.record_id
    tracker.issue = replace(
        tracker.issue,
        labels=("map", "map-stage/delivery"),
    )
    application.reconcile_project(project_id="PVT_acme_7")
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="dispatch-prerequisite-42-again",
    )

    repeated = application.dispatch_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )

    assert repeated["state"] == "blocked"
    assert len(tracker.reports) == 2
    assert tracker.reports[1].report.content.record_id != first_record_id


def test_worker_blocked_report_becomes_a_board_blocker_not_acceptance(tmp_path):
    runtime = WorkerBlockedRuntime()
    application, tracker, lane = _delivery_application(tmp_path, runtime=runtime)
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="dispatch-worker-blocked-42",
    )
    application.dispatch_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="collect-worker-blocked-42",
    )

    result = application.collect_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )

    assert result["state"] == "blocked"
    assert result["report"]["type"] == "blocker"
    assert result["blocked_registry"]["blocker_summary"] == ("dependency unavailable")
    assert tracker.reports[-1].report.content.summary == (
        "Delivery is blocked; implementation details remain in the delivery ticket."
    )
    assert [record.registry.state for record in tracker.registries] == [
        "created",
        "running",
        "blocked",
    ]
    card = application.board()["maps"][0]
    assert card["stage"] == "decision"
    assert card["delivery_summary"]["badges"] == [
        {"type": "whole_map_blocker", "count": 1}
    ]
    restarted = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data",
        tracker=tracker,
        profile_name="pm",
    )
    assert restarted.board()["maps"][0]["delivery_summary"]["latest"]["summary"] == (
        "Delivery is blocked; implementation details remain in the delivery ticket."
    )

    tracker.issue = replace(
        tracker.issue,
        labels=("map", "map-stage/delivery"),
    )
    application.reconcile_project(project_id="PVT_acme_7")
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="resume-worker-blocked-42",
    )
    resumed = application.dispatch_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )
    assert resumed["state"] == "dispatched"
    assert resumed["registry"]["state"] == "running"
    assert resumed["registry"]["evidence_source"] is None
    runtime.blocker_summary = "review environment unavailable"
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="collect-resumed-blocked-again-42",
    )
    blocked_again = application.collect_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )
    assert blocked_again["state"] == "blocked"
    assert tracker.reports[-1].report.content.summary == (
        "Delivery is blocked; implementation details remain in the delivery ticket."
    )
    assert tracker.reports[-1].report.content.record_id != (
        tracker.reports[-2].report.content.record_id
    )
    tracker.issue = replace(
        tracker.issue,
        labels=("map", "map-stage/delivery"),
    )
    application.reconcile_project(project_id="PVT_acme_7")
    runtime.blocked = False
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="resume-worker-blocked-again-42",
    )
    application.dispatch_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="collect-resumed-42",
    )
    accepted = application.collect_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )
    assert accepted["state"] == "locally_validated"
    assert accepted["report"]["type"] == "acceptance"
    assert [record.registry.state for record in tracker.registries] == [
        "created",
        "running",
        "blocked",
        "running",
        "blocked",
        "running",
        "terminal",
        "integrated",
    ]
    assert application.board()["maps"][0]["stage"] == "acceptance"


@pytest.mark.parametrize(
    "runtime_type",
    [ForgedBlockedRegistryRuntime, ForgedBlockedOutcomeRuntime],
)
def test_worker_blocker_rejects_forged_registry_before_tracker_write(
    tmp_path,
    runtime_type,
):
    application, tracker, lane = _delivery_application(
        tmp_path,
        runtime=runtime_type(),
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="dispatch-before-forged-blocker-42",
    )
    application.dispatch_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="forged-blocker-42",
    )

    with pytest.raises(
        RuntimeError,
        match="registry conflicts with the lane contract|does not match the registry",
    ):
        application.collect_pm_delivery_lane(
            request_identity=PM_IDENTITY,
            lane=lane,
        )

    assert [record.registry.state for record in tracker.registries] == [
        "created",
        "running",
    ]
    assert tracker.reports == []
    assert application.board()["maps"][0]["stage"] == "delivery"


def test_worker_blocker_keeps_lane_detail_only_in_ticket_registry(tmp_path):
    runtime = WorkerBlockedRuntime()
    application, tracker, lane = _delivery_application(tmp_path, runtime=runtime)
    blocker = (
        f"failure in {lane.execution_worktree} at {'a' * 40} while running "
        f"{' '.join(lane.validation_argv)}"
    )
    runtime.blocker_summary = blocker
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="dispatch-sensitive-blocker-42",
    )
    application.dispatch_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="collect-sensitive-blocker-42",
    )

    result = application.collect_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )

    assert result["blocked_registry"]["blocker_summary"] == blocker
    assert tracker.reports[-1].report.content.summary == (
        "Delivery is blocked; implementation details remain in the delivery ticket."
    )
    board_text = json.dumps(application.board(), sort_keys=True)
    for delivery_detail in (
        lane.execution_worktree,
        "a" * 40,
        " ".join(lane.validation_argv),
    ):
        assert delivery_detail not in board_text


def test_collect_rejects_lane_payload_drift_before_runtime_mutation(tmp_path):
    tracker = DeliveryTracker()
    runtime = DeliveryRuntimeProbe()
    context = CommissioningContext(
        project_id="PVT_acme_7",
        project_url=PROJECT_URL,
        repository="acme/atlas",
        repository_path=str(tmp_path),
        pm_profile="pm",
        routing_policy="codex",
        herdr_executable="herdr-test",
        skills=("map-governance:pm", "delivery-pipeline", "herdr"),
        supported_worker_kinds=("codex",),
    )
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data",
        tracker=tracker,
        profile_name="pm",
        clock=lambda: datetime(2026, 8, 24, tzinfo=timezone.utc),
        commissioning_prerequisites=StaticDeliveryPrerequisites(context),
        coordinator_runtime=runtime,
    )
    project = application.configure_project(project_url=PROJECT_URL)
    application.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    application.assign_pm(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="dispatch-turn-41",
    )
    lane = _lane(tmp_path)
    application.dispatch_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id="collect-turn-41",
    )

    with pytest.raises(ValueError, match="does not match the dispatch handoff"):
        application.collect_pm_delivery_lane(
            request_identity=PM_IDENTITY,
            lane=replace(lane, ticket_title="Drifted ticket payload"),
        )

    assert len(runtime.requests) == 2
    assert runtime.requests[0].map_id == MAP_ID
    assert runtime.requests[0].map_url == ISSUE_URL
    assert runtime.requests[0].context == context
    assert runtime.requests[0].lane == lane


@pytest.mark.parametrize("foreign_state", ["running", "integrated", "mystery"])
def test_ticket_rejects_a_second_active_lane_registry(tmp_path, foreign_state):
    tracker = DeliveryTracker()
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data",
        tracker=tracker,
        profile_name="pm",
    )
    application.configure_project(project_url=PROJECT_URL)
    lane = _lane(tmp_path)
    request = DeliveryRuntimeRequest(
        map_id=MAP_ID,
        map_url=ISSUE_URL,
        context=CommissioningContext(
            project_id="PVT_acme_7",
            project_url=PROJECT_URL,
            repository="acme/atlas",
            repository_path=str(tmp_path),
            pm_profile="pm",
            routing_policy="codex",
            herdr_executable="herdr-test",
            skills=(),
            supported_worker_kinds=("codex",),
        ),
        lane=lane,
    )
    conflicting = _registry_for_request(
        request,
        state=foreign_state,
        head_commit="b" * 40 if foreign_state == "integrated" else None,
        integrated_commit="c" * 40 if foreign_state == "integrated" else None,
    )
    conflicting = replace(conflicting, lane_id="implementation-999")
    tracker.registries.append(
        TrackerDeliveryLaneRegistryRecord(
            registry=conflicting,
            tracker_record_id="IC_conflict",
            tracker_record_url=f"{TICKET_URL}#issuecomment-conflict",
        )
    )

    with pytest.raises(RuntimeError, match="already owns"):
        application._latest_delivery_lane_registry(
            project_id="PVT_acme_7",
            ticket_url=TICKET_URL,
            lane=lane,
        )


@pytest.mark.parametrize(
    "history_kind",
    ["lone_running", "skipped", "regressed", "drift_then_restored"],
)
def test_tracker_registry_recovery_rejects_invalid_full_history_before_mutation(
    tmp_path,
    history_kind,
):
    runtime = DeliveryRuntimeProbe()
    application, tracker, lane = _delivery_application(tmp_path, runtime=runtime)
    request = DeliveryRuntimeRequest(
        map_id=MAP_ID,
        map_url=ISSUE_URL,
        context=CommissioningContext(
            project_id="PVT_acme_7",
            project_url=PROJECT_URL,
            repository="acme/atlas",
            repository_path=str(tmp_path),
            pm_profile="pm",
            routing_policy="codex",
            herdr_executable="herdr-test",
            skills=(),
            supported_worker_kinds=("codex",),
        ),
        lane=lane,
    )
    created = _registry_for_request(request, state="created")
    running = _registry_for_request(request, state="running")
    terminal = _registry_for_request(
        request,
        state="terminal",
        head_commit="b" * 40,
    )
    integrated = _registry_for_request(
        request,
        state="integrated",
        head_commit="b" * 40,
        integrated_commit="c" * 40,
    )
    histories = {
        "lone_running": [running],
        "skipped": [created, integrated],
        "regressed": [created, running, terminal, running],
        "drift_then_restored": [
            created,
            replace(running, workspace_id="wB"),
            terminal,
        ],
    }
    history = histories[history_kind]
    for index, registry in enumerate(history):
        tracker.registries.append(
            TrackerDeliveryLaneRegistryRecord(
                registry=registry,
                tracker_record_id=f"IC_history_{index}",
                tracker_record_url=f"{TICKET_URL}#issuecomment-history-{index}",
            )
        )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id=f"invalid-history-{history_kind}",
    )

    with pytest.raises(RuntimeError, match="registry history conflicts"):
        application.dispatch_pm_delivery_lane(
            request_identity=PM_IDENTITY,
            lane=lane,
        )

    assert [record.registry for record in tracker.registries] == history
    assert tracker.reports == []
    assert runtime.requests == []


@pytest.mark.parametrize(
    "invalid_relationship",
    [
        "parent",
        "mixed_parent",
        "duplicate_parent",
        "duplicate_dependency",
        "open_dependency",
        "external_dependency",
        "unknown_dependency",
    ],
)
def test_dispatch_requires_tracker_confirmed_ticket_relationships(
    tmp_path,
    invalid_relationship,
):
    runtime = DeliveryRuntimeProbe()
    application, tracker, lane = _delivery_application(tmp_path, runtime=runtime)
    if invalid_relationship in {"parent", "mixed_parent", "duplicate_parent"}:
        parent = (
            "https://github.com/acme/atlas/issues/39"
            if invalid_relationship == "parent"
            else (
                f"{SPEC_URL} https://github.com/other/repo/issues/99"
                if invalid_relationship == "mixed_parent"
                else f"{SPEC_URL}\n\n**Parent:** https://github.com/other/repo/issues/99"
            )
        )
        tracker.ticket = replace(
            tracker.ticket,
            body=(
                f"## Parent\n\n{parent}\n\n"
                "## Blocked by\n\nNone - can start immediately\n"
            ),
        )
        expected = (
            "multiple Parent"
            if invalid_relationship == "duplicate_parent"
            else "does not link"
        )
    elif invalid_relationship == "duplicate_dependency":
        tracker.ticket = replace(
            tracker.ticket,
            body=(
                f"## Parent\n\n{SPEC_URL}\n\n"
                "## Blocked by\n\nNone - can start immediately\n\n"
                "**Blocked by:** #43\n"
            ),
        )
        expected = "multiple Blocked by"
    else:
        dependency_url = "https://github.com/acme/atlas/issues/43"
        tracker.dependencies[dependency_url] = TrackerIssue(
            id="I_atlas_43",
            repository="acme/atlas",
            number=43,
            title="Unfinished prerequisite",
            url=dependency_url,
            state=("open" if invalid_relationship == "open_dependency" else "closed"),
            state_reason=None,
            labels=("implementation",),
        )
        dependency_declaration = {
            "open_dependency": "#43",
            "external_dependency": ("#43, https://github.com/other/repo/issues/99"),
            "unknown_dependency": "#43, blocker TBD",
        }[invalid_relationship]
        tracker.ticket = replace(
            tracker.ticket,
            body=(
                f"## Parent\n\n{SPEC_URL}\n\n"
                f"## Blocked by\n\n{dependency_declaration}\n"
            ),
        )
        expected = {
            "open_dependency": "not independently grabbable",
            "external_dependency": "outside the Map repository",
            "unknown_dependency": "declaration is invalid",
        }[invalid_relationship]
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id="coordinator-atlas",
        turn_id=f"invalid-{invalid_relationship}-42",
    )

    with pytest.raises(ValueError, match=expected):
        application.dispatch_pm_delivery_lane(
            request_identity=PM_IDENTITY,
            lane=lane,
        )

    assert runtime.requests == []


class HerdrDispatchRunner:
    def __init__(
        self,
        *,
        namespace: str,
        workspace_label: str,
        root_agent_name: str,
        repository_path: str,
        execution_path: str,
        on_prompt=None,
    ) -> None:
        self.namespace = namespace
        self.workspace_label = workspace_label
        self.root_agent_name = root_agent_name
        self.repository_path = repository_path
        self.execution_path = execution_path
        self.calls: list[tuple[str, ...]] = []
        self.worker_name = ""
        self.pane_prepared = False
        self.worker_started = False
        self.worker_terminal = False
        self.session_available = True
        self.workspace_available = True
        self.root_pane_available = True
        self.root_agent_available = True
        self.delivery_pane_available = True
        self.worker_available = True
        self.foreign_occupant = False
        self.foreign_tab_delivery_pane = False
        self.foreign_same_cwd_pane = False
        self.malformed_pane = False
        self.wrong_pane_list_type = False
        self.malformed_agent = False
        self.report_truncated = False
        self.final_report_text: str | None = None
        self.on_prompt = on_prompt

    @staticmethod
    def _result(payload: dict) -> CoordinatorCommandResult:
        return CoordinatorCommandResult(0, json.dumps(payload))

    def run(self, arguments, *, timeout):
        call = tuple(arguments)
        self.calls.append(call)
        assert timeout > 0
        command = call[3:]
        if call == ("herdr-test", "session", "list", "--json"):
            return self._result(
                {
                    "sessions": (
                        [{"name": self.namespace}] if self.session_available else []
                    )
                }
            )
        if command == ("workspace", "list"):
            return self._result(
                {
                    "id": "workspace-list",
                    "result": {
                        "type": "workspace_list",
                        "workspaces": (
                            [
                                {
                                    "workspace_id": "wA",
                                    "label": self.workspace_label,
                                }
                            ]
                            if self.workspace_available
                            else []
                        ),
                    },
                }
            )
        if command == ("workspace", "get", "wA"):
            return self._result(
                {
                    "id": "workspace-get",
                    "result": {
                        "type": "workspace_info",
                        "workspace": {
                            "workspace_id": "wA",
                            "label": self.workspace_label,
                            "active_tab_id": "wA:t1",
                        },
                    },
                }
            )
        if command in {("pane", "list", "--workspace", "wA"), ("pane", "list")}:
            panes = []
            if self.root_pane_available:
                panes.append(
                    {
                        "workspace_id": "wA",
                        "tab_id": "wA:t1",
                        "pane_id": "wA:p1",
                        "cwd": self.repository_path,
                    }
                )
            if self.pane_prepared and self.delivery_pane_available:
                panes.append(
                    {
                        "workspace_id": "wA",
                        "tab_id": "wA:t1",
                        "pane_id": "wA:p2",
                        "cwd": self.execution_path,
                    }
                )
            if self.foreign_tab_delivery_pane:
                panes.append(
                    {
                        "workspace_id": "wA",
                        "tab_id": "wA:t9",
                        "pane_id": "wA:p9",
                        "cwd": self.execution_path,
                    }
                )
            if self.malformed_pane:
                panes.append({"pane_id": "partial-pane"})
            if self.foreign_same_cwd_pane and command == ("pane", "list"):
                panes.append(
                    {
                        "workspace_id": "wB",
                        "tab_id": "wB:t1",
                        "pane_id": "wB:p1",
                        "cwd": self.execution_path,
                    }
                )
            return self._result(
                {
                    "id": "pane-list",
                    "result": {
                        "type": (
                            "workspace_list"
                            if self.wrong_pane_list_type
                            else "pane_list"
                        ),
                        "panes": panes,
                    },
                }
            )
        if command == ("agent", "get", self.root_agent_name):
            if not self.root_agent_available:
                return CoordinatorCommandResult(1, "")
            return self._result(
                {
                    "id": "root-agent",
                    "result": {
                        "type": "agent_info",
                        "agent": {
                            "name": self.root_agent_name,
                            "agent": "hermes",
                            "workspace_id": "wA",
                            "tab_id": "wA:t1",
                            "pane_id": "wA:p1",
                            "agent_session": {
                                "source": "herdr:hermes",
                                "agent": "hermes",
                                "kind": "id",
                                "value": "pm-session-atlas",
                            },
                        },
                    },
                }
            )
        if command[:2] == ("pane", "split"):
            assert command == (
                "pane",
                "split",
                "--pane",
                "wA:p1",
                "--direction",
                "right",
                "--cwd",
                self.execution_path,
                "--no-focus",
            )
            self.pane_prepared = True
            return self._result(
                {
                    "id": "pane-split",
                    "result": {
                        "type": "pane_info",
                        "pane": {
                            "workspace_id": "wA",
                            "tab_id": "wA:t1",
                            "pane_id": "wA:p2",
                            "cwd": self.execution_path,
                        },
                    },
                }
            )
        if command == ("agent", "list"):
            agents = []
            if self.worker_started and self.worker_available:
                agents.append(
                    {
                        "name": self.worker_name,
                        "agent": "codex",
                        "agent_status": ("done" if self.worker_terminal else "working"),
                        "workspace_id": "wA",
                        "tab_id": "wA:t1",
                        "pane_id": "wA:p2",
                        "cwd": self.execution_path,
                    }
                )
            if self.foreign_occupant:
                agents.append(
                    {
                        "name": "foreign_writer",
                        "agent": "codex",
                        "agent_status": "working",
                        "workspace_id": "wA",
                        "tab_id": "wA:t1",
                        "pane_id": "wA:p2",
                        "cwd": self.execution_path,
                    }
                )
            if self.malformed_agent:
                agents.append({"name": "partial-agent"})
            return self._result(
                {
                    "id": "agent-list",
                    "result": {"type": "agent_list", "agents": agents},
                }
            )
        if command[:2] == ("agent", "get"):
            if (
                command[2] == self.worker_name
                and self.worker_started
                and self.worker_available
            ):
                return self._worker(
                    "agent_info",
                    status="done" if self.worker_terminal else "working",
                )
            return CoordinatorCommandResult(1, "")
        if command[:2] == ("agent", "start"):
            self.worker_name = command[2]
            self.worker_started = True
            assert command == (
                "agent",
                "start",
                self.worker_name,
                "--kind",
                "codex",
                "--pane",
                "wA:p2",
                "--timeout",
                "30000",
            )
            return self._worker("agent_started", status="idle")
        if command[:2] == ("agent", "prompt"):
            packet = json.loads(command[3])
            assert packet["protocol"] == "delivery-pipeline/herdr-implementation-v1"
            assert packet["ticket"]["id"] == "I_atlas_42"
            assert packet["owner"]["name"] == "implement"
            assert packet["validation"]["argv"][0] in {"python3", "git"}
            assert packet["completion"]["remote_actions"] == "forbidden"
            assert packet["runtime_context"] == {
                "repository": {
                    "coordinate": "acme/atlas",
                    "root": self.repository_path,
                },
                "herdr": {
                    "session": self.namespace,
                    "workspace_id": "wA",
                    "tab_id": "wA:t1",
                    "pane_id": "wA:p2",
                },
            }
            assert packet["final_report_contract"]["required_field_order"] == [
                "Ticket",
                "状态",
                "Pane/worktree/branch",
                "Commit",
                "Checks",
                "Review",
                "Dirty state",
                "Touched files",
                "Blocker",
            ]
            assert packet["final_report_contract"]["completed_values"] == {
                "Ticket": (
                    f"{packet['ticket']['id']} {packet['ticket']['title']} "
                    f"{packet['ticket']['url']}"
                ),
                "状态": "completed",
                "Pane/worktree/branch": (f"wA:p2 {self.execution_path} codex/issue-42"),
                "Commit": "<exact local HEAD SHA and subject>",
                "Checks": "passed",
                "Review": "passed",
                "Dirty state": "clean",
                "Touched files": "<ticket-owned paths>",
                "Blocker": "none",
            }
            assert command[-5:] == (
                "--wait",
                "--until",
                "working",
                "--timeout",
                "30000",
            )
            if self.on_prompt is not None:
                self.on_prompt(packet)
            self.worker_terminal = True
            return self._worker("agent_prompted", status="working")
        if command[:2] == ("agent", "read"):
            assert command == (
                "agent",
                "read",
                self.worker_name,
                "--source",
                "recent-unwrapped",
                "--lines",
                "400",
                "--format",
                "text",
            )
            final_report = self.final_report_text
            if final_report is None:
                commit = _git("rev-parse", "HEAD", cwd=Path(self.execution_path))
                final_report = _worker_final_report(
                    worktree=self.execution_path,
                    commit=f"{commit} local delivery",
                )
            return self._result(
                {
                    "id": "agent-read",
                    "result": {
                        "type": "pane_read",
                        "read": {
                            "workspace_id": "wA",
                            "tab_id": "wA:t1",
                            "pane_id": "wA:p2",
                            "source": "recent_unwrapped",
                            "format": "text",
                            "text": final_report,
                            "revision": 7,
                            "truncated": self.report_truncated,
                        },
                    },
                }
            )
        raise AssertionError(call)

    def _worker(self, result_type: str, *, status: str):
        return self._result(
            {
                "id": result_type,
                "result": {
                    "type": result_type,
                    "agent": {
                        "name": self.worker_name,
                        "agent": "codex",
                        "agent_status": status,
                        "workspace_id": "wA",
                        "tab_id": "wA:t1",
                        "pane_id": "wA:p2",
                        "cwd": self.execution_path,
                    },
                },
            }
        )

    def start(self, _arguments):  # pragma: no cover - root is already running
        raise AssertionError("dispatch must not start a second Herdr session")


def _git(*arguments: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _delivery_git_lane(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _git("init", "-b", "main", cwd=source)
    _git("config", "user.name", "Map Governance Test", cwd=source)
    _git("config", "user.email", "map-governance@example.test", cwd=source)
    (source / "README.md").write_text("base\n", encoding="utf-8")
    _git("add", "README.md", cwd=source)
    _git("commit", "-m", "base", cwd=source)
    base = _git("rev-parse", "HEAD", cwd=source)
    integration = tmp_path / "atlas-map-1"
    execution = tmp_path / "atlas-map-1-issue-41"
    _git("worktree", "add", "-b", "feature/map-41", str(integration), base, cwd=source)
    _git("worktree", "add", "-b", "codex/issue-42", str(execution), base, cwd=source)
    owner = tmp_path / "implement" / "SKILL.md"
    owner.parent.mkdir()
    owner.write_text("---\nname: implement\n---\n", encoding="utf-8")
    context = CommissioningContext(
        project_id="PVT_acme_7",
        project_url=PROJECT_URL,
        repository="acme/atlas",
        repository_path=str(source),
        pm_profile="pm",
        routing_policy="codex",
        herdr_executable="herdr-test",
        skills=("map-governance:pm", "delivery-pipeline", "herdr"),
        supported_worker_kinds=("codex",),
        implement_skill_path=str(owner),
    )
    lane = DeliveryLaneSpec(
        protocol="delivery-pipeline/herdr-implementation-v1",
        lane_id="implementation-42",
        ticket_id="I_atlas_42",
        ticket_title="Add the deterministic delivery proof",
        ticket_url=TICKET_URL,
        parent_spec_url=SPEC_URL,
        integration_worktree=str(integration),
        integration_branch="feature/map-41",
        execution_worktree=str(execution),
        execution_branch="codex/issue-42",
        base_commit=base,
        owner_skill_name="implement",
        owner_skill_path=str(owner),
        owner_invocation_label="$implement",
        worker_kind="codex",
        validation_argv=("python3", "-m", "pytest", "-q", "tests/test_delivery.py"),
        completion_contract="one-local-commit-integrated-and-validated",
    )
    return context, lane


def test_delivery_git_paths_must_belong_to_the_configured_map_repository(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    context, expected_lane = _delivery_git_lane(first)
    _foreign_context, foreign_lane = _delivery_git_lane(second)
    foreign_lane = replace(
        foreign_lane,
        owner_skill_path=expected_lane.owner_skill_path,
    )
    request = DeliveryRuntimeRequest(
        map_id=MAP_ID,
        map_url=ISSUE_URL,
        context=context,
        lane=foreign_lane,
    )

    with pytest.raises(CoordinatorRuntimeError, match="worktree_repository_mismatch"):
        CoordinatorRuntime._validate_delivery_git(request, collecting=False)


def test_delivery_lane_rejects_owner_and_worktree_symlink_aliases(tmp_path):
    context, lane = _delivery_git_lane(tmp_path)
    owner_alias = tmp_path / "owner-alias.md"
    owner_alias.symlink_to(Path(lane.owner_skill_path))
    request = DeliveryRuntimeRequest(
        map_id=MAP_ID,
        map_url=ISSUE_URL,
        context=context,
        lane=replace(lane, owner_skill_path=str(owner_alias)),
    )

    with pytest.raises(ValueError, match="owner path is unavailable"):
        CoordinatorRuntime._validate_delivery_request(request)

    execution_alias = tmp_path / "execution-alias"
    execution_alias.symlink_to(Path(lane.execution_worktree), target_is_directory=True)
    request = replace(
        request,
        lane=replace(lane, execution_worktree=str(execution_alias)),
    )
    with pytest.raises(ValueError, match="existing absolute directories"):
        CoordinatorRuntime._validate_delivery_request(request)


@pytest.mark.parametrize(
    "validation_argv",
    [
        ("ruff", "check", "--fix", "."),
        ("ruff", "check", "--fix-only", "."),
        ("ruff", "check", "--fix=true", "."),
        ("ruff", "check", "--unsafe-fixes=true", "."),
        ("python3", "unsafe-script.py"),
        ("/usr/bin/git", "diff", "--check"),
    ],
)
def test_delivery_validation_rejects_mutating_or_untrusted_argv(
    tmp_path,
    validation_argv,
):
    context, lane = _delivery_git_lane(tmp_path)
    request = DeliveryRuntimeRequest(
        map_id=MAP_ID,
        map_url=ISSUE_URL,
        context=context,
        lane=replace(lane, validation_argv=validation_argv),
    )

    with pytest.raises(ValueError, match="not safely bounded"):
        CoordinatorRuntime._validate_delivery_request(request)


def test_claude_lane_reports_the_unimplemented_bootstrap_prerequisite(tmp_path):
    context, lane = _delivery_git_lane(tmp_path)
    request = DeliveryRuntimeRequest(
        map_id=MAP_ID,
        map_url=ISSUE_URL,
        context=replace(context, supported_worker_kinds=("claude",)),
        lane=replace(
            lane,
            worker_kind="claude",
            execution_branch="claude/issue-42",
        ),
    )

    with pytest.raises(CommissioningPrerequisiteError) as raised:
        CoordinatorRuntime._validate_delivery_request(request)

    assert raised.value.reason == "supported_worker_routing_missing"
    assert raised.value.failed_checks == ("delivery.worker.codex",)


def test_augmented_worker_prompt_retains_sensitive_material_guard(tmp_path):
    context, lane = _delivery_git_lane(tmp_path)
    request = DeliveryRuntimeRequest(
        map_id=MAP_ID,
        map_url=ISSUE_URL,
        context=replace(context, repository_path="/tmp/token=secret-value"),
        lane=lane,
    )
    registry = _registry_for_request(request, state="created")

    with pytest.raises(ValueError, match="sensitive material"):
        CoordinatorRuntime._validate_payload(delivery_worker_prompt(request, registry))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("lane_id", "implementation-41", "identity does not match"),
        ("integration_branch", "feature/map-40", "does not match the Map"),
        ("execution_branch", "codex/issue-41", "does not match the ticket"),
    ],
)
def test_delivery_lane_requires_exact_map_and_ticket_coordinates(
    tmp_path,
    field,
    value,
    message,
):
    context, lane = _delivery_git_lane(tmp_path)
    request = DeliveryRuntimeRequest(
        map_id=MAP_ID,
        map_url=ISSUE_URL,
        context=context,
        lane=replace(lane, **{field: value}),
    )

    with pytest.raises(ValueError, match=message):
        CoordinatorRuntime._validate_delivery_request(request)


@pytest.mark.parametrize(
    "review",
    [
        "not passed",
        "failed",
        "未通过",
        "不通过",
        "没有通过",
        "未能通过",
        "失败",
        "未成功",
    ],
)
def test_final_report_rejects_negative_review_phrasing(review):
    report = _worker_final_report(
        worktree="/tmp/execution",
        commit="a" * 40,
        review=review,
    )

    with pytest.raises(CoordinatorRuntimeError, match="worker_final_report_negative"):
        CoordinatorRuntime._parse_worker_final_report(report)


def test_final_report_rejects_out_of_order_fields_and_completed_blocker():
    report = _worker_final_report(worktree="/tmp/execution", commit="a" * 40)
    out_of_order = report.replace(
        "Checks：passed\nReview：passed\n",
        "Review：passed\nChecks：passed\n",
    )

    with pytest.raises(CoordinatorRuntimeError, match="worker_final_report_negative"):
        CoordinatorRuntime._parse_worker_final_report(out_of_order)
    with pytest.raises(CoordinatorRuntimeError, match="worker_final_report_negative"):
        CoordinatorRuntime._parse_worker_final_report(
            report.replace("Blocker：none", "Blocker：tests failed")
        )


def test_post_validation_cleanliness_gate_rejects_a_mutated_integration(tmp_path):
    _context, lane = _delivery_git_lane(tmp_path)
    integration = Path(lane.integration_worktree)
    (integration / "validation-side-effect.txt").write_text(
        "unexpected mutation\n", encoding="utf-8"
    )

    with pytest.raises(CoordinatorRuntimeError, match="integration_not_clean"):
        CoordinatorRuntime._require_clean_integration(lane.integration_worktree)


def test_post_validation_gate_rejects_a_validation_created_clean_commit(tmp_path):
    _context, lane = _delivery_git_lane(tmp_path)
    integration = Path(lane.integration_worktree)
    expected_commit = _git("rev-parse", "HEAD", cwd=integration)
    (integration / "validation-commit.txt").write_text(
        "created by validation\n", encoding="utf-8"
    )
    _git("add", "validation-commit.txt", cwd=integration)
    _git("commit", "-m", "validation side effect", cwd=integration)
    assert _git("status", "--porcelain=v1", cwd=integration) == ""

    with pytest.raises(
        CoordinatorRuntimeError,
        match="validation_changed_integration_history",
    ):
        CoordinatorRuntime._require_integration_head(
            lane.integration_worktree,
            expected_commit,
        )


def test_integration_ownership_requires_the_exact_base_history(tmp_path):
    _context, lane = _delivery_git_lane(tmp_path)
    execution = Path(lane.execution_worktree)
    integration = Path(lane.integration_worktree)
    (execution / "delivery.txt").write_text("same outcome\n", encoding="utf-8")
    _git("add", "delivery.txt", cwd=execution)
    _git("commit", "-m", "delivery patch", cwd=execution)
    execution_commit = _git("rev-parse", "HEAD", cwd=execution)
    execution_tree = _git("rev-parse", f"{execution_commit}^{{tree}}", cwd=execution)
    unrelated_head = _git(
        "commit-tree",
        execution_tree,
        "-m",
        "unrelated same outcome",
        cwd=integration,
    )
    _git(
        "update-ref",
        f"refs/heads/{lane.integration_branch}",
        unrelated_head,
        cwd=integration,
    )
    _git("reset", "--hard", unrelated_head, cwd=integration)
    assert (
        _git("rev-list", "--count", f"{lane.base_commit}..HEAD", cwd=integration) == "1"
    )
    assert _git("rev-parse", "HEAD^{tree}", cwd=integration) == execution_tree

    with pytest.raises(
        CoordinatorRuntimeError,
        match="overlapping_integration_ownership",
    ):
        CoordinatorRuntime._require_owned_integration_patch(
            lane=lane,
            integration_commit=unrelated_head,
            execution_commit=execution_commit,
        )


def test_failed_cherry_pick_preserves_ambiguous_foreign_operation(tmp_path):
    context, lane = _delivery_git_lane(tmp_path)
    source = Path(context.repository_path)
    foreign = tmp_path / "foreign-writer"
    _git(
        "worktree",
        "add",
        "-b",
        "foreign/writer",
        str(foreign),
        lane.base_commit,
        cwd=source,
    )
    (foreign / "README.md").write_text("foreign cherry-pick\n", encoding="utf-8")
    _git("add", "README.md", cwd=foreign)
    _git("commit", "-m", "foreign cherry-pick", cwd=foreign)
    foreign_commit = _git("rev-parse", "HEAD", cwd=foreign)

    integration = Path(lane.integration_worktree)
    (integration / "README.md").write_text("foreign integration\n", encoding="utf-8")
    _git("add", "README.md", cwd=integration)
    _git("commit", "-m", "foreign integration", cwd=integration)
    conflict = subprocess.run(
        ["git", "cherry-pick", foreign_commit],
        cwd=integration,
        check=False,
        capture_output=True,
        text=True,
    )
    assert conflict.returncode != 0
    status_before = _git("status", "--porcelain=v1", cwd=integration)
    contents_before = (integration / "README.md").read_text(encoding="utf-8")
    assert _git("rev-parse", "CHERRY_PICK_HEAD", cwd=integration) == foreign_commit

    cause = CoordinatorRuntimeError(
        reason="local_evidence_unavailable",
        retryable=True,
    )
    with pytest.raises(
        CoordinatorRuntimeError,
        match="overlapping_integration_ownership",
    ):
        CoordinatorRuntime._handle_failed_cherry_pick(
            lane=lane,
            expected_parent=lane.base_commit,
            cause=cause,
        )

    assert _git("rev-parse", "CHERRY_PICK_HEAD", cwd=integration) == foreign_commit
    assert _git("status", "--porcelain=v1", cwd=integration) == status_before
    assert (integration / "README.md").read_text(encoding="utf-8") == contents_before


def test_cherry_pick_preflight_preserves_same_commit_foreign_operation(tmp_path):
    _context, lane = _delivery_git_lane(tmp_path)
    execution = Path(lane.execution_worktree)
    (execution / "delivery.txt").write_text("delivery\n", encoding="utf-8")
    _git("add", "delivery.txt", cwd=execution)
    _git("commit", "-m", "delivery", cwd=execution)
    execution_commit = _git("rev-parse", "HEAD", cwd=execution)
    integration = Path(lane.integration_worktree)
    _git("update-ref", "CHERRY_PICK_HEAD", execution_commit, cwd=integration)
    status_before = _git("status", "--porcelain=v1", cwd=integration)

    with pytest.raises(
        CoordinatorRuntimeError,
        match="overlapping_integration_ownership",
    ):
        CoordinatorRuntime._require_integration_ready_for_cherry_pick(
            lane,
            expected_head=lane.base_commit,
        )

    assert _git("rev-parse", "CHERRY_PICK_HEAD", cwd=integration) == execution_commit
    assert _git("status", "--porcelain=v1", cwd=integration) == status_before


def test_delivery_terminal_git_proof_rejects_same_head_branch_aliases(tmp_path):
    _context, lane = _delivery_git_lane(tmp_path)
    execution = Path(lane.execution_worktree)
    (execution / "delivery.txt").write_text("delivery\n", encoding="utf-8")
    _git("add", "delivery.txt", cwd=execution)
    _git("commit", "-m", "delivery", cwd=execution)
    execution_commit = _git("rev-parse", "HEAD", cwd=execution)
    _git("switch", "-c", "foreign/execution", cwd=execution)

    with pytest.raises(
        CoordinatorRuntimeError,
        match="worker_completion_contract_unmet",
    ):
        CoordinatorRuntime._execution_commit(lane)

    _git("switch", lane.execution_branch, cwd=execution)
    integration = Path(lane.integration_worktree)
    _git("cherry-pick", execution_commit, cwd=integration)
    integration_commit = _git("rev-parse", "HEAD", cwd=integration)
    _git("switch", "-c", "foreign/integration", cwd=integration)
    with pytest.raises(
        CoordinatorRuntimeError,
        match="overlapping_integration_ownership",
    ):
        CoordinatorRuntime._require_owned_integration_patch(
            lane=lane,
            integration_commit=integration_commit,
            execution_commit=execution_commit,
        )


def test_runtime_dispatches_one_codex_worker_with_fixed_argv_and_full_contract(
    tmp_path,
):
    context, lane = _delivery_git_lane(tmp_path)
    storage = PluginStorage(tmp_path / "plugin-runtime")
    lifecycle = storage.coordinator_lifecycle(created_at="2026-08-24T00:00:00Z")
    namespace = CoordinatorRuntime.session_namespace(lifecycle)
    label = CoordinatorRuntime.workspace_label(lifecycle, MAP_ID)
    root_agent = CoordinatorRuntime.agent_name(lifecycle, MAP_ID)
    storage.reserve_pm_runtime(
        map_id=MAP_ID,
        session_namespace=namespace,
        workspace_label=label,
        agent_id=root_agent,
        ownership_marker=CoordinatorRuntime.ownership_marker(lifecycle, MAP_ID),
        lifecycle_id=lifecycle,
        context=context,
        updated_at="2026-08-24T00:00:00Z",
        session_ownership_marker=CoordinatorRuntime.session_ownership_marker(lifecycle),
    )
    storage.update_pm_runtime(
        map_id=MAP_ID,
        state="active",
        workspace_id="wA",
        window_id="wA:t1",
        pane_id="wA:p1",
        agent_session_id="pm-session-atlas",
        ready_record_id="ready-1",
        updated_at="2026-08-24T00:00:01Z",
    )
    runner = HerdrDispatchRunner(
        namespace=namespace,
        workspace_label=label,
        root_agent_name=root_agent,
        repository_path=context.repository_path,
        execution_path=lane.execution_worktree,
    )
    runtime = CoordinatorRuntime(
        storage=storage,
        runner=runner,
        clock=lambda: "2026-08-24T00:00:02Z",
    )
    request = DeliveryRuntimeRequest(
        map_id=MAP_ID,
        map_url=ISSUE_URL,
        context=context,
        lane=lane,
    )
    runner.wrong_pane_list_type = True
    with pytest.raises(CoordinatorRuntimeError, match="partial_coordinates"):
        runtime.prepare_lane(request)
    assert runner.pane_prepared is False
    runner.wrong_pane_list_type = False
    runner.foreign_same_cwd_pane = True
    with pytest.raises(CoordinatorRuntimeError, match="overlapping_lane_ownership"):
        runtime.prepare_lane(request)
    assert runner.pane_prepared is False
    runner.foreign_same_cwd_pane = False
    runner.foreign_tab_delivery_pane = True
    with pytest.raises(CoordinatorRuntimeError, match="overlapping_lane_ownership"):
        runtime.prepare_lane(request)
    assert runner.pane_prepared is False
    runner.foreign_tab_delivery_pane = False
    runner.malformed_pane = True
    with pytest.raises(CoordinatorRuntimeError, match="partial_coordinates"):
        runtime.prepare_lane(request)
    assert runner.pane_prepared is False
    runner.malformed_pane = False
    prepared = runtime.prepare_lane(request)
    assert runner.worker_started is False
    runner.wrong_pane_list_type = True
    with pytest.raises(CoordinatorRuntimeError, match="partial_coordinates"):
        runtime.dispatch_lane(
            replace(
                request,
                registry=DeliveryLaneRegistry.from_payload(prepared["registry"]),
            )
        )
    assert runner.worker_started is False
    runner.wrong_pane_list_type = False
    runner.foreign_same_cwd_pane = True
    with pytest.raises(CoordinatorRuntimeError, match="overlapping_lane_ownership"):
        runtime.dispatch_lane(
            replace(
                request,
                registry=DeliveryLaneRegistry.from_payload(prepared["registry"]),
            )
        )
    assert runner.worker_started is False
    runner.foreign_same_cwd_pane = False
    runner.malformed_agent = True
    with pytest.raises(CoordinatorRuntimeError, match="partial_coordinates"):
        runtime.dispatch_lane(
            replace(
                request,
                registry=DeliveryLaneRegistry.from_payload(prepared["registry"]),
            )
        )
    assert runner.worker_started is False
    runner.malformed_agent = False
    result = runtime.dispatch_lane(
        replace(
            request,
            registry=DeliveryLaneRegistry.from_payload(prepared["registry"]),
        )
    )

    assert result["state"] == "dispatched"
    assert result["worker_kind"] == "codex"
    assert result["dispatch_id"].startswith("delivery-dispatch:")
    assert result["completion_contract"] == lane.completion_contract
    assert not any("push" in call or "gh" in call for call in runner.calls)
    blocked_registry = replace(
        DeliveryLaneRegistry.from_payload(result["registry"]),
        state="blocked",
        evidence_source="herdr-final-report",
        final_report_digest="sha256:" + "d" * 64,
        blocker_summary="dependency unavailable",
    )
    prompt_count = sum(call[3:5] == ("agent", "prompt") for call in runner.calls)
    resumed = runtime.dispatch_lane(replace(request, registry=blocked_registry))
    assert resumed["registry"]["state"] == "running"
    assert resumed["registry"]["evidence_source"] is None
    assert resumed["registry"]["final_report_digest"] is None
    assert sum(call[3:5] == ("agent", "prompt") for call in runner.calls) == (
        prompt_count + 1
    )
    runner.session_available = False
    with pytest.raises(CoordinatorRuntimeError, match="owned_session_unavailable"):
        runtime.dispatch_lane(
            replace(
                request,
                registry=DeliveryLaneRegistry.from_payload(resumed["registry"]),
            )
        )


def test_runtime_collects_one_durable_commit_integrates_and_validates_idempotently(
    tmp_path,
):
    context, raw_lane = _delivery_git_lane(tmp_path)
    lane = replace(
        raw_lane,
        validation_argv=("git", "diff", "--check", "HEAD^", "HEAD"),
        known_limitations=("Remote publication still requires chairman authority.",),
    )
    storage = PluginStorage(tmp_path / "plugin-runtime")
    lifecycle = storage.coordinator_lifecycle(created_at="2026-08-24T00:00:00Z")
    namespace = CoordinatorRuntime.session_namespace(lifecycle)
    label = CoordinatorRuntime.workspace_label(lifecycle, MAP_ID)
    root_agent = CoordinatorRuntime.agent_name(lifecycle, MAP_ID)
    storage.reserve_pm_runtime(
        map_id=MAP_ID,
        session_namespace=namespace,
        workspace_label=label,
        agent_id=root_agent,
        ownership_marker=CoordinatorRuntime.ownership_marker(lifecycle, MAP_ID),
        lifecycle_id=lifecycle,
        context=context,
        updated_at="2026-08-24T00:00:00Z",
        session_ownership_marker=CoordinatorRuntime.session_ownership_marker(lifecycle),
    )
    storage.update_pm_runtime(
        map_id=MAP_ID,
        state="active",
        workspace_id="wA",
        window_id="wA:t1",
        pane_id="wA:p1",
        agent_session_id="pm-session-atlas",
        ready_record_id="ready-1",
        updated_at="2026-08-24T00:00:01Z",
    )
    runner = HerdrDispatchRunner(
        namespace=namespace,
        workspace_label=label,
        root_agent_name=root_agent,
        repository_path=context.repository_path,
        execution_path=lane.execution_worktree,
    )
    runtime = CoordinatorRuntime(
        storage=storage,
        runner=runner,
        clock=lambda: "2026-08-24T00:00:02Z",
    )
    request = DeliveryRuntimeRequest(
        map_id=MAP_ID,
        map_url=ISSUE_URL,
        context=context,
        lane=lane,
    )
    prepared = runtime.prepare_lane(request)
    request = replace(
        request,
        registry=DeliveryLaneRegistry.from_payload(prepared["registry"]),
    )
    dispatched = runtime.dispatch_lane(request)
    request = replace(
        request,
        registry=DeliveryLaneRegistry.from_payload(dispatched["registry"]),
    )
    execution = Path(lane.execution_worktree)
    (execution / "delivery.txt").write_text("locally delivered\n", encoding="utf-8")
    runner.worker_terminal = False
    dispatch_retry = runtime.dispatch_lane(request)
    assert dispatch_retry["idempotent"] is True
    _git("add", "delivery.txt", cwd=execution)
    _git("commit", "-m", "implement ticket 41", cwd=execution)
    execution_commit = _git("rev-parse", "HEAD", cwd=execution)

    runner.worker_terminal = False
    runner.foreign_same_cwd_pane = True
    with pytest.raises(CoordinatorRuntimeError, match="overlapping_lane_ownership"):
        runtime.collect_lane(request)
    assert (
        _git("rev-parse", "HEAD", cwd=Path(lane.integration_worktree))
        == lane.base_commit
    )
    runner.foreign_same_cwd_pane = False
    with pytest.raises(CoordinatorRuntimeError, match="worker_not_terminal"):
        runtime.collect_lane(request)
    assert (
        _git("rev-parse", "HEAD", cwd=Path(lane.integration_worktree))
        == lane.base_commit
    )
    runner.worker_terminal = True
    for partial_negative in (
        "FINAL_REPORT_BEGIN\n状态：failed\nFINAL_REPORT_END\n",
        ("FINAL_REPORT_BEGIN\n状态：completed\nChecks：failed\nFINAL_REPORT_END\n"),
        (
            "FINAL_REPORT_BEGIN\n"
            "状态：blocked\n"
            "Blocker：dependency unavailable\n"
            "FINAL_REPORT_END\n"
        ),
    ):
        runner.final_report_text = partial_negative
        with pytest.raises(
            CoordinatorRuntimeError,
            match="worker_final_report_negative",
        ):
            runtime.collect_lane(request)
        assert (
            _git("rev-parse", "HEAD", cwd=Path(lane.integration_worktree))
            == lane.base_commit
        )
    runner.final_report_text = (
        "FINAL_REPORT_BEGIN\n"
        "Ticket：I_atlas_999 Foreign ticket\n"
        "状态：completed\n"
        "FINAL_REPORT_END\n"
    )
    with pytest.raises(CoordinatorRuntimeError, match="worker_final_report_invalid"):
        runtime.collect_lane(request)
    runner.final_report_text = _worker_final_report(
        worktree=lane.execution_worktree,
        commit=execution_commit,
        review="failed",
    )
    with pytest.raises(CoordinatorRuntimeError, match="worker_final_report_negative"):
        runtime.collect_lane(request)
    runner.final_report_text = _worker_final_report(
        worktree=lane.execution_worktree,
        commit=execution_commit,
        status="failed",
        touched_files="delivery.txt FINAL_REPORT_BEGIN",
    )
    with pytest.raises(CoordinatorRuntimeError, match="worker_final_report_negative"):
        runtime.collect_lane(request)
    runner.final_report_text = _worker_final_report(
        worktree=lane.execution_worktree,
        commit=execution_commit,
    ).replace("Review：passed\n", "Review：failed\nReview：passed\n")
    with pytest.raises(CoordinatorRuntimeError, match="worker_final_report_negative"):
        runtime.collect_lane(request)
    runner.final_report_text = _worker_final_report(
        worktree=lane.execution_worktree,
        commit=execution_commit,
        ticket=("I_atlas_999 Foreign ticket https://github.com/acme/atlas/issues/999"),
    )
    with pytest.raises(CoordinatorRuntimeError, match="worker_final_report_invalid"):
        runtime.collect_lane(request)
    runner.final_report_text = _worker_final_report(
        worktree=lane.execution_worktree,
        commit="0" * 40,
    )
    with pytest.raises(CoordinatorRuntimeError, match="worker_final_report_invalid"):
        runtime.collect_lane(request)
    blocked_report = _worker_final_report(
        worktree=lane.execution_worktree,
        status="blocked",
        checks="blocked",
        review="blocked",
        touched_files="none",
        blocker="dependency named FINAL_REPORT_BEGIN is unavailable",
    )
    runner.final_report_text = blocked_report
    blocked = runtime.collect_lane(request)
    assert blocked["state"] == "blocked"
    assert blocked["blocker"]["reason"] == "worker_reported_blocker"
    assert (
        _git("rev-parse", "HEAD", cwd=Path(lane.integration_worktree))
        == lane.base_commit
    )
    blocked_registry = DeliveryLaneRegistry.from_payload(blocked["blocked_registry"])
    prompt_count = sum(call[3:5] == ("agent", "prompt") for call in runner.calls)
    resumed = runtime.dispatch_lane(replace(request, registry=blocked_registry))
    assert sum(call[3:5] == ("agent", "prompt") for call in runner.calls) == (
        prompt_count + 1
    )
    resume_prompt = [call for call in runner.calls if call[3:5] == ("agent", "prompt")][
        -1
    ]
    assert json.loads(resume_prompt[6])["resume"]["authority"] == (
        "map-returned-to-delivery"
    )
    request = replace(
        request,
        registry=DeliveryLaneRegistry.from_payload(resumed["registry"]),
    )
    completed_report = _worker_final_report(
        worktree=lane.execution_worktree,
        commit=f"{execution_commit} local delivery",
    )
    for latest_unclosed_negative in (
        "状态：failed\n",
        "状态：blocked\nBlocker：new dependency unavailable\n",
    ):
        runner.final_report_text = (
            completed_report
            + "trailing retry output\nFINAL_REPORT_BEGIN\n"
            + latest_unclosed_negative
        )
        with pytest.raises(
            CoordinatorRuntimeError,
            match="worker_final_report_negative",
        ):
            runtime.collect_lane(request)
        assert (
            _git("rev-parse", "HEAD", cwd=Path(lane.integration_worktree))
            == lane.base_commit
        )
    for incomplete_report in (
        completed_report.replace("Review：passed", "Review："),
        completed_report + "trailing output\nFINAL_REPORT_BEGIN\n",
    ):
        runner.final_report_text = incomplete_report
        with pytest.raises(
            CoordinatorRuntimeError,
            match="worker_final_report_incomplete",
        ):
            runtime.collect_lane(request)
        assert (
            _git("rev-parse", "HEAD", cwd=Path(lane.integration_worktree))
            == lane.base_commit
        )
    runner.final_report_text = blocked_report + completed_report
    first = runtime.collect_lane(request)
    latest_failed_report = completed_report.replace("状态：completed", "状态：failed")
    runner.final_report_text = completed_report + latest_failed_report
    with pytest.raises(CoordinatorRuntimeError, match="worker_final_report_negative"):
        runtime.collect_lane(request)
    runner.final_report_text = "worker exited without the required report markers"
    missing_report = runtime.collect_lane(request)
    runner.final_report_text = completed_report
    runner.report_truncated = True
    truncated_report = runtime.collect_lane(request)
    runner.report_truncated = False
    runner.session_available = False
    second = runtime.collect_lane(request)
    runner.session_available = True
    runner.workspace_available = False
    missing_workspace = runtime.collect_lane(request)
    runner.workspace_available = True
    runner.root_pane_available = False
    runner.worker_terminal = False
    with pytest.raises(CoordinatorRuntimeError, match="worker_not_terminal"):
        runtime.collect_lane(request)
    runner.worker_terminal = True
    terminal_without_root_pane = runtime.collect_lane(request)
    runner.root_pane_available = True
    runner.root_agent_available = False
    runner.worker_terminal = False
    with pytest.raises(CoordinatorRuntimeError, match="worker_not_terminal"):
        runtime.collect_lane(request)
    runner.worker_terminal = True
    terminal_without_root_agent = runtime.collect_lane(request)
    runner.delivery_pane_available = False
    runner.worker_available = False
    missing_pane_and_worker = runtime.collect_lane(request)
    runner.delivery_pane_available = True
    runner.worker_available = True
    runner.root_agent_available = True
    runner.foreign_occupant = True
    with pytest.raises(CoordinatorRuntimeError, match="overlapping_lane_ownership"):
        runtime.collect_lane(request)
    runner.worker_available = False
    with pytest.raises(CoordinatorRuntimeError, match="overlapping_lane_ownership"):
        runtime.collect_lane(request)
    runner.worker_available = True
    runner.foreign_occupant = False

    integration = Path(lane.integration_worktree)
    integration_commit = _git("rev-parse", "HEAD", cwd=integration)
    assert first["state"] == second["state"] == "locally_validated"
    assert first["dispatch_id"] == dispatched["dispatch_id"]
    assert first["evidence"] == {
        "execution_commit": execution_commit,
        "integration_commit": integration_commit,
        "final_report": {
            "status": "confirmed",
            "reported_commit": execution_commit,
            "reported_ticket": (
                "I_atlas_42 Add the deterministic delivery proof "
                "https://github.com/acme/atlas/issues/42"
            ),
            "reported_pane": (f"wA:p2 {lane.execution_worktree} codex/issue-42"),
            "digest": first["evidence"]["final_report"]["digest"],
            "revision": 7,
        },
        "validation": "passed",
        "completion_contract": "satisfied",
    }
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["evidence"]["final_report"]["status"] == "recovered_from_git"
    assert any("transport cache" in item for item in second["limitations"])
    for recovered in (
        missing_report,
        truncated_report,
        missing_workspace,
        missing_pane_and_worker,
    ):
        assert recovered["state"] == "locally_validated"
        assert recovered["evidence"]["final_report"]["status"] == "recovered_from_git"
        assert any("transport cache" in item for item in recovered["limitations"])
    for terminal in (terminal_without_root_pane, terminal_without_root_agent):
        assert terminal["state"] == "locally_validated"
        assert terminal["evidence"]["final_report"]["status"] == "confirmed"
    assert first["acceptance_recommendation"] == "accept"
    assert first["limitations"] == [
        "Remote publication still requires chairman authority.",
        "Push, PR, merge, release, and Issue closure were not performed.",
    ]
    assert (integration / "delivery.txt").read_text(encoding="utf-8") == (
        "locally delivered\n"
    )
    assert (
        _git("rev-list", "--count", lane.base_commit + "..HEAD", cwd=integration) == "1"
    )
    (integration / "unrelated.txt").write_text("other writer\n", encoding="utf-8")
    _git("add", "unrelated.txt", cwd=integration)
    _git("commit", "-m", "unrelated integration writer", cwd=integration)
    with pytest.raises(
        CoordinatorRuntimeError,
        match="overlapping_integration_ownership",
    ):
        runtime.collect_lane(request)


@pytest.mark.parametrize("interleaved_writer", [None, "commit", "branch"])
def test_isolated_e2e_dispatches_integrates_validates_and_recommends_acceptance(
    tmp_path,
    monkeypatch,
    interleaved_writer,
):
    context, raw_lane = _delivery_git_lane(tmp_path)
    lane = replace(
        raw_lane,
        validation_argv=("git", "diff", "--check", "HEAD^", "HEAD"),
        known_limitations=("Remote publication remains separately governed.",),
    )
    storage_root = tmp_path / "plugin-data"
    storage = PluginStorage(storage_root)
    lifecycle = storage.coordinator_lifecycle(created_at="2026-08-24T00:00:00Z")
    namespace = CoordinatorRuntime.session_namespace(lifecycle)
    label = CoordinatorRuntime.workspace_label(lifecycle, MAP_ID)
    root_agent = CoordinatorRuntime.agent_name(lifecycle, MAP_ID)
    storage.reserve_pm_runtime(
        map_id=MAP_ID,
        session_namespace=namespace,
        workspace_label=label,
        agent_id=root_agent,
        ownership_marker=CoordinatorRuntime.ownership_marker(lifecycle, MAP_ID),
        lifecycle_id=lifecycle,
        context=context,
        updated_at="2026-08-24T00:00:00Z",
        session_ownership_marker=CoordinatorRuntime.session_ownership_marker(lifecycle),
    )
    storage.update_pm_runtime(
        map_id=MAP_ID,
        state="active",
        workspace_id="wA",
        window_id="wA:t1",
        pane_id="wA:p1",
        agent_session_id="pm-session-atlas",
        ready_record_id="ready-1",
        updated_at="2026-08-24T00:00:01Z",
    )
    worker_packets = []

    def complete_worker(packet):
        worker_packets.append(packet)
        assert [record.registry.state for record in tracker.registries] == ["created"]
        execution = Path(lane.execution_worktree)
        (execution / "delivery.txt").write_text(
            "delivered by fake codex worker\n", encoding="utf-8"
        )
        _git("add", "delivery.txt", cwd=execution)
        _git("commit", "-m", "deliver ticket 41", cwd=execution)

    runner = HerdrDispatchRunner(
        namespace=namespace,
        workspace_label=label,
        root_agent_name=root_agent,
        repository_path=context.repository_path,
        execution_path=lane.execution_worktree,
        on_prompt=complete_worker,
    )
    runtime = CoordinatorRuntime(
        storage=storage,
        runner=runner,
        clock=lambda: "2026-08-24T00:00:02Z",
    )
    tracker = DeliveryTracker()
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        profile_name="pm",
        clock=lambda: datetime(2026, 8, 24, 0, 0, 5, tzinfo=timezone.utc),
        commissioning_prerequisites=StaticDeliveryPrerequisites(context),
        coordinator_runtime=runtime,
    )
    project = application.configure_project(project_url=PROJECT_URL)
    application.bind_map(project_id=project["id"], issue_url=ISSUE_URL)
    application.assign_pm(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id=lifecycle,
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id=lifecycle,
        turn_id="checkpoint-turn-41",
    )
    checkpoint = application.report_pm(
        request_identity=PM_IDENTITY,
        report=PMReportDraft(
            record_id="delivery-checkpoint-41",
            report_type="checkpoint",
            summary="One ready implementation outcome is entering local delivery.",
            timestamp="2026-08-24T00:00:00Z",
        ),
    )
    assert checkpoint["coordinator"]["state"] == "idle"
    assert (
        application.board()["maps"][0]["delivery_summary"]["latest"]["type"]
        == "checkpoint"
    )
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id=lifecycle,
        turn_id="dispatch-turn-41",
    )

    dispatched = application.dispatch_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )
    assert dispatched["state"] == "dispatched"
    assert dispatched["coordinator"]["state"] == "idle"
    application.begin_pm_turn(
        map_id=MAP_ID,
        request_identity=PM_IDENTITY,
        coordinator_id=lifecycle,
        turn_id="collect-turn-41",
    )
    if interleaved_writer:
        original_local_result = CoordinatorRuntime._local_result
        injected = False

        def local_result_with_interleaved_writer(arguments, *, cwd=None):
            nonlocal injected
            cherry_pick = list(arguments[:4]) == [
                "git",
                "-C",
                lane.integration_worktree,
                "cherry-pick",
            ]
            validation = tuple(arguments) == lane.validation_argv and (
                cwd == lane.integration_worktree
            )
            if not injected and interleaved_writer == "commit" and cherry_pick:
                injected = True
                integration = Path(lane.integration_worktree)
                (integration / "foreign.txt").write_text(
                    "concurrent integration writer\n", encoding="utf-8"
                )
                _git("add", "foreign.txt", cwd=integration)
                _git("commit", "-m", "foreign integration write", cwd=integration)
            result = original_local_result(arguments, cwd=cwd)
            if not injected and interleaved_writer == "branch" and validation:
                injected = True
                _git(
                    "switch",
                    "-c",
                    "foreign/post-validation",
                    cwd=Path(lane.integration_worktree),
                )
            return result

        monkeypatch.setattr(
            CoordinatorRuntime,
            "_local_result",
            staticmethod(local_result_with_interleaved_writer),
        )
        with pytest.raises(
            CoordinatorRuntimeError,
            match="overlapping_integration_ownership",
        ):
            application.collect_pm_delivery_lane(
                request_identity=PM_IDENTITY,
                lane=lane,
            )
        assert injected is True
        expected_commit_count = "2" if interleaved_writer == "commit" else "1"
        assert (
            _git(
                "rev-list",
                "--count",
                f"{lane.base_commit}..HEAD",
                cwd=Path(lane.integration_worktree),
            )
            == expected_commit_count
        )
        assert [record.registry.state for record in tracker.registries] == [
            "created",
            "running",
        ]
        assert [record.report.content.report_type for record in tracker.reports] == [
            "checkpoint"
        ]
        return
    accepted = application.collect_pm_delivery_lane(
        request_identity=PM_IDENTITY,
        lane=lane,
    )

    assert len(worker_packets) == 1
    assert worker_packets[0]["lane"] == {
        "id": "implementation-42",
        "runtime": "herdr-codex-pane",
    }
    assert accepted["state"] == "locally_validated"
    assert accepted["acceptance_recommendation"] == "accept"
    assert accepted["evidence"]["final_report"]["status"] == "confirmed"
    assert accepted["coordinator"]["state"] == "idle"
    assert tracker.issue.labels == ("map", "map-stage/acceptance")
    assert [record.registry.state for record in tracker.registries] == [
        "created",
        "running",
        "terminal",
        "integrated",
    ]
    assert [record.report.content.report_type for record in tracker.reports] == [
        "checkpoint",
        "acceptance",
    ]
    board = application.board()
    assert len(board["maps"]) == 1
    assert board["maps"][0]["stage"] == "acceptance"
    assert board["maps"][0]["delivery_summary"]["badges"] == [
        {"type": "acceptance_request", "count": 1}
    ]
    assert board["maps"][0]["delivery_summary"]["count"] == 2
    board_text = json.dumps(board, sort_keys=True)
    assert lane.ticket_title not in board_text
    assert lane.execution_worktree not in board_text
    assert accepted["evidence"]["execution_commit"] not in board_text
    integration = Path(lane.integration_worktree)
    assert (integration / "delivery.txt").read_text(encoding="utf-8") == (
        "delivered by fake codex worker\n"
    )
    assert (
        _git("rev-list", "--count", lane.base_commit + "..HEAD", cwd=integration) == "1"
    )
    transport_argv = []
    for call in runner.calls:
        if call[3:5] == ("agent", "prompt"):
            transport_argv.append((*call[:6], *call[7:]))
        else:
            transport_argv.append(call)
    command_tokens = {str(token).lower() for call in transport_argv for token in call}
    for forbidden in ("push", "pull", "gh", "release", "close"):
        assert forbidden not in command_tokens
