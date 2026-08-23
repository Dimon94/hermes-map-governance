from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _run_git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def test_black_box_install_discover_open_and_remove(tmp_path, hermes_host_root):
    hermes_agent_root = hermes_host_root
    hermes_python = hermes_agent_root / "venv" / "bin" / "python"
    if not hermes_python.is_file():
        pytest.fail(f"Hermes Python runtime not found at {hermes_python}")

    source_repository = tmp_path / "source-plugin"
    shutil.copytree(
        PLUGIN_ROOT,
        source_repository,
        ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__"),
    )
    _run_git("init", "--quiet", cwd=source_repository)
    _run_git("config", "user.name", "Map Governance Tests", cwd=source_repository)
    _run_git(
        "config",
        "user.email",
        "map-governance-tests@example.invalid",
        cwd=source_repository,
    )
    _run_git("add", ".", cwd=source_repository)
    _run_git("commit", "--quiet", "-m", "test plugin snapshot", cwd=source_repository)

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled: []\n",
        encoding="utf-8",
    )
    bundled_plugins = tmp_path / "bundled-plugins"
    bundled_plugins.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(hermes_home),
            "HERMES_BUNDLED_PLUGINS": str(bundled_plugins),
            "HERMES_DASHBOARD_SESSION_TOKEN": "map-governance-lifecycle-token",
            "PYTHONPATH": str(hermes_agent_root),
        }
    )
    env.pop("HERMES_ENABLE_PROJECT_PLUGINS", None)
    result = subprocess.run(
        [
            str(hermes_python),
            str(PLUGIN_ROOT / "tests" / "lifecycle_probe.py"),
            source_repository.as_uri(),
            str(hermes_home),
            str(PLUGIN_ROOT / "tests" / "dashboard_shell_probe.mjs"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report == {
        "dashboard_discovered": True,
        "installed": True,
        "native_discovered": True,
        "opened": True,
        "removed": True,
    }
