from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from map_governance.prerequisites import (
    CommandResult,
    PrerequisiteApplication,
    SetupApplyError,
    YamlConfigRepository,
)
from map_governance.coordinator import CommissioningPrerequisiteError


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 24, 1, 30, tzinfo=timezone.utc)


def _skill(path: Path, name: str) -> Path:
    skill = path / name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        f"---\nname: {name}\ndescription: test {name}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return skill


def _profile(home: Path, name: str, toolset: str, external_dirs: list[Path]) -> Path:
    profile = home if name == "default" else home / "profiles" / name
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "toolsets": [toolset],
                "plugins": {"enabled": ["map-governance"]},
                "skills": {"external_dirs": [str(path) for path in external_dirs]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return profile


def _registry(storage_root: Path) -> None:
    storage_root.mkdir(parents=True)
    with sqlite3.connect(storage_root / "registry.db") as connection:
        connection.executescript(
            """
            CREATE TABLE plugin_metadata(namespace TEXT, schema_version INTEGER);
            INSERT INTO plugin_metadata VALUES ('map-governance', 8);
            CREATE TABLE ceo_projects(
                project_id TEXT, project_url TEXT, owner_login TEXT,
                owner_type TEXT, project_number INTEGER
            );
            INSERT INTO ceo_projects VALUES (
                'PVT_acme_7', 'https://github.com/orgs/acme/projects/7',
                'acme', 'organization', 7
            );
            CREATE TABLE map_bindings(
                map_id TEXT, project_id TEXT, issue_url TEXT
            );
            CREATE TABLE map_projections(
                map_id TEXT, project_id TEXT, repository TEXT
            );
            INSERT INTO map_bindings VALUES (
                'I_atlas_41', 'PVT_acme_7',
                'https://github.com/acme/atlas/issues/41'
            );
            INSERT INTO map_projections VALUES (
                'I_atlas_41', 'PVT_acme_7', 'acme/atlas'
            );
            """
        )


def _desired(
    *,
    delivery_skill: Path,
    implement_skill: Path,
    repository: Path,
    routing_policy: str = "mixed",
    publication_required: bool = False,
) -> dict:
    return {
        "schema_version": 1,
        "profiles": {"ceo": "ceo", "pm": "pm"},
        "skills": {
            "plugin": ["map-governance:ceo", "map-governance:pm"],
            "external": [
                {"id": "delivery-pipeline", "path": str(delivery_skill)},
                {"id": "implement", "path": str(implement_skill)},
            ],
        },
        "routing": {"policy": routing_policy},
        "github": {
            "hostname": "github.com",
            "projects": [
                {
                    "id": "PVT_acme_7",
                    "url": "https://github.com/orgs/acme/projects/7",
                    "owner": "acme",
                    "owner_type": "organization",
                    "number": 7,
                    "required_capability": "write",
                }
            ],
            "repositories": [
                {
                    "coordinate": "acme/atlas",
                    "path": str(repository),
                    "worker_write_required": True,
                    "publication_required": publication_required,
                }
            ],
        },
        "authorities": {
            "worker": {"kind": "local_git", "credential_ref": "local-git"},
            "publisher": {
                "kind": "gh",
                "credential_ref": "gh:github.com:release-bot",
                "account": "release-bot",
                "required": publication_required,
            },
        },
        "herdr": {"executable": "herdr"},
    }


class FakeRunner:
    def __init__(self, results: dict[tuple[str, ...], CommandResult]):
        self.results = results
        self.calls: list[tuple[str, ...]] = []

    def run(self, arguments, *, timeout):
        argv = tuple(arguments)
        self.calls.append(argv)
        return self.results.get(
            argv,
            CommandResult(
                arguments=argv,
                returncode=127,
                stdout="",
                stderr="unexpected command",
            ),
        )


class ReadConfig:
    def __init__(self, value):
        self.value = value

    def read(self):
        return copy.deepcopy(self.value)

    def revision(self):
        return "revision"

    def commit(self, **_arguments):
        raise AssertionError("commissioning context is read-only")


def test_commissioning_context_consumes_selected_doctor_confirmed_coordinates(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    skills = tmp_path / "skills"
    desired = _desired(
        delivery_skill=_skill(skills, "delivery-pipeline"),
        implement_skill=_skill(skills, "implement"),
        repository=repository,
        routing_policy="codex",
    )
    application = PrerequisiteApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data",
        config_repository=ReadConfig({"prerequisites": desired}),
        profile_resolver=lambda profile: tmp_path / "profiles" / profile,
    )
    monkeypatch.setattr(
        application,
        "doctor",
        lambda: {"status": "pass", "checks": []},
    )

    context = application.commissioning_context(
        project_id="PVT_acme_7",
        repository="acme/atlas",
    )

    assert context.project_url == "https://github.com/orgs/acme/projects/7"
    assert context.repository_path == str(repository)
    assert context.pm_profile == "pm"
    assert context.ceo_profile == "ceo"
    assert context.pm_storage_root == str(
        tmp_path / "profiles" / "pm" / "plugin-data" / "plugin-data"
    )
    assert context.routing_policy == "codex"
    assert context.skills == (
        "map-governance:pm",
        "delivery-pipeline",
        "herdr",
    )


def test_commissioning_context_rejects_failed_doctor_evidence(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    skills = tmp_path / "skills"
    desired = _desired(
        delivery_skill=_skill(skills, "delivery-pipeline"),
        implement_skill=_skill(skills, "implement"),
        repository=repository,
    )
    application = PrerequisiteApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data",
        config_repository=ReadConfig({"prerequisites": desired}),
        profile_resolver=lambda profile: tmp_path / "profiles" / profile,
    )
    monkeypatch.setattr(
        application,
        "doctor",
        lambda: {
            "status": "fail",
            "checks": [
                {"id": "herdr.integration.codex", "status": "fail"},
                {"id": "authority.publisher", "status": "warning"},
            ],
        },
    )

    with pytest.raises(CommissioningPrerequisiteError) as raised:
        application.commissioning_context(
            project_id="PVT_acme_7",
            repository="acme/atlas",
        )

    assert raised.value.failed_checks == ("herdr.integration.codex",)


def test_commissioning_context_ignores_unrelated_project_doctor_failure(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    skills = tmp_path / "skills"
    desired = _desired(
        delivery_skill=_skill(skills, "delivery-pipeline"),
        implement_skill=_skill(skills, "implement"),
        repository=repository,
        routing_policy="codex",
    )
    application = PrerequisiteApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=tmp_path / "plugin-data",
        config_repository=ReadConfig({"prerequisites": desired}),
        profile_resolver=lambda profile: tmp_path / "profiles" / profile,
    )
    monkeypatch.setattr(
        application,
        "doctor",
        lambda: {
            "status": "fail",
            "checks": [
                {"id": "github.project.PVT_unrelated_9.read", "status": "fail"},
                {"id": "repository.other/repository.worker", "status": "fail"},
            ],
        },
    )

    context = application.commissioning_context(
        project_id="PVT_acme_7",
        repository="acme/atlas",
    )

    assert context.project_id == "PVT_acme_7"
    assert context.repository == "acme/atlas"


def _command_results(repository: Path) -> dict[tuple[str, ...], CommandResult]:
    project_query_argv = PrerequisiteApplication.project_command(
        executable="gh",
        hostname="github.com",
        owner="acme",
        owner_type="organization",
        number=7,
    )
    return {
        (
            "gh",
            "auth",
            "status",
            "--active",
            "--hostname",
            "github.com",
            "--json",
            "hosts",
        ): CommandResult(
            arguments=(),
            returncode=0,
            stdout=json.dumps(
                {
                    "hosts": {
                        "github.com": [
                            {
                                "state": "success",
                                "active": True,
                                "login": "release-bot",
                                "scopes": "read:project, repo",
                            }
                        ]
                    }
                }
            ),
            stderr="",
        ),
        tuple(project_query_argv): CommandResult(
            arguments=(),
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "organization": {
                            "projectV2": {
                                "id": "PVT_acme_7",
                                "url": "https://github.com/orgs/acme/projects/7",
                                "viewerCanUpdate": True,
                            }
                        }
                    }
                }
            ),
            stderr="",
        ),
        (
            "gh",
            "repo",
            "view",
            "github.com/acme/atlas",
            "--json",
            "nameWithOwner,url,viewerPermission,isArchived",
        ): CommandResult(
            arguments=(),
            returncode=0,
            stdout=json.dumps(
                {
                    "nameWithOwner": "acme/atlas",
                    "url": "https://github.com/acme/atlas",
                    "viewerPermission": "WRITE",
                    "isArchived": False,
                }
            ),
            stderr="",
        ),
        ("git", "-C", str(repository), "rev-parse", "--show-toplevel"): CommandResult(
            arguments=(), returncode=0, stdout=f"{repository}\n", stderr=""
        ),
        ("git", "-C", str(repository), "remote", "get-url", "origin"): CommandResult(
            arguments=(),
            returncode=0,
            stdout="git@github.com:acme/atlas.git\n",
            stderr="",
        ),
        ("herdr", "--version"): CommandResult(
            arguments=(), returncode=0, stdout="herdr 0.8.2\n", stderr=""
        ),
        ("herdr", "integration", "status"): CommandResult(
            arguments=(),
            returncode=0,
            stdout=(
                "hermes: current (v5) (/Users/person/.hermes/plugin)\n"
                "codex: current (v3) (/Users/person/.codex/hook)\n"
                "claude: current (v8) (/Users/person/.claude/hook)\n"
            ),
            stderr="",
        ),
    }


@pytest.fixture
def isolated(tmp_path):
    home = tmp_path / "hermes-home"
    skills_a = tmp_path / "agent-skills"
    skills_b = tmp_path / "codex-skills"
    delivery = _skill(skills_a, "delivery-pipeline")
    implement = _skill(skills_b, "implement")
    _profile(home, "ceo", "map-governance-ceo", [skills_a, skills_b])
    _profile(home, "pm", "map-governance-pm", [skills_a, skills_b])
    control = _profile(home, "default", "hermes-cli", [skills_a, skills_b])
    repository = tmp_path / "atlas"
    repository.mkdir()
    (repository / ".git").mkdir()
    storage = control / "plugin-data" / "map-governance"
    _registry(storage)
    setup_config = storage / "prerequisites.yaml"
    setup_config.write_text("unrelated:\n  preserved: true\n", encoding="utf-8")
    desired = _desired(
        delivery_skill=delivery,
        implement_skill=implement,
        repository=repository,
    )
    return {
        "home": home,
        "control": control,
        "storage": storage,
        "setup_config": setup_config,
        "desired": desired,
        "repository": repository,
        "runner": FakeRunner(_command_results(repository)),
    }


def _application(isolated, *, runner=None, clock=lambda: NOW):
    home = isolated["home"]
    return PrerequisiteApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=isolated["storage"],
        config_repository=YamlConfigRepository(isolated["setup_config"]),
        profile_resolver=lambda profile: (
            home if profile == "default" else home / "profiles" / profile
        ),
        runner=runner or isolated["runner"],
        clock=clock,
    )


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        digest.update(str(relative).encode())
        stat = path.lstat()
        digest.update(str(stat.st_mode).encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_setup_plan_requires_explicit_stable_actions_and_secret_safe_preview(isolated):
    application = _application(isolated)

    desired = isolated["desired"]
    plan = application.setup_plan(desired=desired)

    assert plan["status"] == "planned"
    assert plan["plan_id"].startswith("setup-plan:")
    assert [action["action_id"] for action in plan["actions"]] == [
        "config.prerequisites",
    ]
    assert {action["authority"] for action in plan["actions"]} == {
        "operator:config.write"
    }
    assert plan["actions"][0]["after"] == desired
    assert "token" not in json.dumps(plan).lower()
    assert yaml.safe_load(isolated["setup_config"].read_text()) == {
        "unrelated": {"preserved": True}
    }


def test_setup_apply_only_mutates_explicit_selected_actions_and_reads_back(isolated):
    application = _application(isolated)
    plan = application.setup_plan(desired=isolated["desired"])

    report = application.setup_apply(
        plan=plan,
        selected_action_ids=["config.prerequisites"],
    )

    config = yaml.safe_load(isolated["setup_config"].read_text())
    assert report["status"] == "applied"
    assert report["applied_action_ids"] == ["config.prerequisites"]
    assert report["not_selected_action_ids"] == []
    assert report["readback"] == "confirmed"
    assert config["prerequisites"] == isolated["desired"]
    assert config["unrelated"] == {"preserved": True}


def test_setup_atomic_readback_failure_restores_original_config(isolated, monkeypatch):
    repository = YamlConfigRepository(isolated["setup_config"])
    application = PrerequisiteApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=isolated["storage"],
        config_repository=repository,
        profile_resolver=lambda profile: isolated["home"] / "profiles" / profile,
        runner=isolated["runner"],
        clock=lambda: NOW,
    )
    original = isolated["setup_config"].read_bytes()
    plan = application.setup_plan(desired=isolated["desired"])
    real_atomic_write = repository._atomic_write
    real_read = repository.read
    corrupt_readback = False

    def mark_first_readback(content, *, original, expected_revision=None):
        nonlocal corrupt_readback
        real_atomic_write(
            content,
            original=original,
            expected_revision=expected_revision,
        )
        corrupt_readback = True

    def read_once_as_corrupt():
        nonlocal corrupt_readback
        if corrupt_readback:
            corrupt_readback = False
            return {}
        return real_read()

    monkeypatch.setattr(repository, "_atomic_write", mark_first_readback)
    monkeypatch.setattr(repository, "read", read_once_as_corrupt)

    with pytest.raises(SetupApplyError) as raised:
        application.setup_apply(
            plan=plan,
            selected_action_ids=["config.prerequisites"],
        )

    assert raised.value.as_dict()["reason"] == "commit_failed"
    assert repository.path.read_bytes() == original


def test_setup_commit_refuses_to_overwrite_a_concurrent_config_change(
    isolated, monkeypatch
):
    repository = YamlConfigRepository(isolated["setup_config"])
    application = PrerequisiteApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=isolated["storage"],
        config_repository=repository,
        profile_resolver=lambda profile: isolated["home"] / "profiles" / profile,
        runner=isolated["runner"],
        clock=lambda: NOW,
    )
    plan = application.setup_plan(desired=isolated["desired"])
    real_revision = repository.revision
    revision_calls = 0

    def inject_concurrent_change():
        nonlocal revision_calls
        revision_calls += 1
        if revision_calls == 2:
            repository.path.write_text("concurrent: preserved\n", encoding="utf-8")
        return real_revision()

    monkeypatch.setattr(repository, "revision", inject_concurrent_change)

    with pytest.raises(SetupApplyError, match="concurrent config change") as raised:
        application.setup_apply(
            plan=plan,
            selected_action_ids=["config.prerequisites"],
        )

    assert raised.value.reason == "commit_failed"
    assert repository.path.read_text(encoding="utf-8") == "concurrent: preserved\n"


def test_plugin_owned_config_serializes_competing_setup_writers(isolated, monkeypatch):
    first_repository = YamlConfigRepository(isolated["setup_config"])
    second_repository = YamlConfigRepository(isolated["setup_config"])
    first = PrerequisiteApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=isolated["storage"],
        config_repository=first_repository,
        profile_resolver=lambda profile: isolated["home"] / "profiles" / profile,
        runner=isolated["runner"],
        clock=lambda: NOW,
    )
    second = PrerequisiteApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=isolated["storage"],
        config_repository=second_repository,
        profile_resolver=lambda profile: isolated["home"] / "profiles" / profile,
        runner=isolated["runner"],
        clock=lambda: NOW,
    )
    first_desired = copy.deepcopy(isolated["desired"])
    first_desired["routing"]["policy"] = "codex"
    second_desired = copy.deepcopy(isolated["desired"])
    second_desired["routing"]["policy"] = "claude"
    first_plan = first.setup_plan(desired=first_desired)
    second_plan = second.setup_plan(desired=second_desired)
    entered_commit = threading.Event()
    release_commit = threading.Event()
    second_finished = threading.Event()
    outcomes = []
    real_atomic_write = first_repository._atomic_write

    def hold_first_writer(content, *, original, expected_revision=None):
        entered_commit.set()
        assert release_commit.wait(timeout=2)
        real_atomic_write(
            content,
            original=original,
            expected_revision=expected_revision,
        )

    monkeypatch.setattr(first_repository, "_atomic_write", hold_first_writer)

    def apply(application, plan, finished=None):
        try:
            outcomes.append(
                application.setup_apply(
                    plan=plan,
                    selected_action_ids=["config.prerequisites"],
                )
            )
        except SetupApplyError as error:
            outcomes.append(error)
        finally:
            if finished is not None:
                finished.set()

    first_thread = threading.Thread(target=apply, args=(first, first_plan))
    second_thread = threading.Thread(
        target=apply,
        args=(second, second_plan, second_finished),
    )
    first_thread.start()
    assert entered_commit.wait(timeout=2)
    second_thread.start()
    assert not second_finished.wait(timeout=0.05)
    release_commit.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert sum(isinstance(outcome, SetupApplyError) for outcome in outcomes) == 1
    stored = yaml.safe_load(isolated["setup_config"].read_text())
    assert stored["prerequisites"]["routing"]["policy"] == "codex"


def test_setup_rejects_secret_material_instead_of_copying_it_to_plugin_config(
    isolated,
):
    desired = isolated["desired"]
    desired["authorities"]["publisher"]["accessToken"] = "github_pat_never-copy-me"

    with pytest.raises(ValueError, match="secret material is forbidden"):
        _application(isolated).setup_plan(desired=desired)

    assert "never-copy-me" not in isolated["setup_config"].read_text()


def test_setup_plan_redacts_secret_shaped_values_from_existing_config(isolated):
    existing = {
        "prerequisites": {
            "credential_ref": "github_pat_existing-secret",
            "note": "token ghp_existing-secret",
            "github_pat_existingkeysecret": "value",
        }
    }
    isolated["setup_config"].write_text(
        yaml.safe_dump(existing),
        encoding="utf-8",
    )

    plan = _application(isolated).setup_plan(desired=isolated["desired"])

    serialized = json.dumps(plan)
    assert "existing-secret" not in serialized
    assert plan["actions"][0]["before"] == {
        "credential_ref": "[REDACTED]",
        "note": "[REDACTED]",
        "[REDACTED]": "[REDACTED]",
    }


def test_setup_and_doctor_reject_plugin_config_symlink_without_reading_target(
    isolated,
):
    external = isolated["home"].parent / "unrelated-user-config.yaml"
    external.write_text(
        "prerequisites:\n  note: token ghp_external-secret\n",
        encoding="utf-8",
    )
    isolated["setup_config"].unlink()
    isolated["setup_config"].symlink_to(external)
    before = external.read_bytes()
    application = _application(isolated)

    with pytest.raises(ValueError, match="unavailable or invalid"):
        application.setup_plan(desired=isolated["desired"])
    report = application.doctor()

    assert external.read_bytes() == before
    assert "external-secret" not in json.dumps(report)
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["configuration"]["status"] == "fail"
    assert checks["configuration"]["evidence"] == {
        "readable": False,
        "schema_version": None,
    }
    assert (
        not isolated["setup_config"]
        .with_name(f".{isolated['setup_config'].name}.lock")
        .exists()
    )


def test_doctor_redacts_validation_errors_from_damaged_stored_config(isolated):
    damaged = copy.deepcopy(isolated["desired"])
    damaged["skills"]["external"][0]["id"] = "github_pat_damaged-secret"
    damaged["skills"]["external"][1]["id"] = "github_pat_damaged-secret"
    isolated["setup_config"].write_text(
        yaml.safe_dump({"prerequisites": damaged}),
        encoding="utf-8",
    )

    report = _application(isolated).doctor()

    serialized = json.dumps(report)
    assert "damaged-secret" not in serialized
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["configuration"]["status"] == "fail"
    assert checks["configuration"]["remediation"]["description"] == (
        "Generate a new secret-free setup plan and explicitly apply its config action."
    )


def test_setup_rejects_a_shared_ceo_and_pm_profile(isolated):
    desired = copy.deepcopy(isolated["desired"])
    desired["profiles"]["pm"] = desired["profiles"]["ceo"]

    with pytest.raises(ValueError, match="must be distinct"):
        _application(isolated).setup_plan(desired=desired)


def test_setup_rejects_token_shaped_credential_refs_and_recomputed_forged_plans(
    isolated,
):
    application = _application(isolated)
    desired = copy.deepcopy(isolated["desired"])
    desired["authorities"]["publisher"]["credential_ref"] = "github_pat_never"
    with pytest.raises(ValueError, match="gh:HOST:ACCOUNT"):
        application.setup_plan(desired=desired)

    plan = application.setup_plan(desired=isolated["desired"])
    plan["actions"][0]["after"]["authorities"]["publisher"]["token"] = (
        "github_pat_never"
    )
    unsigned = dict(plan)
    unsigned.pop("plan_id")
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    plan["plan_id"] = (
        "setup-plan:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    )

    with pytest.raises(SetupApplyError) as raised:
        application.setup_apply(
            plan=plan,
            selected_action_ids=["config.prerequisites"],
        )

    assert raised.value.reason == "payload_drift"
    assert "github_pat_never" not in isolated["setup_config"].read_text()


@pytest.mark.parametrize("mode", ["unknown", "stale", "expired", "drift"])
def test_setup_apply_fails_closed_for_invalid_plan(isolated, mode):
    application = _application(isolated)
    plan = application.setup_plan(desired=isolated["desired"])
    before = isolated["setup_config"].read_bytes()
    selected = ["config.prerequisites"]
    if mode == "unknown":
        selected = ["config.unknown"]
    elif mode == "stale":
        config = yaml.safe_load(before)
        config["unrelated"] = True
        isolated["setup_config"].write_text(yaml.safe_dump(config), encoding="utf-8")
        before = isolated["setup_config"].read_bytes()
    elif mode == "expired":
        application = _application(
            isolated,
            clock=lambda: datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc),
        )
    else:
        plan["actions"][0]["after"]["routing"]["policy"] = "codex"

    with pytest.raises(SetupApplyError) as raised:
        application.setup_apply(plan=plan, selected_action_ids=selected)

    assert raised.value.as_dict()["reason"] in {
        "unknown_action",
        "stale_plan",
        "expired_plan",
        "payload_drift",
    }
    assert isolated["setup_config"].read_bytes() == before


def test_doctor_is_repeatable_and_does_not_write_any_external_state(isolated):
    application = _application(isolated)
    plan = application.setup_plan(desired=isolated["desired"])
    application.setup_apply(
        plan=plan,
        selected_action_ids=["config.prerequisites"],
    )
    before = _tree_digest(isolated["home"].parent)

    first = application.doctor()
    second = application.doctor()

    assert first == second
    assert first["status"] == "pass"
    assert first["checked_at"] == "2026-08-24T01:30:00Z"
    assert _tree_digest(isolated["home"].parent) == before
    assert not list(isolated["storage"].glob("*.db-wal"))
    assert not list(isolated["storage"].glob("*.db-shm"))
    checks = {check["id"]: check for check in first["checks"]}
    assert checks["profiles.separation"]["status"] == "pass"
    assert checks["skills.external.delivery-pipeline"]["status"] == "pass"
    assert checks["github.project.PVT_acme_7.write"]["status"] == "pass"
    assert checks["repository.acme/atlas.governance"]["status"] == "pass"
    assert checks["repository.acme/atlas.worker"]["status"] == "pass"
    assert checks["repository.acme/atlas.publisher"]["status"] == "warning"
    assert checks["herdr.integration.codex"]["status"] == "pass"
    assert all(isinstance(check["evidence"], dict) for check in first["checks"])
    assert all(
        "command" not in remediation or isinstance(remediation["command"], list)
        for check in first["checks"]
        if (remediation := check.get("remediation"))
    )


def test_doctor_reports_profile_skill_storage_binding_and_authority_failures(isolated):
    desired = isolated["desired"]
    desired["skills"]["external"][0]["path"] = str(
        isolated["home"].parent / "missing" / "SKILL.md"
    )
    desired["github"]["repositories"][0]["publication_required"] = True
    desired["authorities"]["publisher"]["required"] = True
    plan = _application(isolated).setup_plan(desired=desired)
    _application(isolated).setup_apply(
        plan=plan,
        selected_action_ids=["config.prerequisites"],
    )
    os.chmod(isolated["storage"], 0o500)

    report = _application(isolated).doctor()

    checks = {check["id"]: check for check in report["checks"]}
    assert report["status"] == "fail"
    assert checks["profiles.separation"]["status"] == "pass"
    assert checks["skills.external.delivery-pipeline"]["status"] == "fail"
    assert checks["storage.permissions"]["status"] == "fail"
    assert checks["repository.acme/atlas.publisher"]["status"] == "pass"
    assert "token" not in json.dumps(report).lower()


def test_doctor_opens_a_read_only_registry_without_requiring_database_write_mode(
    isolated,
):
    application = _application(isolated)
    plan = application.setup_plan(desired=isolated["desired"])
    application.setup_apply(plan=plan, selected_action_ids=["config.prerequisites"])
    database = isolated["storage"] / "registry.db"
    os.chmod(database, 0o400)
    before = database.read_bytes()

    report = application.doctor()

    checks = {check["id"]: check for check in report["checks"]}
    assert checks["storage.database"]["status"] == "pass"
    assert checks["storage.database"]["evidence"]["read_only_open"] is True
    assert database.read_bytes() == before
    assert not list(isolated["storage"].glob("registry.db-*"))


def test_doctor_distinguishes_project_read_from_missing_write_capability(isolated):
    application = _application(isolated)
    plan = application.setup_plan(desired=isolated["desired"])
    application.setup_apply(plan=plan, selected_action_ids=["config.prerequisites"])
    results = _command_results(isolated["repository"])
    project_argv = tuple(
        PrerequisiteApplication.project_command(
            executable="gh",
            hostname="github.com",
            owner="acme",
            owner_type="organization",
            number=7,
        )
    )
    results[project_argv] = CommandResult(
        arguments=project_argv,
        returncode=0,
        stdout=json.dumps(
            {
                "data": {
                    "organization": {
                        "projectV2": {
                            "id": "PVT_acme_7",
                            "url": "https://github.com/orgs/acme/projects/7",
                            "viewerCanUpdate": False,
                        }
                    }
                }
            }
        ),
        stderr="",
    )

    report = _application(isolated, runner=FakeRunner(results)).doctor()

    checks = {check["id"]: check for check in report["checks"]}
    assert checks["github.project.PVT_acme_7.read"]["status"] == "pass"
    assert checks["github.project.PVT_acme_7.write"]["status"] == "fail"
    assert checks["github.project.PVT_acme_7.write"]["evidence"] == {
        "project_id": "PVT_acme_7",
        "required": True,
        "viewer_can_update": False,
    }


def test_doctor_accepts_triage_for_issue_governance_but_not_publication(isolated):
    application = _application(isolated)
    desired = copy.deepcopy(isolated["desired"])
    desired["github"]["repositories"][0]["publication_required"] = True
    desired["authorities"]["publisher"]["required"] = True
    plan = application.setup_plan(desired=desired)
    application.setup_apply(plan=plan, selected_action_ids=["config.prerequisites"])
    results = _command_results(isolated["repository"])
    repository_argv = (
        "gh",
        "repo",
        "view",
        "github.com/acme/atlas",
        "--json",
        "nameWithOwner,url,viewerPermission,isArchived",
    )
    payload = json.loads(results[repository_argv].stdout)
    payload["viewerPermission"] = "TRIAGE"
    results[repository_argv] = CommandResult(
        arguments=repository_argv,
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
    )

    report = _application(isolated, runner=FakeRunner(results)).doctor()

    checks = {check["id"]: check for check in report["checks"]}
    assert checks["repository.acme/atlas.read"]["status"] == "pass"
    assert checks["repository.acme/atlas.governance"]["status"] == "pass"
    assert checks["repository.acme/atlas.publisher"]["status"] == "fail"


@pytest.mark.parametrize(
    ("routing", "missing", "expected"),
    [
        ("codex", "codex", "fail"),
        ("claude", "codex", "warning"),
        ("mixed", "codex", "fail"),
        ("codex", "claude", "warning"),
    ],
)
def test_doctor_classifies_required_herdr_integrations_by_routing_policy(
    isolated, routing, missing, expected
):
    desired = isolated["desired"]
    desired["routing"]["policy"] = routing
    application = _application(isolated)
    plan = application.setup_plan(desired=desired)
    application.setup_apply(plan=plan, selected_action_ids=["config.prerequisites"])
    results = _command_results(isolated["repository"])
    results[("herdr", "integration", "status")] = CommandResult(
        arguments=(),
        returncode=0,
        stdout="\n".join(
            f"{name}: {'not installed' if name == missing else 'current (v1)'}"
            for name in ("hermes", "codex", "claude")
        ),
        stderr="",
    )

    report = _application(isolated, runner=FakeRunner(results)).doctor()

    check = next(
        item
        for item in report["checks"]
        if item["id"] == f"herdr.integration.{missing}"
    )
    assert check["status"] == expected
    assert check["remediation"]["command"] == [
        "herdr",
        "integration",
        "install",
        missing,
    ]


@pytest.mark.parametrize(
    ("mode", "outcome"), [("missing", "unavailable"), ("malformed", "success")]
)
def test_doctor_reports_missing_or_malformed_herdr_without_leaking_output(
    isolated, mode, outcome
):
    plan = _application(isolated).setup_plan(desired=isolated["desired"])
    _application(isolated).setup_apply(
        plan=plan, selected_action_ids=["config.prerequisites"]
    )
    results = _command_results(isolated["repository"])
    if mode == "missing":
        results[("herdr", "--version")] = CommandResult(
            arguments=(), returncode=127, stdout="", stderr="secret path"
        )
        results[("herdr", "integration", "status")] = CommandResult(
            arguments=(), returncode=127, stdout="", stderr="secret path"
        )
    else:
        results[("herdr", "integration", "status")] = CommandResult(
            arguments=(),
            returncode=0,
            stdout="credential=github_pat_hidden /Users/person/.config\n",
            stderr="",
        )

    report = _application(isolated, runner=FakeRunner(results)).doctor()

    checks = {check["id"]: check for check in report["checks"]}
    if mode == "missing":
        assert checks["herdr.binary"]["status"] == "fail"
        assert checks["herdr.binary"]["evidence"]["outcome"] == outcome
    else:
        assert checks["herdr.binary"]["status"] == "pass"
        assert (
            checks["herdr.integration.hermes"]["evidence"]["probe_outcome"]
            == "malformed"
        )
    assert checks["herdr.integration.hermes"]["status"] == "fail"
    assert "github_pat_hidden" not in json.dumps(report)
    assert "/Users/person" not in json.dumps(report)


def test_doctor_rejects_secret_bearing_herdr_version_output(isolated):
    plan = _application(isolated).setup_plan(desired=isolated["desired"])
    _application(isolated).setup_apply(
        plan=plan, selected_action_ids=["config.prerequisites"]
    )
    results = _command_results(isolated["repository"])
    results[("herdr", "--version")] = CommandResult(
        arguments=(),
        returncode=0,
        stdout="herdr 0.8.2 token=github_pat_hidden /Users/person\n",
        stderr="",
    )

    report = _application(isolated, runner=FakeRunner(results)).doctor()

    check = next(item for item in report["checks"] if item["id"] == "herdr.binary")
    assert check["status"] == "fail"
    assert check["evidence"]["version"] is None
    assert "github_pat_hidden" not in json.dumps(report)
    assert "/Users/person" not in json.dumps(report)


def test_doctor_requires_the_configured_github_host_for_local_origin(isolated):
    plan = _application(isolated).setup_plan(desired=isolated["desired"])
    _application(isolated).setup_apply(
        plan=plan, selected_action_ids=["config.prerequisites"]
    )
    results = _command_results(isolated["repository"])
    remote_argv = (
        "git",
        "-C",
        str(isolated["repository"]),
        "remote",
        "get-url",
        "origin",
    )
    results[remote_argv] = CommandResult(
        arguments=remote_argv,
        returncode=0,
        stdout="git@evil.example:acme/atlas.git\n",
        stderr="",
    )

    report = _application(isolated, runner=FakeRunner(results)).doctor()

    worker = next(
        item
        for item in report["checks"]
        if item["id"] == "repository.acme/atlas.worker"
    )
    assert worker["status"] == "fail"
    assert worker["evidence"]["origin_matches"] is False


@pytest.mark.parametrize("failure", ["timeout", "denied", "invalid-json"])
def test_doctor_turns_github_command_failures_into_safe_evidence(isolated, failure):
    plan = _application(isolated).setup_plan(desired=isolated["desired"])
    _application(isolated).setup_apply(
        plan=plan, selected_action_ids=["config.prerequisites"]
    )
    results = _command_results(isolated["repository"])
    auth_argv = (
        "gh",
        "auth",
        "status",
        "--active",
        "--hostname",
        "github.com",
        "--json",
        "hosts",
    )
    if failure == "timeout":
        results[auth_argv] = CommandResult(
            arguments=auth_argv,
            returncode=None,
            stdout="",
            stderr="ghp_super-secret-token",
            timed_out=True,
        )
    elif failure == "denied":
        results[auth_argv] = CommandResult(
            arguments=auth_argv,
            returncode=1,
            stdout="",
            stderr="token ghp_super-secret-token denied",
        )
    else:
        results[auth_argv] = CommandResult(
            arguments=auth_argv,
            returncode=0,
            stdout="{not json",
            stderr="github_pat_super-secret",
        )

    report = _application(isolated, runner=FakeRunner(results)).doctor()

    auth = next(item for item in report["checks"] if item["id"] == "github.auth")
    serialized = json.dumps(report)
    assert auth["status"] == "fail"
    assert auth["evidence"]["outcome"] in {"timeout", "denied", "invalid_json"}
    assert "super-secret" not in serialized
    assert "/Users/person" not in serialized


def test_doctor_storage_absence_does_not_create_directory(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("{}\n", encoding="utf-8")
    storage = home / "plugin-data" / "map-governance"
    application = PrerequisiteApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage,
        config_repository=YamlConfigRepository(config),
        profile_resolver=lambda _profile: home,
        runner=FakeRunner({}),
        clock=lambda: NOW,
    )

    report = application.doctor()

    assert not storage.exists()
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["storage.path"]["status"] == "fail"
    assert checks["profiles.ceo"]["status"] == "fail"
    assert checks["profiles.pm"]["status"] == "fail"
    assert checks["github.projects"]["status"] == "fail"
    assert checks["github.repositories"]["status"] == "fail"
    assert checks["authority.worker"]["status"] == "fail"
    assert checks["authority.publisher"]["status"] == "warning"


def test_doctor_turns_unreadable_config_into_safe_independent_evidence(tmp_path):
    class UnreadableConfig:
        def read(self):
            raise PermissionError("secret home path")

        def revision(self):  # pragma: no cover - doctor never asks
            raise AssertionError

        def commit(self, **_arguments):  # pragma: no cover - doctor never mutates
            raise AssertionError

    home = tmp_path / "home"
    home.mkdir()
    application = PrerequisiteApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=home / "plugin-data" / "map-governance",
        config_repository=UnreadableConfig(),
        profile_resolver=lambda _profile: home,
        runner=FakeRunner({}),
        clock=lambda: NOW,
    )

    report = application.doctor()

    checks = {check["id"]: check for check in report["checks"]}
    assert report["status"] == "fail"
    assert checks["configuration"]["evidence"]["readable"] is False
    assert checks["profiles.ceo"]["status"] == "fail"
    assert checks["github.auth"]["status"] == "fail"
    assert "secret home path" not in json.dumps(report)
