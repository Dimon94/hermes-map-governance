from __future__ import annotations

import hashlib
import json
import grp
import os
import pwd
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from map_governance.publication import (
    AcceptanceEvidence,
    ApprovedPublicationAction,
    GitBundlePublicationHandoff,
    GitHubPublisherBoundary,
    PublicationAction,
    PublicationPartialFailure,
    SubprocessPublisherCommandRunner,
)
from map_governance.runtime import (
    ProfileResolutionError,
    application_for_profile,
    application_for_storage,
    publisher_application_for_profile,
    publisher_application_for_storage,
)
from map_governance.storage import PluginStorage


REVISION = "a" * 40
FAKE_GIT = Path("/trusted/git")
FAKE_GH = Path("/trusted/gh")


def _other_os_user() -> str:
    current_uid = os.geteuid()
    return next(user.pw_name for user in pwd.getpwall() if user.pw_uid != current_uid)


class IsolatedRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Path | None]] = []
        self.environments: list[dict[str, str]] = []
        self.responses: list[str | Exception] = []
        self.repository_capability = {
            "nameWithOwner": "acme/atlas",
            "viewerPermission": "WRITE",
            "isArchived": False,
        }

    def run(self, command, *, cwd=None, environment=None):
        self.calls.append((tuple(command), cwd))
        self.environments.append(dict(environment or {}))
        if tuple(command[1:3]) == ("repo", "view"):
            return json.dumps(self.repository_capability)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _boundary(runner: IsolatedRunner, repository: Path) -> GitHubPublisherBoundary:
    config_dir = repository / "publisher-gh-config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.chmod(0o700)
    return GitHubPublisherBoundary(
        hostname="github.com",
        account="governance-bot",
        authority_ref="gh:github.com:governance-bot",
        repositories={"acme/atlas": repository},
        handoff_root=repository / "publication-handoffs",
        worker_os_user=_other_os_user(),
        control_group=grp.getgrgid(os.getgid()).gr_name,
        gh_config_dir=config_dir,
        git_executable=FAKE_GIT,
        gh_executable=FAKE_GH,
        profile_name="publisher-cli",
        runner=runner,
        clock=lambda: datetime(2026, 8, 24, 8, 5, tzinfo=timezone.utc),
    )


def _push_action() -> ApprovedPublicationAction:
    return ApprovedPublicationAction(
        action_id="push-main-a",
        map_id="I_atlas_41",
        revision=REVISION,
        action="push",
        target={"repository": "acme/atlas", "ref": "refs/heads/main"},
    )


def test_publication_target_rejects_unapproved_extra_fields():
    with pytest.raises(ValueError, match="must contain exactly"):
        ApprovedPublicationAction(
            action_id="push-main-extra",
            map_id="I_atlas_41",
            revision=REVISION,
            action="push",
            target={
                "repository": "acme/atlas",
                "ref": "refs/heads/main",
                "force": True,
            },
        )


def test_github_publisher_executes_only_exact_revision_after_account_preflight(
    tmp_path,
):
    runner = IsolatedRunner()
    runner.responses = [
        "governance-bot\n",
        "",
        "",
        "",
        "",
    ]
    boundary = _boundary(runner, tmp_path)

    boundary.validate_authority()
    boundary.validate_action(_push_action())
    assert not any((tmp_path / "publisher-gh-config" / "publication-staging").iterdir())
    boundary.prepare_action(_push_action())
    boundary.execute(_push_action())

    assert len(runner.calls) == 6
    assert runner.calls[0][0][0] == str(FAKE_GH)
    assert runner.calls[2][0][:3] == (str(FAKE_GIT), "init", "--bare")
    assert "fetch" in runner.calls[3][0]
    assert str(tmp_path) not in runner.calls[3][0]
    assert runner.calls[3][0][-2].endswith(f"{REVISION}.bundle")
    assert "cat-file" in runner.calls[4][0]
    push = runner.calls[5][0]
    assert push[0] == str(FAKE_GIT)
    assert "--no-verify" in push
    assert "core.hooksPath=/dev/null" in push
    assert "http.sslVerify=true" in push
    assert f"credential.helper=!{FAKE_GH} auth git-credential" in push
    publisher_environment = {
        "GH_CONFIG_DIR": str(tmp_path / "publisher-gh-config"),
        "GH_HOST": "github.com",
        "HOME": str(tmp_path / "publisher-gh-config"),
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    assert runner.environments == [publisher_environment] * 6
    assert not list(
        (tmp_path / "publisher-gh-config" / "publication-staging").iterdir()
    )


def test_publisher_discards_prepared_staging_when_marker_fence_rejects(tmp_path):
    runner = IsolatedRunner()
    runner.responses = ["", "", ""]
    boundary = _boundary(runner, tmp_path)

    boundary.prepare_action(_push_action())
    staging_root = tmp_path / "publisher-gh-config" / "publication-staging"
    assert any(staging_root.iterdir())

    boundary.abort_action(_push_action())

    assert not any(staging_root.iterdir())


def test_subprocess_publisher_drops_ambient_worker_credentials(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **arguments):
        captured.update(command=command, **arguments)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("GH_TOKEN", "worker-token-must-not-cross")
    monkeypatch.setenv("GITHUB_TOKEN", "worker-token-must-not-cross")
    monkeypatch.setenv("HOME", str(tmp_path / "worker-home"))
    monkeypatch.setattr("map_governance.publication.subprocess.run", fake_run)

    SubprocessPublisherCommandRunner().run(
        ("gh", "auth", "status"),
        environment={"GH_CONFIG_DIR": str(tmp_path), "HOME": str(tmp_path)},
    )

    assert captured["env"]["GH_CONFIG_DIR"] == str(tmp_path)
    assert captured["env"]["HOME"] == str(tmp_path)
    assert captured["env"]["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
    assert "GH_TOKEN" not in captured["env"]
    assert "GITHUB_TOKEN" not in captured["env"]


def test_github_publisher_readback_returns_exact_immutable_push_evidence(tmp_path):
    runner = IsolatedRunner()
    runner.responses = [
        "governance-bot\n",
        json.dumps(
            {
                "data": {
                    "repository": {
                        "ref": {
                            "target": {
                                "__typename": "Commit",
                                "oid": REVISION,
                                "committedDate": "2020-01-01T00:00:00Z",
                            }
                        }
                    }
                }
            }
        ),
    ]

    evidence = _boundary(runner, tmp_path).readback(_push_action())

    assert evidence is not None
    assert evidence.action_id == "push-main-a"
    assert evidence.revision == REVISION
    assert evidence.remote_url == f"https://github.com/acme/atlas/commit/{REVISION}"
    assert evidence.published_at == "2026-08-24T08:05:00Z"


def test_github_publisher_rejects_account_mismatch_without_remote_mutation(tmp_path):
    runner = IsolatedRunner()
    runner.responses = ["worker-account\n"]

    with pytest.raises(PublicationPartialFailure, match="does not match"):
        _boundary(runner, tmp_path).validate_authority()

    assert len(runner.calls) == 1


def test_github_publisher_rejects_missing_repository_write_capability(tmp_path):
    runner = IsolatedRunner()
    runner.responses = ["governance-bot\n"]
    runner.repository_capability["viewerPermission"] = "READ"

    with pytest.raises(PublicationPartialFailure, match="capability is unavailable"):
        _boundary(runner, tmp_path).validate_authority()

    assert len(runner.calls) == 2


def test_github_publisher_treats_command_exception_as_unknown_terminal_outcome(
    tmp_path,
):
    runner = IsolatedRunner()
    runner.responses = [
        "governance-bot\n",
        "",
        "",
        "",
        RuntimeError("connection ended"),
    ]

    boundary = _boundary(runner, tmp_path)
    boundary.validate_authority()
    boundary.validate_action(_push_action())
    boundary.prepare_action(_push_action())
    with pytest.raises(PublicationPartialFailure, match="unknown remote outcome"):
        boundary.execute(_push_action())


def test_github_publisher_treats_an_absent_new_ref_as_safe_to_create(tmp_path):
    runner = IsolatedRunner()
    runner.responses = [
        "governance-bot\n",
        json.dumps({"data": {"repository": {"ref": None}}}),
    ]

    assert _boundary(runner, tmp_path).readback(_push_action()) is None


def test_github_publisher_distinguishes_confirmed_absence_from_ref_drift(tmp_path):
    absent = json.dumps({"data": {"repository": {"ref": None}}})
    drifted = json.dumps(
        {
            "data": {
                "repository": {
                    "ref": {
                        "target": {
                            "__typename": "Commit",
                            "oid": "b" * 40,
                            "committedDate": "2026-08-24T08:04:00Z",
                        }
                    }
                }
            }
        }
    )
    absent_runner = IsolatedRunner()
    absent_runner.responses = ["governance-bot\n", absent]
    drifted_runner = IsolatedRunner()
    drifted_runner.responses = ["governance-bot\n", drifted]

    assert _boundary(absent_runner, tmp_path / "absent").confirms_absence(
        _push_action()
    )
    assert not _boundary(drifted_runner, tmp_path / "drifted").confirms_absence(
        _push_action()
    )


def test_github_publisher_rejects_incomplete_graphql_as_ambiguous(tmp_path):
    runner = IsolatedRunner()
    runner.responses = [
        "governance-bot\n",
        json.dumps({"errors": [{"message": "partial provider failure"}]}),
    ]

    with pytest.raises(RuntimeError, match="provider errors"):
        _boundary(runner, tmp_path).confirms_absence(_push_action())


def test_github_publisher_pr_readback_requires_exact_approved_content(tmp_path):
    action = ApprovedPublicationAction(
        action_id="open-pr-a",
        map_id="I_atlas_41",
        revision=REVISION,
        action="pull_request",
        target={
            "repository": "acme/atlas",
            "head": "delivery",
            "base": "main",
            "title": "Ship Atlas",
            "body": "Verified publication scope.",
        },
    )
    runner = IsolatedRunner()
    runner.responses = [
        "governance-bot\n",
        json.dumps(
            [
                {
                    "number": 7,
                    "url": "https://github.com/acme/atlas/pull/7",
                    "headRefOid": REVISION,
                    "baseRefName": "main",
                    "createdAt": "2026-08-24T08:05:00Z",
                    "title": "Drifted title",
                    "body": "Verified publication scope.",
                }
            ]
        ),
    ]

    assert _boundary(runner, tmp_path).readback(action) is None


def test_github_publisher_pr_revision_drift_is_conflict_not_absence(tmp_path):
    action = ApprovedPublicationAction(
        action_id="open-pr-drift",
        map_id="I_atlas_41",
        revision=REVISION,
        action="pull_request",
        target={
            "repository": "acme/atlas",
            "head": "delivery",
            "base": "main",
            "title": "Ship Atlas",
            "body": "Verified publication scope.",
        },
    )
    runner = IsolatedRunner()
    runner.responses = [
        "governance-bot\n",
        json.dumps(
            [
                {
                    "number": 8,
                    "headRefOid": "b" * 40,
                    "baseRefName": "main",
                    "title": "Ship Atlas",
                    "body": "Verified publication scope.",
                }
            ]
        ),
    ]

    assert not _boundary(runner, tmp_path).confirms_absence(action)


def test_github_publisher_incomplete_pr_cannot_prove_absence(tmp_path):
    action = ApprovedPublicationAction(
        action_id="open-pr-incomplete",
        map_id="I_atlas_41",
        revision=REVISION,
        action="pull_request",
        target={
            "repository": "acme/atlas",
            "head": "delivery",
            "base": "main",
            "title": "Ship Atlas",
            "body": "Verified publication scope.",
        },
    )
    runner = IsolatedRunner()
    runner.responses = ["governance-bot\n", json.dumps([{"number": 8}])]

    with pytest.raises(RuntimeError, match="incomplete content"):
        _boundary(runner, tmp_path).confirms_absence(action)


def test_publisher_garbage_collects_only_old_owned_action_staging(tmp_path):
    config_dir = tmp_path / "publisher-gh-config"
    staging_root = config_dir / "publication-staging"
    old = staging_root / ("a" * 64)
    recent = staging_root / ("b" * 64)
    old.mkdir(parents=True)
    recent.mkdir()
    config_dir.chmod(0o700)
    staging_root.chmod(0o700)
    old_time = 1_700_000_000
    os.utime(old, (old_time, old_time))
    runner = IsolatedRunner()

    _boundary(runner, tmp_path)

    assert not old.exists()
    assert recent.exists()


def test_publisher_rejects_symlink_staging_root(tmp_path):
    config_dir = tmp_path / "publisher-gh-config"
    target = tmp_path / "outside-staging"
    config_dir.mkdir()
    config_dir.chmod(0o700)
    target.mkdir(mode=0o700)
    (config_dir / "publication-staging").symlink_to(target)

    with pytest.raises(ValueError, match="Publisher staging"):
        _boundary(IsolatedRunner(), tmp_path)


def test_worker_atomically_produces_exact_revision_bundle(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()

    class BundleRunner:
        def __init__(self):
            self.calls = []

        def run(self, command, *, cwd=None, environment=None):
            self.calls.append(tuple(command))
            if "create" in command:
                Path(command[-2]).write_bytes(b"fake exact bundle")
                return ""
            return f"{REVISION} refs/heads/exact\n"

    runner = BundleRunner()
    handoff = GitBundlePublicationHandoff(
        repositories={"acme/atlas": repository},
        handoff_root=tmp_path / "publication-handoffs",
        worker_os_user=pwd.getpwuid(os.geteuid()).pw_name,
        control_group=grp.getgrgid(os.getgid()).gr_name,
        git_executable=FAKE_GIT,
        runner=runner,
    )
    evidence = AcceptanceEvidence(
        revision=REVISION,
        delivered_scope=("Exact local revision",),
        validations=("Focused checks passed",),
        known_limitations=(),
        rollback_considerations=("Restore prior ref",),
        requested_publication_action=PublicationAction(
            action="push",
            target={"repository": "acme/atlas", "ref": "refs/heads/main"},
        ),
    )

    handoff.prepare(evidence)

    bundle = (
        tmp_path
        / "publication-handoffs"
        / hashlib.sha256(b"acme/atlas").hexdigest()
        / f"{REVISION}.bundle"
    )
    assert bundle.read_bytes() == b"fake exact bundle"
    assert bundle.stat().st_mode & 0o777 == 0o440
    assert any("bundle" in call and "create" in call for call in runner.calls)


def test_worker_produces_a_real_exact_revision_bundle(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    git = Path("/usr/bin/git")
    subprocess.run([git, "init", "--quiet"], cwd=repository, check=True)
    (repository / "delivery.txt").write_text("governed delivery\n", encoding="utf-8")
    subprocess.run([git, "add", "delivery.txt"], cwd=repository, check=True)
    subprocess.run(
        [
            git,
            "-c",
            "user.name=Map Governance Tests",
            "-c",
            "user.email=map-governance@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "delivery",
        ],
        cwd=repository,
        check=True,
    )
    revision = subprocess.run(
        [git, "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    handoff_root = tmp_path / "plugin-data" / "publication-handoffs"
    handoff_root.parent.mkdir()
    producer = GitBundlePublicationHandoff(
        repositories={"acme/atlas": repository},
        handoff_root=handoff_root,
        worker_os_user=pwd.getpwuid(os.geteuid()).pw_name,
        control_group=grp.getgrgid(os.getgid()).gr_name,
        git_executable=git,
    )
    evidence = AcceptanceEvidence(
        revision=revision,
        delivered_scope=("Exact local revision",),
        validations=("Focused checks passed",),
        known_limitations=(),
        rollback_considerations=("Restore prior ref",),
        requested_publication_action=PublicationAction(
            action="push",
            target={"repository": "acme/atlas", "ref": "refs/heads/main"},
        ),
    )

    producer.prepare(evidence)

    bundle = (
        handoff_root / hashlib.sha256(b"acme/atlas").hexdigest() / f"{revision}.bundle"
    )
    listed = subprocess.run(
        [git, "bundle", "list-heads", bundle],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert revision in listed
    assert bundle.stat().st_mode & 0o777 == 0o440


def test_release_preflight_rejects_drifted_existing_tag(tmp_path):
    action = ApprovedPublicationAction(
        action_id="release-tag-drift",
        map_id="I_atlas_41",
        revision=REVISION,
        action="release",
        target={
            "repository": "acme/atlas",
            "tag": "v1.0.0",
            "title": "Atlas 1.0",
            "notes": "Verified release notes.",
        },
    )
    runner = IsolatedRunner()
    runner.responses = [
        json.dumps({"sha": REVISION}),
        json.dumps({"data": {"repository": {"release": None}}}),
        json.dumps(
            {
                "data": {
                    "repository": {
                        "ref": {
                            "target": {
                                "__typename": "Commit",
                                "oid": "b" * 40,
                                "committedDate": "2026-08-24T08:00:00Z",
                            }
                        }
                    }
                }
            }
        ),
    ]

    with pytest.raises(ValueError, match="no longer matches"):
        _boundary(runner, tmp_path).validate_action(action)


def test_merge_execution_atomically_guards_the_approved_head_revision(tmp_path):
    action = ApprovedPublicationAction(
        action_id="merge-approved-head",
        map_id="I_atlas_41",
        revision=REVISION,
        action="merge",
        target={
            "repository": "acme/atlas",
            "pull_request": 17,
            "method": "merge",
        },
    )
    runner = IsolatedRunner()
    runner.responses = [""]

    _boundary(runner, tmp_path).execute(action)

    command = runner.calls[-1][0]
    assert "--match-head-commit" in command
    assert command[command.index("--match-head-commit") + 1] == REVISION


def test_github_publisher_release_readback_allows_an_absent_new_release(tmp_path):
    action = ApprovedPublicationAction(
        action_id="release-a",
        map_id="I_atlas_41",
        revision=REVISION,
        action="release",
        target={
            "repository": "acme/atlas",
            "tag": "v1.0.0",
            "title": "Atlas 1.0",
            "notes": "Verified release notes.",
        },
    )
    runner = IsolatedRunner()
    runner.responses = [
        "governance-bot\n",
        json.dumps({"data": {"repository": {"release": None}}}),
    ]

    assert _boundary(runner, tmp_path).readback(action) is None


def test_privileged_composition_requires_the_configured_publisher_profile(
    tmp_path, monkeypatch
):
    storage = tmp_path / "plugin-data"
    storage.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    gh_config_dir = tmp_path / "publisher-gh-config"
    gh_config_dir.mkdir(mode=0o700)
    (storage / "prerequisites.yaml").write_text(
        yaml.safe_dump(
            {
                "prerequisites": {
                    "profiles": {
                        "ceo": "ceo",
                        "pm": "pm",
                        "publisher": "publisher",
                    },
                    "authorities": {
                        "worker": {
                            "kind": "local_git",
                            "credential_ref": "local-git",
                            "os_user": _other_os_user(),
                            "git_executable": "/usr/bin/git",
                        },
                        "publisher": {
                            "kind": "gh",
                            "credential_ref": "gh:github.com:governance-bot",
                            "account": "governance-bot",
                            "gh_config_dir": str(gh_config_dir),
                            "os_user": pwd.getpwuid(os.geteuid()).pw_name,
                            "git_executable": "/usr/local/bin/git",
                            "gh_executable": "/usr/local/bin/gh",
                            "control_group": grp.getgrgid(os.getgid()).gr_name,
                            "required": True,
                        },
                    },
                    "github": {
                        "hostname": "github.com",
                        "repositories": [
                            {
                                "coordinate": "acme/atlas",
                                "path": str(repository),
                                "publication_required": True,
                            }
                        ],
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProfileResolutionError, match="Worker/PM composition"):
        application_for_storage(storage)

    monkeypatch.setattr(
        "map_governance.runtime._publisher_control_configuration",
        lambda root: yaml.safe_load((root / "prerequisites.yaml").read_text()),
    )
    monkeypatch.setattr(
        "map_governance.runtime._require_configured_process_identity",
        lambda *args, **kwargs: os.getgid(),
    )

    with pytest.raises(ProfileResolutionError, match="not configured"):
        publisher_application_for_storage(
            storage,
            request_profile_name="pm",
        )

    stored = yaml.safe_load((storage / "prerequisites.yaml").read_text())
    stored["prerequisites"]["authorities"]["worker"]["os_user"] = pwd.getpwuid(
        os.geteuid()
    ).pw_name
    (storage / "prerequisites.yaml").write_text(
        yaml.safe_dump(stored), encoding="utf-8"
    )
    with pytest.raises(ProfileResolutionError, match="OS service identity"):
        publisher_application_for_storage(
            storage,
            request_profile_name="publisher",
        )
    stored["prerequisites"]["authorities"]["worker"]["os_user"] = _other_os_user()
    (storage / "prerequisites.yaml").write_text(
        yaml.safe_dump(stored), encoding="utf-8"
    )

    monkeypatch.setenv("GH_TOKEN", "ambient-token-must-not-cross-boundary")
    application = publisher_application_for_storage(
        storage,
        request_profile_name="publisher",
    )
    assert application._publisher is not None
    assert application._publisher.profile_name == "publisher"
    assert application._tracker._executable == str(Path("/usr/local/bin/gh").resolve())
    assert application._tracker._executable == application._publisher.tracker_executable
    tracker_environment = application._tracker._runner._environment
    assert tracker_environment == application._publisher.tracker_environment
    assert tracker_environment["GH_CONFIG_DIR"] == str(gh_config_dir)
    assert tracker_environment["GH_HOST"] == "github.com"
    assert "GH_TOKEN" not in tracker_environment


def test_profile_composition_uses_control_profile_storage_not_publisher_storage(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / "hermes-home"
    ceo_home = hermes_home / "profiles" / "ceo"
    publisher_home = hermes_home / "profiles" / "publisher"
    ceo_home.mkdir(parents=True)
    publisher_home.mkdir(parents=True)
    plugin_settings = {
        "plugins": {
            "enabled": ["map-governance"],
            "entries": {"map-governance": {"settings": {}}},
        }
    }
    for profile_home in (ceo_home, publisher_home):
        (profile_home / "config.yaml").write_text(
            yaml.safe_dump(plugin_settings), encoding="utf-8"
        )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from hermes_cli.plugins import PluginState
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(ceo_home)
    try:
        control_storage = PluginState("map-governance").data_dir
    finally:
        reset_hermes_home_override(token)
    token = set_hermes_home_override(publisher_home)
    try:
        publisher_storage = PluginState("map-governance").data_dir
    finally:
        reset_hermes_home_override(token)
    control_storage.mkdir(parents=True)
    repository = tmp_path / "repository"
    repository.mkdir()
    gh_config_dir = tmp_path / "publisher-gh-config"
    gh_config_dir.mkdir(mode=0o700)
    (control_storage / "prerequisites.yaml").write_text(
        yaml.safe_dump(
            {
                "prerequisites": {
                    "profiles": {
                        "ceo": "ceo",
                        "pm": "pm",
                        "publisher": "publisher",
                    },
                    "authorities": {
                        "worker": {
                            "kind": "local_git",
                            "credential_ref": "local-git",
                            "os_user": _other_os_user(),
                            "git_executable": "/usr/bin/git",
                        },
                        "publisher": {
                            "kind": "gh",
                            "credential_ref": "gh:github.com:governance-bot",
                            "account": "governance-bot",
                            "gh_config_dir": str(gh_config_dir),
                            "os_user": pwd.getpwuid(os.geteuid()).pw_name,
                            "git_executable": "/usr/local/bin/git",
                            "gh_executable": "/usr/local/bin/gh",
                            "control_group": grp.getgrgid(os.getgid()).gr_name,
                            "required": True,
                        },
                    },
                    "github": {
                        "hostname": "github.com",
                        "repositories": [
                            {
                                "coordinate": "acme/atlas",
                                "path": str(repository),
                                "publication_required": True,
                            }
                        ],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "map_governance.runtime._publisher_control_configuration",
        lambda root: yaml.safe_load((root / "prerequisites.yaml").read_text()),
    )
    monkeypatch.setattr(
        "map_governance.runtime._require_configured_process_identity",
        lambda *args, **kwargs: os.getgid(),
    )

    application = publisher_application_for_profile(
        "ceo", request_profile_name="publisher"
    )

    assert application._storage.root == control_storage.resolve()
    assert application._storage.root != publisher_storage.resolve()


def test_profile_application_cache_rebuilds_when_setup_revision_changes(
    tmp_path, monkeypatch
):
    import map_governance.runtime as runtime

    hermes_home = tmp_path / "hermes-home"
    profile_home = hermes_home / "profiles" / "ceo"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "enabled": ["map-governance"],
                    "entries": {"map-governance": {"settings": {}}},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    runtime._PROFILE_APPLICATIONS.clear()

    first = application_for_profile("ceo")
    config_path = first._storage.root / "prerequisites.yaml"
    config_path.write_text("prerequisites: {}\n", encoding="utf-8")
    second = application_for_profile("ceo")

    assert second is not first
    assert first._outbox_runtime is None
    second.stop_outbox_runtime()
    runtime._PROFILE_APPLICATIONS.clear()


def test_profile_application_cache_retries_config_change_during_construction(
    tmp_path, monkeypatch
):
    import map_governance.runtime as runtime
    from map_governance.prerequisites import YamlConfigRepository

    hermes_home = tmp_path / "hermes-home"
    profile_home = hermes_home / "profiles" / "ceo"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "enabled": ["map-governance"],
                    "entries": {"map-governance": {"settings": {}}},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    runtime._PROFILE_APPLICATIONS.clear()
    real_composition = runtime.application_for_storage
    calls = []

    def racing_composition(storage_root, *args, **kwargs):
        application = real_composition(storage_root, *args, **kwargs)
        calls.append(application)
        if len(calls) == 1:
            (storage_root / "prerequisites.yaml").write_text(
                "prerequisites: {}\n", encoding="utf-8"
            )
        return application

    monkeypatch.setattr(runtime, "application_for_storage", racing_composition)

    selected = application_for_profile("ceo")

    assert len(calls) == 2
    assert selected is calls[-1]
    cache_key = next(iter(runtime._PROFILE_APPLICATIONS))
    repository = YamlConfigRepository(
        selected._storage.root / "prerequisites.yaml",
        storage_root=selected._storage.root,
    )
    assert cache_key[2] == repository.revision()
    selected.stop_outbox_runtime()
    runtime._PROFILE_APPLICATIONS.clear()


def test_worker_prepares_shared_control_storage_for_publisher_identity(
    tmp_path, monkeypatch
):
    import map_governance.runtime as runtime

    storage = tmp_path / "plugin-data"
    storage.mkdir()
    worker = pwd.getpwuid(os.geteuid())
    publisher_name = _other_os_user()
    publisher = pwd.getpwnam(publisher_name)
    control_group = grp.getgrgid(os.getgid())
    config_path = storage / "prerequisites.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "prerequisites": {
                    "authorities": {
                        "worker": {"os_user": worker.pw_name},
                        "publisher": {
                            "os_user": publisher_name,
                            "control_group": control_group.gr_name,
                            "required": True,
                        },
                    },
                    "github": {"repositories": []},
                }
            }
        ),
        encoding="utf-8",
    )
    shared_group = SimpleNamespace(
        gr_gid=control_group.gr_gid,
        gr_name=control_group.gr_name,
        gr_mem=[worker.pw_name, publisher_name],
    )
    monkeypatch.setattr(runtime.grp, "getgrnam", lambda name: shared_group)
    monkeypatch.setattr(runtime.grp, "getgrall", lambda: [shared_group])

    shared_gid = runtime._require_configured_process_identity(
        storage, publisher_process=False
    )
    assert shared_gid == control_group.gr_gid
    PluginStorage(storage, shared_gid=shared_gid).check_readiness()
    assert storage.stat().st_mode & 0o7777 == 0o2770
    assert config_path.stat().st_mode & 0o777 == 0o640
    assert (storage / "registry.db").stat().st_mode & 0o777 == 0o660

    monkeypatch.setattr(runtime.os, "geteuid", lambda: publisher.pw_uid)
    stored = runtime._publisher_control_configuration(storage)
    assert stored["prerequisites"]["authorities"]["worker"]["os_user"] == (
        worker.pw_name
    )
