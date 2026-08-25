from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from map_governance import (
    AcceptanceEvidence,
    ApprovalPacket,
    MapGovernanceApplication,
    PublicationAction,
    RemotePublicationEvidence,
)
from map_governance.approvals import ApprovalHistoryEvent
from map_governance.publication import PublicationRecord
from map_governance.reports import PMReportDraft, TrackerPMReportRecord
from map_governance.tracker import (
    TrackerApprovalRecord,
    TrackerError,
    TrackerIssue,
    TrackerPublicationRecord,
    TrackerProject,
)


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugin"
ACME_PROJECT_URL = "https://github.com/orgs/acme/projects/7"
OCTO_PROJECT_URL = "https://github.com/users/octocat/projects/3"
PUBLISHER_AUTHORITY = "gh:github.com:portfolio-publisher"


def _approval_history(
    packet: ApprovalPacket,
    *,
    issue_url: str,
    action_id: str,
) -> list[TrackerApprovalRecord]:
    events = (
        ApprovalHistoryEvent(
            event_id=f"approval:{packet.request_id}:request",
            request_id=packet.request_id,
            event_type="requested",
            occurred_at="2026-08-25T00:10:00Z",
            payload_hash=packet.payload_hash,
            details={**packet.payload(), "packet_hash": packet.packet_hash},
        ),
        ApprovalHistoryEvent(
            event_id=f"approval:{packet.request_id}:decision",
            request_id=packet.request_id,
            event_type="approved",
            occurred_at="2026-08-25T00:20:00Z",
            payload_hash=packet.payload_hash,
            details={
                "decision": "approved",
                "actor_id": "basic:chairman",
                "actor_profile": "ceo",
                "note": "Approve the exact governed action.",
                "expires_at": "2026-08-25T03:20:00Z",
            },
        ),
        ApprovalHistoryEvent(
            event_id=f"approval:{packet.request_id}:consumed:{action_id}",
            request_id=packet.request_id,
            event_type="consumed",
            occurred_at="2026-08-25T00:30:00Z",
            payload_hash=packet.payload_hash,
            details={
                "mutation_id": action_id,
                "action": packet.proposed_action,
                "actor_id": "basic:chairman",
                "actor_profile": "ceo",
                "note": "Approved action consumed.",
            },
        ),
    )
    return [
        TrackerApprovalRecord(
            event=event,
            tracker_record_id=f"IC_{event.event_id}",
            tracker_record_url=f"{issue_url}#issuecomment-{index}",
        )
        for index, event in enumerate(events, start=1)
    ]


class IsolatedPortfolioTracker:
    """Credential-scoped fake that rejects every cross-project resource."""

    def __init__(
        self,
        delegate: PortfolioTracker,
        *,
        project_url: str,
        repository_owner: str,
    ) -> None:
        self.delegate = delegate
        self.project_url = project_url
        self.repository_owner = repository_owner
        self.credential_ref = f"fake:{repository_owner}"
        self.accessible = True
        self.resource_urls: list[str] = []

    def _check(self, url: str) -> None:
        if url != self.project_url and not url.startswith(
            f"https://github.com/{self.repository_owner}/"
        ):
            raise AssertionError("project tracker received another project's resource")
        self.resource_urls.append(url)
        if not self.accessible:
            raise TrackerError("403 project credential access lost")

    def __getattr__(self, name):
        operation = getattr(self.delegate, name)

        def isolated(url, *args, **kwargs):
            self._check(url)
            return operation(url, *args, **kwargs)

        return isolated


class PortfolioTracker:
    def __init__(self) -> None:
        self.projects = {
            ACME_PROJECT_URL: TrackerProject(
                id="PVT_acme_7",
                owner="acme",
                owner_type="organization",
                number=7,
                title="Acme portfolio",
                url=ACME_PROJECT_URL,
            ),
            OCTO_PROJECT_URL: TrackerProject(
                id="PVT_octocat_3",
                owner="octocat",
                owner_type="user",
                number=3,
                title="Octocat portfolio",
                url=OCTO_PROJECT_URL,
            ),
        }
        self.issues = {
            "https://github.com/acme/atlas/issues/41": TrackerIssue(
                id="I_acme_41",
                repository="acme/atlas",
                number=41,
                title="Colliding launch title",
                url="https://github.com/acme/atlas/issues/41",
                state="open",
                state_reason=None,
                labels=("map", "map-stage/awaiting-approval"),
            ),
            "https://github.com/acme/atlas/issues/42": TrackerIssue(
                id="I_acme_42",
                repository="acme/atlas",
                number=42,
                title="Blocked delivery",
                url="https://github.com/acme/atlas/issues/42",
                state="open",
                state_reason=None,
                labels=("map", "map-stage/decision"),
            ),
            "https://github.com/acme/atlas/issues/43": TrackerIssue(
                id="I_acme_43",
                repository="acme/atlas",
                number=43,
                title="Ready for acceptance",
                url="https://github.com/acme/atlas/issues/43",
                state="open",
                state_reason=None,
                labels=("map", "map-stage/acceptance"),
            ),
            "https://github.com/acme/atlas/issues/44": TrackerIssue(
                id="I_acme_44",
                repository="acme/atlas",
                number=44,
                title="Delivered outcome",
                url="https://github.com/acme/atlas/issues/44",
                state="closed",
                state_reason="completed",
                labels=("map",),
            ),
            "https://github.com/acme/atlas/issues/45": TrackerIssue(
                id="I_acme_45",
                repository="acme/atlas",
                number=45,
                title="Cancelled outcome",
                url="https://github.com/acme/atlas/issues/45",
                state="closed",
                state_reason="not_planned",
                labels=("map",),
            ),
            "https://github.com/octocat/atlas/issues/41": TrackerIssue(
                id="I_octocat_41",
                repository="octocat/atlas",
                number=41,
                title="Colliding launch title",
                url="https://github.com/octocat/atlas/issues/41",
                state="open",
                state_reason=None,
                labels=("map", "map-stage/delivery"),
            ),
        }
        packet = ApprovalPacket(
            request_id="approval-acme-41",
            decision_class="delivery_authorization",
            proposed_action="transition_map",
            alternatives=("Authorize", "Revise"),
            rationale="Delivery needs chairman authority.",
            cost_risk="One engineering week.",
            evidence=("https://github.com/acme/atlas/issues/41",),
            requested_scope={"map_id": "I_acme_41"},
            decision_payload={
                "expected_stage": "awaiting-approval",
                "requested_stage": "authorized",
            },
        )
        self.approvals = {issue_url: [] for issue_url in self.issues}
        self.approvals["https://github.com/acme/atlas/issues/41"] = [
            TrackerApprovalRecord(
                event=ApprovalHistoryEvent(
                    event_id="approval:approval-acme-41:request",
                    request_id=packet.request_id,
                    event_type="requested",
                    occurred_at="2026-08-25T01:00:00Z",
                    payload_hash=packet.payload_hash,
                    details={**packet.payload(), "packet_hash": packet.packet_hash},
                ),
                tracker_record_id="IC_approval_acme_41",
                tracker_record_url=(
                    "https://github.com/acme/atlas/issues/41#issuecomment-1"
                ),
            )
        ]
        publication_action = PublicationAction(
            action="push",
            target={"repository": "acme/atlas", "ref": "refs/heads/main"},
        )
        acceptance = AcceptanceEvidence(
            revision="a" * 40,
            delivered_scope=("Portfolio terminal outcome fixture",),
            validations=("Deterministic acceptance evidence verified",),
            known_limitations=(),
            rollback_considerations=("Revert the exact published revision",),
            requested_publication_action=publication_action,
        )
        publication_packet = ApprovalPacket(
            request_id="publish-acme-44",
            decision_class="remote_publication",
            proposed_action="publish_map",
            alternatives=("Publish", "Request changes"),
            rationale="The accepted revision is ready for publication.",
            cost_risk="Remote publication changes the declared target.",
            evidence=("https://github.com/acme/atlas/issues/44#issuecomment-1",),
            requested_scope={
                "map_id": "I_acme_44",
                "publication_target": publication_action.target,
                "publisher_authority_ref": PUBLISHER_AUTHORITY,
            },
            decision_payload={
                "revision": acceptance.revision,
                "publication_action": publication_action.action,
                "publication_target": publication_action.target,
                "publisher_authority_ref": PUBLISHER_AUTHORITY,
                "acceptance_evidence_hash": acceptance.content_hash,
                "acceptance_report_id": "acceptance-acme-44",
            },
        )
        self.approvals["https://github.com/acme/atlas/issues/44"] = _approval_history(
            publication_packet,
            issue_url="https://github.com/acme/atlas/issues/44",
            action_id="publish-action-acme-44",
        )[:2]
        cancellation_packet = ApprovalPacket(
            request_id="cancel-acme-45",
            decision_class="cancellation",
            proposed_action="cancel_map",
            alternatives=("Cancel as not planned", "Keep open"),
            rationale="The initiative is no longer planned.",
            cost_risk="Cancellation closes the governance Issue.",
            evidence=("https://github.com/acme/atlas/issues/45",),
            requested_scope={"map_id": "I_acme_45"},
            decision_payload={"state_reason": "not_planned"},
        )
        self.approvals["https://github.com/acme/atlas/issues/45"] = _approval_history(
            cancellation_packet,
            issue_url="https://github.com/acme/atlas/issues/45",
            action_id="cancel-action-acme-45",
        )
        self.reports = {issue_url: [] for issue_url in self.issues}
        self.reports["https://github.com/acme/atlas/issues/42"] = [
            TrackerPMReportRecord(
                report=PMReportDraft(
                    record_id="blocker-acme-42",
                    report_type="blocker",
                    summary="The whole Map needs an executive decision.",
                    timestamp="2026-08-25T01:10:00Z",
                    blocking=True,
                    continuation_requirement="Choose the supported launch region.",
                ).assign_to("I_acme_42"),
                tracker_record_id="IC_blocker_acme_42",
                tracker_record_url=(
                    "https://github.com/acme/atlas/issues/42#issuecomment-1"
                ),
            )
        ]
        self.reports["https://github.com/acme/atlas/issues/44"] = [
            TrackerPMReportRecord(
                report=PMReportDraft(
                    record_id="acceptance-acme-44",
                    report_type="acceptance",
                    summary="The exact local revision is accepted.",
                    timestamp="2026-08-25T00:05:00Z",
                    evidence=("All declared local outcomes passed.",),
                    acceptance=acceptance,
                ).assign_to("I_acme_44"),
                tracker_record_id="IC_acceptance_acme_44",
                tracker_record_url=(
                    "https://github.com/acme/atlas/issues/44#issuecomment-1"
                ),
            )
        ]
        self.reports["https://github.com/octocat/atlas/issues/41"] = [
            TrackerPMReportRecord(
                report=PMReportDraft(
                    record_id="failure-octocat-41",
                    report_type="failure",
                    summary="Delivery ended without acceptance evidence.",
                    timestamp="2026-08-25T01:20:00Z",
                    failure_code="invalid-delivery-contract",
                ).assign_to("I_octocat_41"),
                tracker_record_id="IC_failure_octocat_41",
                tracker_record_url=(
                    "https://github.com/octocat/atlas/issues/41#issuecomment-1"
                ),
            )
        ]
        self.reads = 0
        self.writes = 0
        self.failed_project_urls: set[str] = set()
        evidence = RemotePublicationEvidence(
            action_id="publish-action-acme-44",
            revision=acceptance.revision,
            action=publication_action.action,
            target=publication_action.target,
            provider="github",
            remote_id="acme/atlas@main",
            remote_url="https://github.com/acme/atlas/commit/" + acceptance.revision,
            published_at="2026-08-25T00:30:00Z",
        )
        self.publications = {issue_url: [] for issue_url in self.issues}
        self.publications["https://github.com/acme/atlas/issues/44"] = [
            TrackerPublicationRecord(
                record=PublicationRecord(
                    record_id="publication-acme-44",
                    action_id=evidence.action_id,
                    map_id="I_acme_44",
                    approval_request_id=publication_packet.request_id,
                    status="succeeded",
                    revision=evidence.revision,
                    action=evidence.action,
                    target=evidence.target,
                    occurred_at=evidence.published_at,
                    evidence=evidence,
                ),
                tracker_record_id="IC_publication_acme_44",
                tracker_record_url=(
                    "https://github.com/acme/atlas/issues/44#issuecomment-2"
                ),
            )
        ]

    def get_project(self, url: str) -> TrackerProject:
        self.reads += 1
        if url in self.failed_project_urls:
            raise TrackerError("403 credential access lost")
        return self.projects[url]

    def get_issue(self, url: str) -> TrackerIssue:
        self.reads += 1
        return self.issues[url]

    def list_decisions(self, url: str):
        self.reads += 1
        return []

    def list_approval_events(self, url: str):
        self.reads += 1
        return list(self.approvals[url])

    def list_pm_reports(self, url: str):
        self.reads += 1
        return list(self.reports[url])

    def list_publication_records(self, url: str):
        self.reads += 1
        return list(self.publications[url])

    def transition_issue_stage(self, url, *, expected_stage, requested_stage):
        self.writes += 1
        issue = self.issues[url]
        self.issues[url] = replace(
            issue,
            labels=("map", f"map-stage/{requested_stage}"),
        )
        return self.issues[url]


def _application(
    tmp_path,
    tracker: PortfolioTracker,
    *,
    tracker_for_project=None,
) -> MapGovernanceApplication:
    return MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data" / "map-governance",
        tracker=tracker,
        tracker_for_project=tracker_for_project,
        clock=lambda: datetime(2026, 8, 25, 2, 0, tzinfo=timezone.utc),
    )


def _configured_application(tmp_path, *, isolated_trackers: bool = False):
    tracker = PortfolioTracker()
    project_trackers = {
        ACME_PROJECT_URL: IsolatedPortfolioTracker(
            tracker,
            project_url=ACME_PROJECT_URL,
            repository_owner="acme",
        ),
        OCTO_PROJECT_URL: IsolatedPortfolioTracker(
            tracker,
            project_url=OCTO_PROJECT_URL,
            repository_owner="octocat",
        ),
    }
    application = _application(
        tmp_path,
        tracker,
        tracker_for_project=(
            project_trackers.__getitem__ if isolated_trackers else None
        ),
    )
    projects = {
        project["id"]: project
        for project in (
            application.configure_project(project_url=ACME_PROJECT_URL),
            application.configure_project(project_url=OCTO_PROJECT_URL),
        )
    }
    for issue_url in tracker.issues:
        project_id = "PVT_acme_7" if "/acme/" in issue_url else "PVT_octocat_3"
        application.bind_map(project_id=project_id, issue_url=issue_url)
    return application, tracker, projects, project_trackers


def test_portfolio_groups_current_projections_and_summarizes_executive_pressure(
    tmp_path,
):
    application, tracker, _projects, _project_trackers = _configured_application(
        tmp_path
    )
    reads_before = tracker.reads

    portfolio = application.portfolio()

    assert tracker.reads == reads_before
    assert tracker.writes == 0
    groups = {project["id"]: project for project in portfolio["projects"]}
    assert groups["PVT_acme_7"]["summary"] == {
        "map_count": 5,
        "stage_counts": {
            "acceptance": 1,
            "awaiting-approval": 1,
            "cancelled": 1,
            "decision": 1,
            "done": 1,
        },
        "awaiting_approvals": {"map_count": 1, "request_count": 1},
        "blocking_decisions": 1,
        "stale_maps": 0,
        "acceptance_readiness": 1,
        "terminal_outcomes": {"done": 1, "cancelled": 1, "total": 2},
        "health_counts": {"blocked": 1, "healthy": 4},
    }
    assert groups["PVT_octocat_3"]["summary"]["health_counts"] == {"needs_attention": 1}
    assert portfolio["summary"]["project_count"] == 2
    assert portfolio["summary"]["map_count"] == 6
    assert portfolio["summary"]["stale_project_count"] == 0
    assert portfolio["read_only"] is True
    assert portfolio["capabilities"] == {
        "open_map_detail": True,
        "open_ceo_session": True,
        "mutations": [],
    }


def test_portfolio_filters_are_server_authoritative_and_never_touch_tracker_state(
    tmp_path,
):
    application, tracker, _projects, _project_trackers = _configured_application(
        tmp_path
    )
    reads_before = tracker.reads

    filtered = application.portfolio(
        project_id="PVT_acme_7",
        stage="awaiting-approval",
        approval_need=True,
        health="healthy",
        stale=False,
    )

    assert [item["map_id"] for item in filtered["items"]] == ["I_acme_41"]
    assert [project["id"] for project in filtered["projects"]] == ["PVT_acme_7"]
    assert filtered["filters"] == {
        "project_id": "PVT_acme_7",
        "stage": "awaiting-approval",
        "approval_need": True,
        "health": "healthy",
        "stale": False,
    }
    assert tracker.reads == reads_before
    assert tracker.writes == 0
    assert [
        item["map_id"] for item in application.portfolio(stage="acceptance")["items"]
    ] == ["I_acme_43"]
    assert [
        item["map_id"] for item in application.portfolio(health="blocked")["items"]
    ] == ["I_acme_42"]
    assert {
        item["map_id"] for item in application.portfolio(approval_need=False)["items"]
    } == {
        "I_acme_42",
        "I_acme_43",
        "I_acme_44",
        "I_acme_45",
        "I_octocat_41",
    }
    assert tracker.reads == reads_before
    assert tracker.writes == 0


def test_approval_need_includes_awaiting_approval_without_a_pending_packet(tmp_path):
    application, tracker, _projects, _project_trackers = _configured_application(
        tmp_path
    )
    issue_url = "https://github.com/acme/atlas/issues/41"
    requested = tracker.approvals[issue_url][0].event
    tracker.approvals[issue_url].append(
        TrackerApprovalRecord(
            event=ApprovalHistoryEvent(
                event_id="approval:approval-acme-41:decision",
                request_id=requested.request_id,
                event_type="rejected",
                occurred_at="2026-08-25T01:30:00Z",
                payload_hash=requested.payload_hash,
                details={
                    "decision": "rejected",
                    "actor_id": "basic:chairman",
                    "actor_profile": "ceo",
                    "note": "Revise the authorization packet.",
                    "expires_at": None,
                },
            ),
            tracker_record_id="IC_approval_acme_41_rejected",
            tracker_record_url=f"{issue_url}#issuecomment-2",
        )
    )

    application.refresh(project_id="PVT_acme_7")
    item = next(
        item
        for item in application.portfolio(approval_need=True)["items"]
        if item["map_id"] == "I_acme_41"
    )

    assert item["stage"] == "awaiting-approval"
    assert item["pending_approval_count"] == 0
    assert item["approval_need"] is True


def test_one_project_access_loss_marks_only_its_portfolio_group_stale(tmp_path):
    application, tracker, _projects, _project_trackers = _configured_application(
        tmp_path
    )
    tracker.failed_project_urls.add(OCTO_PROJECT_URL)

    application.refresh()
    portfolio = application.portfolio()

    groups = {project["id"]: project for project in portfolio["projects"]}
    assert groups["PVT_octocat_3"]["stale"] is True
    assert groups["PVT_octocat_3"]["authority"]["reason"] == (
        "Tracker authority authentication failed"
    )
    assert groups["PVT_octocat_3"]["summary"]["stale_maps"] == 1
    assert groups["PVT_acme_7"]["stale"] is False
    assert len(groups["PVT_acme_7"]["items"]) == 5
    assert portfolio["summary"]["stale_project_count"] == 1
    stale_only = application.portfolio(stale=True)
    assert [project["id"] for project in stale_only["projects"]] == ["PVT_octocat_3"]
    assert [item["map_id"] for item in stale_only["items"]] == ["I_octocat_41"]


def test_project_scoped_trackers_never_cross_credentials_or_failure_boundaries(
    tmp_path,
):
    application, _tracker, _projects, project_trackers = _configured_application(
        tmp_path,
        isolated_trackers=True,
    )
    acme = project_trackers[ACME_PROJECT_URL]
    octocat = project_trackers[OCTO_PROJECT_URL]

    assert acme.credential_ref != octocat.credential_ref
    assert acme is not octocat
    assert all("/octocat/" not in url for url in acme.resource_urls)
    assert all("/acme/" not in url for url in octocat.resource_urls)

    octocat.accessible = False
    application.refresh()
    groups = {group["id"]: group for group in application.portfolio()["projects"]}

    assert groups["PVT_acme_7"]["stale"] is False
    assert groups["PVT_octocat_3"]["stale"] is True
    assert len(groups["PVT_acme_7"]["items"]) == 5
    assert all("/octocat/" not in url for url in acme.resource_urls)
    assert all("/acme/" not in url for url in octocat.resource_urls)
    visible = json.dumps(application.portfolio(), sort_keys=True)
    assert acme.credential_ref not in visible
    assert octocat.credential_ref not in visible


def test_colliding_titles_and_issue_numbers_keep_canonical_navigation_isolated(
    tmp_path,
):
    application, _tracker, _projects, _project_trackers = _configured_application(
        tmp_path
    )

    colliding = [
        item
        for item in application.portfolio()["items"]
        if item["title"] == "Colliding launch title"
    ]

    assert {item["map_id"] for item in colliding} == {
        "I_acme_41",
        "I_octocat_41",
    }
    assert {item["tracker"]["identity"] for item in colliding} == {
        "acme/atlas#41",
        "octocat/atlas#41",
    }
    assert {item["project"]["id"] for item in colliding} == {
        "PVT_acme_7",
        "PVT_octocat_3",
    }
    for item in colliding:
        assert item["navigation"] == {
            "map_detail": {"map_id": item["map_id"]},
            "ceo_session": {"map_id": item["map_id"]},
        }
        assert application.map_detail(map_id=item["map_id"])["id"] == item["map_id"]
        assert "available_transitions" not in item
        assert "runtime" not in item
        assert "credentials" not in item
        assert "approvals" not in item
    by_map = {item["map_id"]: item for item in colliding}
    assert by_map["I_acme_41"]["approval_need"] is True
    assert by_map["I_acme_41"]["health"]["state"] == "healthy"
    assert by_map["I_octocat_41"]["approval_need"] is False
    assert by_map["I_octocat_41"]["health"]["state"] == "needs_attention"
    assert (
        by_map["I_octocat_41"]["delivery_summary"]["latest"]["assignment_map_id"]
        == "I_octocat_41"
    )


def test_portfolio_rebuild_from_bindings_and_tracker_truth_is_visibly_equivalent(
    tmp_path,
):
    application, _tracker, _projects, _project_trackers = _configured_application(
        tmp_path
    )
    before = application.portfolio()
    restarted = _application(tmp_path, _tracker)
    assert restarted.portfolio() == before
    with ThreadPoolExecutor(max_workers=4) as workers:
        concurrent = list(workers.map(lambda _index: restarted.portfolio(), range(8)))
    assert concurrent == [before] * 8
    rebuilt = _application(tmp_path / "rebuilt", _tracker)
    projects = {
        project["id"]: project
        for project in (
            rebuilt.configure_project(project_url=ACME_PROJECT_URL),
            rebuilt.configure_project(project_url=OCTO_PROJECT_URL),
        )
    }
    for issue_url in _tracker.issues:
        project_id = "PVT_acme_7" if "/acme/" in issue_url else "PVT_octocat_3"
        rebuilt.bind_map(project_id=projects[project_id]["id"], issue_url=issue_url)

    assert rebuilt.portfolio() == before
