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


def test_ready_map_opens_the_canonical_session_through_the_desktop_sdk():
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node.js is required to exercise the dashboard plugin bundle")
    result = subprocess.run(
        [
            node,
            str(PLUGIN_ROOT / "tests" / "dashboard_shell_probe.mjs"),
            str(PLUGIN_ROOT / "dashboard" / "dist" / "index.js"),
            "ready",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "dashboard session open ready\n"


def test_transient_session_hydration_uses_the_desktop_retry_contract():
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node.js is required to exercise the dashboard plugin bundle")
    result = subprocess.run(
        [
            node,
            str(PLUGIN_ROOT / "tests" / "dashboard_shell_probe.mjs"),
            str(PLUGIN_ROOT / "dashboard" / "dist" / "index.js"),
            "hydration-retry",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "dashboard hydration retry ready\n"


def test_failed_dashboard_transition_keeps_committed_stage_and_shows_error():
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node.js is required to exercise the dashboard plugin bundle")
    result = subprocess.run(
        [
            node,
            str(PLUGIN_ROOT / "tests" / "dashboard_shell_probe.mjs"),
            str(PLUGIN_ROOT / "dashboard" / "dist" / "index.js"),
            "transition-failure",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "dashboard transition failure ready\n"


def test_map_detail_exposes_explicit_accessible_chairman_approval_actions():
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node.js is required to exercise the dashboard plugin bundle")
    result = subprocess.run(
        [
            node,
            str(PLUGIN_ROOT / "tests" / "dashboard_shell_probe.mjs"),
            str(PLUGIN_ROOT / "dashboard" / "dist" / "index.js"),
            "approval",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "dashboard chairman approval ready\n"


def test_map_card_and_detail_render_pm_executive_summary_without_lane_cards():
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node.js is required to exercise the dashboard plugin bundle")
    result = subprocess.run(
        [
            node,
            str(PLUGIN_ROOT / "tests" / "dashboard_shell_probe.mjs"),
            str(PLUGIN_ROOT / "dashboard" / "dist" / "index.js"),
            "pm-report",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "dashboard PM reporting ready\n"


def test_map_card_renders_terminal_outbox_reason_and_repair_command():
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node.js is required to exercise the dashboard plugin bundle")
    result = subprocess.run(
        [
            node,
            str(PLUGIN_ROOT / "tests" / "dashboard_shell_probe.mjs"),
            str(PLUGIN_ROOT / "dashboard" / "dist" / "index.js"),
            "outbox-terminal",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "dashboard Outbox repair ready\n"
