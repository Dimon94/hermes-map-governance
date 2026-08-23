from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


hermes_agent_root = os.environ.get("HERMES_AGENT_ROOT")
if hermes_agent_root:
    resolved_root = Path(hermes_agent_root).expanduser().resolve()
    if str(resolved_root) not in sys.path:
        sys.path.insert(0, str(resolved_root))


@pytest.fixture(scope="session")
def hermes_host_root() -> Path:
    """Return the explicitly configured Hermes checkout for integration tests."""
    if not hermes_agent_root:
        pytest.fail(
            "Set HERMES_AGENT_ROOT to a Hermes checkout for plugin integration tests"
        )
    return Path(hermes_agent_root).expanduser().resolve()


@pytest.fixture
def fake_github_boundary(tmp_path) -> tuple[Path, Path]:
    """Provide a controllable executable boundary for the real GitHub adapter."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    executable = bin_dir / "gh"
    log = tmp_path / "gh-calls.jsonl"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

arguments = sys.argv[1:]
with open(os.environ["MAP_GOVERNANCE_FAKE_GH_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(arguments) + "\\n")
query = next(value.removeprefix("query=") for value in arguments if value.startswith("query="))
if "projectV2" in query:
    owner = next(value.removeprefix("owner=") for value in arguments if value.startswith("owner="))
    number = int(next(value.removeprefix("number=") for value in arguments if value.startswith("number=")))
    owner_type = "Organization" if "organization(" in query else "User"
    resource = {
        "id": "PVT_acme_7",
        "number": number,
        "title": "Acme CEO portfolio",
        "url": f"https://github.com/{'orgs' if owner_type == 'Organization' else 'users'}/{owner}/projects/{number}",
        "owner": {"__typename": owner_type, "login": owner},
    }
    owner_field = "organization" if owner_type == "Organization" else "user"
    payload = {"data": {owner_field: {"projectV2": resource}}}
else:
    url = next(value.removeprefix("url=") for value in arguments if value.startswith("url="))
    resource = {
        "__typename": "Issue",
        "id": "I_atlas_41",
        "number": 41,
        "title": "Map the Atlas launch",
        "url": url,
        "state": "OPEN",
        "stateReason": None,
        "repository": {"nameWithOwner": "acme/atlas"},
        "labels": {"nodes": [{"name": "map"}, {"name": "map-stage/authorized"}]},
    }
    payload = {"data": {"resource": resource}}
print(json.dumps(payload))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, log
