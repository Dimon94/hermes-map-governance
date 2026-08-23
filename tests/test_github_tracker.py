from __future__ import annotations

import json

from map_governance.tracker import GitHubTrackerAdapter, TrackerIssue, TrackerProject


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
