from __future__ import annotations

import json

import pytest

from map_governance.approvals import ApprovalHistoryEvent
from map_governance.tracker import (
    GitHubTrackerAdapter,
    StructuredDecision,
    TrackerConflictError,
    TrackerError,
    TrackerIssue,
    TrackerProject,
)


class ScriptedRunner:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def run(self, arguments):
        self.calls.append(list(arguments))
        return json.dumps(self.payloads.pop(0))


def _issue_resource(*, stage="authorized"):
    return {
        "__typename": "Issue",
        "id": "I_atlas_41",
        "number": 41,
        "title": "Map the Atlas launch",
        "url": "https://github.com/acme/atlas/issues/41",
        "state": "OPEN",
        "stateReason": None,
        "repository": {"nameWithOwner": "acme/atlas"},
        "labels": {
            "nodes": [
                {"id": "L_map", "name": "map"},
                {"id": f"L_{stage}", "name": f"map-stage/{stage}"},
            ],
            "pageInfo": {"hasNextPage": False},
        },
    }


def test_real_github_adapter_contract_uses_read_only_graphql_boundary(
    fake_github_boundary, monkeypatch
):
    executable, log = fake_github_boundary
    monkeypatch.setenv("MAP_GOVERNANCE_FAKE_GH_LOG", str(log))
    adapter = GitHubTrackerAdapter(executable=str(executable))

    project = adapter.get_project("https://github.com/orgs/acme/projects/7")
    issue = adapter.get_issue("https://github.com/acme/atlas/issues/41")

    assert project == TrackerProject(
        id="PVT_acme_7",
        owner="acme",
        owner_type="organization",
        number=7,
        title="Acme CEO portfolio",
        url="https://github.com/orgs/acme/projects/7",
    )
    assert issue == TrackerIssue(
        id="I_atlas_41",
        repository="acme/atlas",
        number=41,
        title="Map the Atlas launch",
        url="https://github.com/acme/atlas/issues/41",
        state="open",
        state_reason=None,
        labels=("map", "map-stage/authorized"),
    )
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert all(call[:2] == ["api", "graphql"] for call in calls)
    assert all("mutation" not in " ".join(call).lower() for call in calls)
    assert [value for value in calls[0] if value.startswith(("owner=", "number="))] == [
        "owner=acme",
        "number=7",
    ]
    assert [value for value in calls[1] if value.startswith("url=")] == [
        "url=https://github.com/acme/atlas/issues/41"
    ]


def test_github_stage_transition_replaces_labels_atomically_then_reads_back():
    context = _issue_resource()
    context["repository"]["label"] = {
        "id": "L_delivery",
        "name": "map-stage/delivery",
    }
    mutation_issue = _issue_resource(stage="delivery")
    committed_issue = _issue_resource(stage="delivery")
    runner = ScriptedRunner(
        {"data": {"resource": context}},
        {"data": {"updateIssue": {"issue": mutation_issue}}},
        {"data": {"resource": committed_issue}},
    )
    adapter = GitHubTrackerAdapter(runner=runner)

    committed = adapter.transition_issue_stage(
        "https://github.com/acme/atlas/issues/41",
        expected_stage="authorized",
        requested_stage="delivery",
    )

    assert committed.labels == ("map", "map-stage/delivery")
    assert len(runner.calls) == 3
    mutation = runner.calls[1]
    assert "mutation MapGovernanceUpdateIssueStage" in next(
        value.removeprefix("query=") for value in mutation if value.startswith("query=")
    )
    assert [value for value in mutation if value.startswith("issue=")] == [
        "issue=I_atlas_41"
    ]
    assert [value for value in mutation if value.startswith("labels[]=")] == [
        "labels[]=L_map",
        "labels[]=L_delivery",
    ]
    assert all("L_authorized" not in value for value in mutation)


def test_github_stage_transition_detects_stale_expected_stage_before_write():
    context = _issue_resource(stage="parked")
    context["repository"]["label"] = {
        "id": "L_delivery",
        "name": "map-stage/delivery",
    }
    runner = ScriptedRunner({"data": {"resource": context}})
    adapter = GitHubTrackerAdapter(runner=runner)

    with pytest.raises(TrackerConflictError) as raised:
        adapter.transition_issue_stage(
            "https://github.com/acme/atlas/issues/41",
            expected_stage="authorized",
            requested_stage="delivery",
        )

    assert raised.value.current_stage == "parked"
    assert raised.value.requested_stage == "delivery"
    assert len(runner.calls) == 1


def test_github_stage_transition_detects_competing_write_during_readback():
    context = _issue_resource()
    context["repository"]["label"] = {
        "id": "L_delivery",
        "name": "map-stage/delivery",
    }
    runner = ScriptedRunner(
        {"data": {"resource": context}},
        {"data": {"updateIssue": {"issue": {"id": "I_atlas_41"}}}},
        {"data": {"resource": _issue_resource(stage="parked")}},
    )
    adapter = GitHubTrackerAdapter(runner=runner)

    with pytest.raises(TrackerConflictError) as raised:
        adapter.transition_issue_stage(
            "https://github.com/acme/atlas/issues/41",
            expected_stage="authorized",
            requested_stage="delivery",
        )

    assert raised.value.current_stage == "parked"
    assert len(runner.calls) == 3


def test_github_issue_read_refuses_truncated_labels_for_single_stage_proof():
    resource = _issue_resource()
    resource["labels"]["pageInfo"]["hasNextPage"] = True
    adapter = GitHubTrackerAdapter(
        runner=ScriptedRunner({"data": {"resource": resource}})
    )

    with pytest.raises(TrackerError, match="cannot prove one active stage"):
        adapter.get_issue("https://github.com/acme/atlas/issues/41")


def test_github_decision_write_builds_a_human_and_machine_readable_comment():
    decision = StructuredDecision(
        decision_id="decision-atlas-market-001",
        type="product",
        rationale="Launch to the research cohort before widening access.",
        authority="CEO within the authorized Map envelope",
        affected_stage="authorized",
        timestamp="2026-08-23T09:25:00Z",
        authority_context={
            "decision_payload": {"amount": 750, "currency": "USD"},
            "requested_scope": {"map_id": "I_atlas_41"},
        },
    )
    runner = ScriptedRunner(
        {
            "data": {
                "addComment": {
                    "commentEdge": {
                        "node": {
                            "id": "IC_1",
                            "url": f"{_issue_resource()['url']}#issuecomment-1",
                            "body": "placeholder",
                        }
                    }
                }
            }
        }
    )
    adapter = GitHubTrackerAdapter(runner=runner)

    # Return the body sent by the adapter as GitHub's committed comment body.
    def echo_mutation(arguments):
        runner.calls.append(list(arguments))
        body = next(
            value.removeprefix("body=")
            for value in arguments
            if value.startswith("body=")
        )
        return json.dumps(
            {
                "data": {
                    "addComment": {
                        "commentEdge": {
                            "node": {
                                "id": "IC_1",
                                "url": f"{_issue_resource()['url']}#issuecomment-1",
                                "body": body,
                            }
                        }
                    }
                }
            }
        )

    runner.run = echo_mutation
    committed = adapter.append_decision(
        _issue_resource()["url"],
        issue_id="I_atlas_41",
        decision=decision,
    )

    assert committed.decision == decision
    assert committed.tracker_record_id == "IC_1"
    mutation = runner.calls[0]
    query = next(
        value.removeprefix("query=") for value in mutation if value.startswith("query=")
    )
    body = next(
        value.removeprefix("body=") for value in mutation if value.startswith("body=")
    )
    assert "mutation MapGovernanceAppendDecision" in query
    assert "<!-- map-governance:decision:v1 " in body
    assert "## CEO decision · decision-atlas-market-001" in body
    assert "Authority context:" in body
    assert '"amount":750' in body
    assert "Launch to the research cohort before widening access." in body
    assert [value for value in mutation if value.startswith("subject=")] == [
        "subject=I_atlas_41"
    ]


def test_github_decision_history_reads_all_pages_and_ignores_ordinary_comments():
    first = StructuredDecision(
        decision_id="decision-001",
        type="product",
        rationale="First rationale.",
        authority="CEO",
        affected_stage="authorized",
        timestamp="2026-08-23T09:00:00Z",
    )
    second = StructuredDecision(
        decision_id="decision-002",
        type="operations",
        rationale="Second rationale.",
        authority="CEO",
        affected_stage="delivery",
        timestamp="2026-08-23T10:00:00Z",
    )

    def body(decision):
        marker = json.dumps(
            decision.payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"<!-- map-governance:decision:v1 {marker} -->\nHuman summary"

    runner = ScriptedRunner(
        {
            "data": {
                "resource": {
                    "__typename": "Issue",
                    "id": "I_atlas_41",
                    "comments": {
                        "nodes": [
                            {"id": "IC_plain", "url": "plain", "body": "hello"},
                            {"id": "IC_1", "url": "decision-1", "body": body(first)},
                        ],
                        "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                    },
                }
            }
        },
        {
            "data": {
                "resource": {
                    "__typename": "Issue",
                    "id": "I_atlas_41",
                    "comments": {
                        "nodes": [
                            {"id": "IC_2", "url": "decision-2", "body": body(second)}
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }
        },
    )

    records = GitHubTrackerAdapter(runner=runner).list_decisions(
        _issue_resource()["url"]
    )

    assert [record.decision for record in records] == [first, second]
    assert [record.tracker_record_id for record in records] == ["IC_1", "IC_2"]
    assert len(runner.calls) == 2
    assert not any(value.startswith("after=") for value in runner.calls[0])
    assert [value for value in runner.calls[1] if value.startswith("after=")] == [
        "after=cursor-1"
    ]


def test_github_approval_event_is_human_readable_and_round_trips_marker():
    event = ApprovalHistoryEvent(
        event_id="approval:approval-delivery-001:decision",
        request_id="approval-delivery-001",
        event_type="approved",
        occurred_at="2026-08-23T09:30:00Z",
        payload_hash="sha256:" + "a" * 64,
        details={
            "actor_id": "basic:chairman-1",
            "note": "Approved for the declared scope.",
            "expires_at": "2026-08-24T09:30:00Z",
        },
    )
    runner = ScriptedRunner()

    def echo_mutation(arguments):
        runner.calls.append(list(arguments))
        body = next(
            value.removeprefix("body=")
            for value in arguments
            if value.startswith("body=")
        )
        return json.dumps(
            {
                "data": {
                    "addComment": {
                        "commentEdge": {
                            "node": {
                                "id": "IC_approval_1",
                                "url": f"{_issue_resource()['url']}#issuecomment-approval-1",
                                "body": body,
                            }
                        }
                    }
                }
            }
        )

    runner.run = echo_mutation
    record = GitHubTrackerAdapter(runner=runner).append_approval_event(
        _issue_resource()["url"],
        issue_id="I_atlas_41",
        event=event,
    )

    assert record.event == event
    body = next(
        value.removeprefix("body=")
        for value in runner.calls[0]
        if value.startswith("body=")
    )
    assert "mutation MapGovernanceAppendApproval" in next(
        value.removeprefix("query=")
        for value in runner.calls[0]
        if value.startswith("query=")
    )
    assert "<!-- map-governance:approval:v1 " in body
    assert "## Chairman approved · approval-delivery-001" in body
    assert "Approved for the declared scope." in body


def test_github_approval_history_paginates_and_ignores_other_comments():
    first = ApprovalHistoryEvent(
        event_id="approval:approval-delivery-001:request",
        request_id="approval-delivery-001",
        event_type="requested",
        occurred_at="2026-08-23T09:00:00Z",
        payload_hash="sha256:" + "b" * 64,
        details={"rationale": "Ready for approval."},
    )
    second = ApprovalHistoryEvent(
        event_id="approval:approval-delivery-001:decision",
        request_id="approval-delivery-001",
        event_type="rejected",
        occurred_at="2026-08-23T09:30:00Z",
        payload_hash=first.payload_hash,
        details={"actor_id": "basic:chairman", "note": "Revise evidence."},
    )

    def body(event):
        marker = json.dumps(
            event.payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"<!-- map-governance:approval:v1 {marker} -->\nSummary"

    runner = ScriptedRunner(
        {
            "data": {
                "resource": {
                    "__typename": "Issue",
                    "comments": {
                        "nodes": [
                            {"id": "IC_plain", "url": "plain", "body": "ordinary"},
                            {"id": "IC_request", "url": "request", "body": body(first)},
                        ],
                        "pageInfo": {"hasNextPage": True, "endCursor": "cursor-a"},
                    },
                }
            }
        },
        {
            "data": {
                "resource": {
                    "__typename": "Issue",
                    "comments": {
                        "nodes": [
                            {
                                "id": "IC_decision",
                                "url": "decision",
                                "body": body(second),
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }
        },
    )

    records = GitHubTrackerAdapter(runner=runner).list_approval_events(
        _issue_resource()["url"]
    )

    assert [record.event for record in records] == [first, second]
    assert [record.tracker_record_id for record in records] == [
        "IC_request",
        "IC_decision",
    ]
    assert [value for value in runner.calls[1] if value.startswith("after=")] == [
        "after=cursor-a"
    ]
