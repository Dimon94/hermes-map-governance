from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def test_maps_dashboard_renders_application_empty_state():
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node.js is required to exercise the dashboard plugin bundle")
    result = subprocess.run(
        [
            node,
            str(PLUGIN_ROOT / "tests" / "dashboard_shell_probe.mjs"),
            str(PLUGIN_ROOT / "dashboard" / "dist" / "index.js"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "dashboard shell ready\n"


def test_maps_dashboard_groups_complete_cards_by_project():
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node.js is required to exercise the dashboard plugin bundle")
    result = subprocess.run(
        [
            node,
            str(PLUGIN_ROOT / "tests" / "dashboard_shell_probe.mjs"),
            str(PLUGIN_ROOT / "dashboard" / "dist" / "index.js"),
            "populated",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "dashboard board ready\n"
