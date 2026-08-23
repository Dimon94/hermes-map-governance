from __future__ import annotations

import json
import sqlite3
import sys
from argparse import ArgumentParser
from pathlib import Path
from types import SimpleNamespace

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

import map_governance.native as native
from dashboard.plugin_api import router
from hermes_cli.plugins import PluginState


def _write_skill(root: Path, name: str) -> Path:
    skill = root / name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        f"---\nname: {name}\ndescription: isolated {name}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return skill


def _write_profile(home: Path, name: str, toolset: str, skill_roots: list[Path]):
    profile = home / "profiles" / name
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {"enabled": ["map-governance"]},
                "toolsets": [toolset],
                "skills": {"external_dirs": [str(path) for path in skill_roots]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_registry(storage: Path):
    storage.mkdir(parents=True)
    with sqlite3.connect(storage / "registry.db") as connection:
        connection.executescript(
            """
            CREATE TABLE plugin_metadata(namespace TEXT, schema_version INTEGER);
            INSERT INTO plugin_metadata VALUES ('map-governance', 8);
            CREATE TABLE ceo_projects(project_id TEXT, project_url TEXT);
            INSERT INTO ceo_projects VALUES (
              'PVT_isolated_9', 'https://github.com/orgs/isolated/projects/9'
            );
            CREATE TABLE map_bindings(map_id TEXT, project_id TEXT, issue_url TEXT);
            CREATE TABLE map_projections(map_id TEXT, project_id TEXT, repository TEXT);
            INSERT INTO map_bindings VALUES (
              'I_isolated_3', 'PVT_isolated_9',
              'https://github.com/isolated/widget/issues/3'
            );
            INSERT INTO map_projections VALUES (
              'I_isolated_3', 'PVT_isolated_9', 'isolated/widget'
            );
            """
        )


def _fake_executable(path: Path, body: str):
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)


def test_setup_then_doctor_uses_only_isolated_config_and_read_only_fake_boundaries(
    tmp_path, monkeypatch, capsys
):
    isolated_home = tmp_path / "isolated-hermes"
    isolated_home.mkdir()
    decoy_home = tmp_path / "unrelated-user-home"
    decoy_home.mkdir()
    sentinel = decoy_home / "must-not-be-read-or-written"
    sentinel.write_bytes(b"unrelated-user-state")
    sentinel_stat = sentinel.stat()
    monkeypatch.setenv("HOME", str(decoy_home))
    monkeypatch.setenv("HERMES_HOME", str(isolated_home))

    agent_skills = tmp_path / "agent-skills"
    codex_skills = tmp_path / "codex-skills"
    delivery = _write_skill(agent_skills, "delivery-pipeline")
    implement = _write_skill(codex_skills, "implement")
    _write_profile(
        isolated_home,
        "ceo",
        "map-governance-ceo",
        [agent_skills, codex_skills],
    )
    _write_profile(
        isolated_home,
        "pm",
        "map-governance-pm",
        [agent_skills, codex_skills],
    )
    config = isolated_home / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "plugins": {"enabled": ["map-governance"]},
                "unrelated": {"preserved": True},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    original_hermes_config = config.read_bytes()
    repository = tmp_path / "widget"
    repository.mkdir()
    (repository / ".git").mkdir()
    storage = PluginState("map-governance").data_dir

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    call_log = tmp_path / "boundary-calls.jsonl"
    monkeypatch.setenv("MAP_GOVERNANCE_E2E_CALL_LOG", str(call_log))
    monkeypatch.setenv("PATH", str(fake_bin))
    prelude = (
        "import json, os, sys\n"
        "with open(os.environ['MAP_GOVERNANCE_E2E_CALL_LOG'], 'a', encoding='utf-8') as h:\n"
        "    h.write(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]) + '\\n')\n"
    )
    _fake_executable(
        fake_bin / "gh",
        prelude
        + "args = sys.argv[1:]\n"
        + "if args[:2] == ['auth', 'status']:\n"
        + "    payload = {'hosts': {'github.com': [{'state': 'success', 'active': True, 'login': 'governance', 'scopes': 'read:project, repo'}]}}\n"
        + "elif args[:2] == ['repo', 'view']:\n"
        + "    payload = {'nameWithOwner': 'isolated/widget', 'url': 'https://github.com/isolated/widget', 'viewerPermission': 'WRITE', 'isArchived': False}\n"
        + "else:\n"
        + "    payload = {'data': {'organization': {'projectV2': {'id': 'PVT_isolated_9', 'url': 'https://github.com/orgs/isolated/projects/9', 'viewerCanUpdate': True}}}}\n"
        + "print(json.dumps(payload))\n",
    )
    _fake_executable(
        fake_bin / "git",
        prelude
        + "args = sys.argv[1:]\n"
        + f"print({str(repository)!r} if args[-2:] == ['rev-parse', '--show-toplevel'] else 'git@github.com:isolated/widget.git')\n",
    )
    _fake_executable(
        fake_bin / "herdr",
        prelude
        + "args = sys.argv[1:]\n"
        + "if args == ['--version']:\n"
        + "    print('herdr 0.8.2')\n"
        + "else:\n"
        + "    print('hermes: current (v5) (/decoy/hermes)')\n"
        + "    print('codex: current (v3) (/decoy/codex)')\n"
        + "    print('claude: current (v8) (/decoy/claude)')\n",
    )

    desired = {
        "schema_version": 1,
        "profiles": {"ceo": "ceo", "pm": "pm"},
        "skills": {
            "plugin": ["map-governance:ceo", "map-governance:pm"],
            "external": [
                {"id": "delivery-pipeline", "path": str(delivery)},
                {"id": "implement", "path": str(implement)},
            ],
        },
        "routing": {"policy": "mixed"},
        "github": {
            "hostname": "github.com",
            "projects": [
                {
                    "id": "PVT_isolated_9",
                    "url": "https://github.com/orgs/isolated/projects/9",
                    "owner": "isolated",
                    "owner_type": "organization",
                    "number": 9,
                    "required_capability": "write",
                }
            ],
            "repositories": [
                {
                    "coordinate": "isolated/widget",
                    "path": str(repository),
                    "worker_write_required": True,
                    "publication_required": False,
                }
            ],
        },
        "authorities": {
            "worker": {"kind": "local_git", "credential_ref": "local-git"},
            "publisher": {
                "kind": "gh",
                "credential_ref": "gh:github.com:governance",
                "account": "governance",
                "required": False,
            },
        },
        "herdr": {"executable": str(fake_bin / "herdr")},
    }
    registrations = []
    _write_registry(storage)
    registry_before_registration = (storage / "registry.db").read_bytes()
    context = SimpleNamespace(
        state=SimpleNamespace(data_dir=storage),
        profile_name="custom",
        register_skill=lambda *args, **kwargs: None,
        register_tool=lambda **kwargs: None,
        register_hook=lambda *args, **kwargs: None,
        register_cli_command=lambda **command: registrations.append(command),
        get_config=lambda _key, default=None: default,
    )
    native.register(context)
    assert (storage / "registry.db").read_bytes() == registry_before_registration
    assert not list(storage.glob("registry.db-*"))
    assert not call_log.exists()
    parser = ArgumentParser()
    registrations[0]["setup_fn"](parser)
    desired_file = tmp_path / "desired.json"
    desired_file.write_text(json.dumps(desired), encoding="utf-8")

    assert (
        registrations[0]["handler_fn"](
            parser.parse_args(["setup", "plan", "--file", str(desired_file)])
        )
        == 0
    )
    plan = json.loads(capsys.readouterr().out)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    assert (
        registrations[0]["handler_fn"](
            parser.parse_args(
                [
                    "setup",
                    "apply",
                    "--file",
                    str(plan_file),
                    "--action",
                    "config.prerequisites",
                ]
            )
        )
        == 0
    )
    applied = json.loads(capsys.readouterr().out)
    setup_config = storage / "prerequisites.yaml"
    assert config.read_bytes() == original_hermes_config
    before_doctor_config = config.read_bytes()
    before_doctor_setup_config = setup_config.read_bytes()
    before_registry = (storage / "registry.db").read_bytes()
    assert registrations[0]["handler_fn"](parser.parse_args(["doctor"])) == 0
    report = json.loads(capsys.readouterr().out)

    dashboard = FastAPI()
    dashboard.include_router(router, prefix="/api/plugins/map-governance")
    dashboard_response = TestClient(dashboard).get(
        "/api/plugins/map-governance/doctor",
        params={"profile": "default"},
    )

    assert applied["readback"] == "confirmed"
    assert report["status"] == "pass"
    assert dashboard_response.status_code == 200
    assert dashboard_response.json()["status"] == "pass"
    assert config.read_bytes() == before_doctor_config
    assert setup_config.read_bytes() == before_doctor_setup_config
    assert (storage / "registry.db").read_bytes() == before_registry
    assert yaml.safe_load(config.read_text())["unrelated"] == {"preserved": True}
    assert sentinel.read_bytes() == b"unrelated-user-state"
    assert sentinel.stat().st_mtime_ns == sentinel_stat.st_mtime_ns
    assert not list(storage.glob("registry.db-*"))
    calls = [json.loads(line) for line in call_log.read_text().splitlines()]
    assert calls[0][:3] == ["gh", "auth", "status"]
    assert any(call[:3] == ["gh", "repo", "view"] for call in calls)
    assert any(call[0] == "git" and "rev-parse" in call for call in calls)
    assert [call[1:] for call in calls if call[0] == "herdr"] == [
        ["--version"],
        ["integration", "status"],
        ["--version"],
        ["integration", "status"],
    ]
    assert all("install" not in call and "login" not in call for call in calls)
    assert "/decoy/" not in json.dumps(report)
