from __future__ import annotations

import json

import pytest

from map_governance.tracker import (
    GitHubTrackerAdapter,
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
        value.removeprefix("query=")
        for value in mutation
        if value.startswith("query=")
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
