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
