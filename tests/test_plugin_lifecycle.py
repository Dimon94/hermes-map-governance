from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPOSITORY_ROOT / "plugin"
PLUGIN_SUBDIRECTORY = "plugin"
PUBLIC_INSTALL_IDENTIFIER = (
    "https://github.com/Dimon94/hermes-map-governance.git#plugin"
)


def _run_git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def test_black_box_install_discover_open_and_remove(
    tmp_path,
    hermes_host_root,
    fake_github_boundary,
):
    hermes_agent_root = hermes_host_root
    hermes_python = hermes_agent_root / "venv" / "bin" / "python"
    if not hermes_python.is_file():
        pytest.fail(f"Hermes Python runtime not found at {hermes_python}")

    source_repository = tmp_path / "source-plugin"
    shutil.copytree(
        REPOSITORY_ROOT,
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
        "plugins:\n  enabled: []\n  scan_on_install: true\n",
        encoding="utf-8",
    )
    bundled_plugins = tmp_path / "bundled-plugins"
    bundled_plugins.mkdir()
    env = os.environ.copy()
    fake_gh, fake_gh_log = fake_github_boundary
    env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": (f"url.{source_repository.as_uri()}.insteadOf"),
            "GIT_CONFIG_VALUE_0": PUBLIC_INSTALL_IDENTIFIER.removesuffix(
                f"#{PLUGIN_SUBDIRECTORY}"
            ),
            "GIT_TERMINAL_PROMPT": "0",
            "HERMES_HOME": str(hermes_home),
            "HERMES_BUNDLED_PLUGINS": str(bundled_plugins),
            "HERMES_DASHBOARD_SESSION_TOKEN": "map-governance-lifecycle-token",
            "PYTHONPATH": str(hermes_agent_root),
            "PATH": f"{fake_gh.parent}{os.pathsep}{env['PATH']}",
            "MAP_GOVERNANCE_FAKE_GH_LOG": str(fake_gh_log),
        }
    )
    env.pop("HERMES_ENABLE_PROJECT_PLUGINS", None)
    result = subprocess.run(
        [
            str(hermes_python),
            str(REPOSITORY_ROOT / "tests" / "lifecycle_probe.py"),
            PUBLIC_INSTALL_IDENTIFIER,
            str(hermes_home),
            str(REPOSITORY_ROOT / "tests" / "dashboard_shell_probe.mjs"),
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
        "disabled": True,
        "doctor_available": True,
        "installed": True,
        "map_bound": True,
        "native_discovered": True,
        "canonical_session_opened": True,
        "payload_isolated": True,
        "removed": True,
        "setup_verified": True,
        "source_recorded": True,
    }


def test_public_install_identifier_and_package_metadata_are_consistent():
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    after_install = (PLUGIN_ROOT / "after-install.md").read_text(encoding="utf-8")
    manifest = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")

    command = f"hermes plugins install {PUBLIC_INSTALL_IDENTIFIER} --enable"
    assert command in readme
    assert PUBLIC_INSTALL_IDENTIFIER in after_install
    assert f"homepage: {PUBLIC_INSTALL_IDENTIFIER}" in manifest

    assert {
        path.name for path in PLUGIN_ROOT.iterdir() if path.name != "__pycache__"
    } == {
        "__init__.py",
        "after-install.md",
        "dashboard",
        "map_governance",
        "plugin.yaml",
        "skills",
    }
    for former_root_runtime in (
        "__init__.py",
        "after-install.md",
        "dashboard",
        "map_governance",
        "plugin.yaml",
        "skills",
    ):
        assert not (REPOSITORY_ROOT / former_root_runtime).exists()
    assert not any(path.is_symlink() for path in PLUGIN_ROOT.rglob("*"))
