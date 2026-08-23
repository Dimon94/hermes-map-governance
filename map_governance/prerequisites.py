"""Explicit setup planning and read-only prerequisite diagnostics.

The operational governance application owns durable state.  This module is a
separate public seam because doctor must be usable before that state exists and
must never initialize the registry merely to inspect it.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlparse

import yaml


from .prerequisites_setup import (
    PLUGIN_ID,
    REQUIRED_EXTERNAL_SKILLS,
    ROUTING_INTEGRATIONS,
    ConfigCommitError,
    ConfigRepository,
    SetupApplyError,
    SetupWorkflow,
    YamlConfigRepository,
    _normalize_desired,
    _prerequisite_settings,
    _redact,
    _timestamp,
)

__all__ = [
    "CommandResult",
    "PrerequisiteApplication",
    "ReadOnlyCommandRunner",
    "SetupApplyError",
    "YamlConfigRepository",
]

_PROJECT_QUERY_ORGANIZATION = """
query MapGovernanceDoctorOrganizationProject($owner: String!, $number: Int!) {
  organization(login: $owner) {
    projectV2(number: $number) { id url viewerCanUpdate }
  }
}
""".strip()
_PROJECT_QUERY_USER = _PROJECT_QUERY_ORGANIZATION.replace(
    "MapGovernanceDoctorOrganizationProject",
    "MapGovernanceDoctorUserProject",
).replace("organization(login: $owner)", "user(login: $owner)")


@dataclass(frozen=True)
class CommandResult:
    arguments: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


class CommandRunner(Protocol):
    def run(self, arguments: Sequence[str], *, timeout: float) -> CommandResult: ...


class ReadOnlyCommandRunner:
    """Run preconstructed read-only argv without invoking a shell."""

    def run(self, arguments: Sequence[str], *, timeout: float) -> CommandResult:
        argv = tuple(str(item) for item in arguments)
        try:
            completed = subprocess.run(
                list(argv),
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(argv, None, "", "", timed_out=True)
        except OSError:
            return CommandResult(argv, 127, "", "")
        return CommandResult(
            argv,
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )


def _check(
    check_id: str,
    status: str,
    summary: str,
    evidence: Mapping[str, Any] | None = None,
    *,
    remediation: str | None = None,
    command: Sequence[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": check_id,
        "status": status,
        "summary": summary,
        "evidence": _redact(dict(evidence or {})),
    }
    if remediation is not None:
        remediation_payload: dict[str, Any] = {"description": remediation}
        if command is not None:
            remediation_payload["command"] = list(command)
        result["remediation"] = remediation_payload
    return result


def _command_outcome(result: CommandResult) -> str:
    if result.timed_out:
        return "timeout"
    if result.returncode != 0:
        return "denied" if result.returncode in {1, 2, 4} else "unavailable"
    return "success"


def _parse_yaml_file(path: Path) -> dict[str, Any] | None:
    try:
        parsed = yaml.safe_load(path.read_bytes())
    except (OSError, yaml.YAMLError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _skill_name(path: Path) -> str | None:
    config = _parse_yaml_frontmatter(path)
    name = config.get("name") if config is not None else None
    return str(name).strip() if isinstance(name, str) and name.strip() else None


def _parse_yaml_frontmatter(path: Path) -> dict[str, Any] | None:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not content.startswith("---\n"):
        return None
    marker = content.find("\n---", 4)
    if marker < 0:
        return None
    try:
        parsed = yaml.safe_load(content[4:marker])
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


class PrerequisiteApplication:
    """Public setup/doctor seam shared by native and dashboard adapters."""

    def __init__(
        self,
        *,
        plugin_root: Path,
        storage_root: Path,
        config_repository: ConfigRepository,
        profile_resolver: Callable[[str], Path],
        runner: CommandRunner | None = None,
        clock: Callable[[], datetime] | None = None,
        command_timeout: float = 10.0,
    ) -> None:
        self._plugin_root = plugin_root.resolve()
        self._storage_root = Path(os.path.abspath(os.fspath(storage_root)))
        self._config = config_repository
        self._profile_resolver = profile_resolver
        self._runner = runner or ReadOnlyCommandRunner()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._command_timeout = command_timeout
        self._setup = SetupWorkflow(
            config_repository=config_repository,
            clock=self._clock,
        )

    def setup_plan(self, *, desired: Mapping[str, Any]) -> dict[str, Any]:
        return self._setup.setup_plan(desired=desired)

    def setup_apply(
        self,
        *,
        plan: Mapping[str, Any],
        selected_action_ids: Sequence[str],
    ) -> dict[str, Any]:
        return self._setup.setup_apply(
            plan=plan, selected_action_ids=selected_action_ids
        )

    def doctor(self) -> dict[str, Any]:
        """Inspect every configured prerequisite without creating state."""
        checks: list[dict[str, Any]] = []
        config_readable = True
        try:
            config = self._config.read()
        except (ConfigCommitError, OSError):
            config = {}
            config_readable = False
        settings = _prerequisite_settings(config)
        raw_desired = settings.get("prerequisites") if settings else None
        try:
            if settings is not None and not isinstance(raw_desired, Mapping):
                raise ValueError("Stored prerequisites must be an object")
            desired = (
                _normalize_desired(raw_desired)
                if isinstance(raw_desired, Mapping)
                else None
            )
        except ValueError:
            desired = None
            checks.append(
                _check(
                    "configuration",
                    "fail",
                    "Map Governance prerequisite configuration is invalid",
                    {"outcome": "invalid"},
                    remediation=(
                        "Generate a new secret-free setup plan and explicitly apply "
                        "its config action."
                    ),
                    command=["hermes", "maps", "setup", "plan", "--file", "SETUP.json"],
                )
            )
        else:
            checks.append(
                _check(
                    "configuration",
                    "pass" if desired is not None and config_readable else "fail",
                    (
                        "Prerequisite configuration is valid"
                        if desired is not None and config_readable
                        else (
                            "Plugin prerequisite config is unreadable"
                            if not config_readable
                            else "Prerequisite configuration has not been applied"
                        )
                    ),
                    {
                        "readable": config_readable,
                        "schema_version": (
                            desired["schema_version"] if desired is not None else None
                        ),
                    },
                    remediation=(
                        None
                        if desired is not None
                        else "Generate a setup plan and explicitly apply its config action."
                    ),
                    command=(
                        None
                        if desired is not None
                        else ["hermes", "maps", "setup", "plan", "--file", "SETUP.json"]
                    ),
                )
            )

        profile_configs = self._doctor_profiles(checks, desired)
        self._doctor_skills(checks, desired, profile_configs)
        registry = self._doctor_storage(checks)
        auth = self._doctor_github(checks, desired)
        self._doctor_bindings(checks, desired, registry)
        self._doctor_herdr(checks, desired)

        failure_count = sum(check["status"] == "fail" for check in checks)
        warning_count = sum(check["status"] == "warning" for check in checks)
        pass_count = sum(check["status"] == "pass" for check in checks)
        return {
            "status": "fail" if failure_count else "pass",
            "checked_at": _timestamp(self._clock()),
            "summary": {
                "pass": pass_count,
                "warning": warning_count,
                "fail": failure_count,
            },
            "authority": {
                "worker_ready": self._authority_ready(checks, suffix=".worker"),
                "publisher_ready": self._authority_ready(checks, suffix=".publisher"),
                "github_account": auth,
            },
            "checks": checks,
        }

    @staticmethod
    def _authority_ready(checks: Sequence[Mapping[str, Any]], *, suffix: str) -> bool:
        relevant = [
            check for check in checks if str(check.get("id", "")).endswith(suffix)
        ]
        return bool(relevant) and all(
            check.get("status") == "pass" for check in relevant
        )

    def _doctor_profiles(
        self,
        checks: list[dict[str, Any]],
        desired: Mapping[str, Any] | None,
    ) -> dict[str, dict[str, Any] | None]:
        if desired is None:
            checks.append(
                _check(
                    "profiles.separation",
                    "fail",
                    "CEO and PM profiles cannot be resolved without configuration",
                    {"distinct": False},
                    remediation="Apply the prerequisite configuration with distinct CEO and PM profile IDs.",
                )
            )
            for role in ("ceo", "pm"):
                checks.append(
                    _check(
                        f"profiles.{role}",
                        "fail",
                        f"{role.upper()} profile cannot be verified without configuration",
                        {"configured": False, "verified": False},
                        remediation=f"Configure the {role.upper()} profile ID in an explicit setup plan.",
                    )
                )
            return {}
        ceo = str(desired["profiles"]["ceo"])
        pm = str(desired["profiles"]["pm"])
        distinct = ceo != pm
        checks.append(
            _check(
                "profiles.separation",
                "pass" if distinct else "fail",
                (
                    "CEO and PM use independent Hermes profiles"
                    if distinct
                    else "CEO and PM must not share one Hermes profile"
                ),
                {"ceo_profile": ceo, "pm_profile": pm, "distinct": distinct},
                remediation=(
                    None
                    if distinct
                    else "Choose a separate PM profile and generate a new setup plan."
                ),
            )
        )
        configs: dict[str, dict[str, Any] | None] = {}
        for role, profile, required, forbidden in (
            ("ceo", ceo, "map-governance-ceo", "map-governance-pm"),
            ("pm", pm, "map-governance-pm", "map-governance-ceo"),
        ):
            home = self._profile_resolver(profile)
            config = _parse_yaml_file(home / "config.yaml") if home.is_dir() else None
            configs[role] = config
            plugins = config.get("plugins") if isinstance(config, Mapping) else None
            enabled = plugins.get("enabled") if isinstance(plugins, Mapping) else None
            toolsets = config.get("toolsets") if isinstance(config, Mapping) else None
            ready = (
                config is not None
                and isinstance(enabled, list)
                and PLUGIN_ID in enabled
                and isinstance(toolsets, list)
                and required in toolsets
                and forbidden not in toolsets
            )
            checks.append(
                _check(
                    f"profiles.{role}",
                    "pass" if ready else "fail",
                    (
                        f"{role.upper()} profile has its role-specific plugin toolset"
                        if ready
                        else f"{role.upper()} profile is missing or has an unsafe role configuration"
                    ),
                    {
                        "profile": profile,
                        "exists": home.is_dir(),
                        "plugin_enabled": isinstance(enabled, list)
                        and PLUGIN_ID in enabled,
                        "required_toolset": required,
                        "required_toolset_enabled": isinstance(toolsets, list)
                        and required in toolsets,
                        "opposite_toolset_absent": not isinstance(toolsets, list)
                        or forbidden not in toolsets,
                    },
                    remediation=(
                        None
                        if ready
                        else "Enable the plugin and only the role-specific Map Governance toolset in this profile."
                    ),
                    command=(
                        None if ready else ["hermes", "-p", profile, "config", "edit"]
                    ),
                )
            )
        return configs

    def _doctor_skills(
        self,
        checks: list[dict[str, Any]],
        desired: Mapping[str, Any] | None,
        profile_configs: Mapping[str, dict[str, Any] | None],
    ) -> None:
        for qualified, relative in (
            ("map-governance:ceo", Path("skills/ceo/SKILL.md")),
            ("map-governance:pm", Path("skills/pm/SKILL.md")),
        ):
            expected_name = qualified.rsplit(":", 1)[1]
            path = self._plugin_root / relative
            valid = path.is_file() and _skill_name(path) == expected_name
            checks.append(
                _check(
                    f"skills.plugin.{expected_name}",
                    "pass" if valid else "fail",
                    (
                        f"Plugin Skill {qualified} is readable"
                        if valid
                        else f"Plugin Skill {qualified} is missing or malformed"
                    ),
                    {"skill_id": qualified, "readable": valid},
                    remediation=(
                        None
                        if valid
                        else "Reinstall the Map Governance plugin package."
                    ),
                    command=(
                        None if valid else ["hermes", "plugins", "doctor", PLUGIN_ID]
                    ),
                )
            )
        if desired is None:
            for skill_id in REQUIRED_EXTERNAL_SKILLS:
                checks.append(
                    _check(
                        f"skills.external.{skill_id}",
                        "fail",
                        f"External owner Skill {skill_id} is not configured",
                        {"skill_id": skill_id, "readable": False},
                        remediation="Select the existing Skill path in a new setup plan; doctor will not install it.",
                    )
                )
            return
        external_by_id = {
            str(item["id"]): item for item in desired["skills"]["external"]
        }
        for skill_id in REQUIRED_EXTERNAL_SKILLS:
            item = external_by_id.get(skill_id)
            path = Path(str(item["path"])) if item is not None else Path()
            root = path.parent.parent if item is not None else None
            discovered_by: list[str] = []
            for role, config in profile_configs.items():
                skills = config.get("skills") if isinstance(config, Mapping) else None
                external_dirs = (
                    skills.get("external_dirs") if isinstance(skills, Mapping) else None
                )
                if isinstance(external_dirs, list) and root is not None:
                    try:
                        configured = {
                            Path(str(value)).resolve() for value in external_dirs
                        }
                    except OSError:
                        configured = set()
                    if root.resolve() in configured:
                        discovered_by.append(role)
            valid_file = (
                item is not None and path.is_file() and _skill_name(path) == skill_id
            )
            valid = valid_file and set(discovered_by) == {"ceo", "pm"}
            checks.append(
                _check(
                    f"skills.external.{skill_id}",
                    "pass" if valid else "fail",
                    (
                        f"External owner Skill {skill_id} is available to CEO and PM"
                        if valid
                        else f"External owner Skill {skill_id} is missing or undiscoverable"
                    ),
                    {
                        "skill_id": skill_id,
                        "readable": valid_file,
                        "discovered_by_profiles": discovered_by,
                    },
                    remediation=(
                        None
                        if valid
                        else "Restore the existing Skill and explicitly add its root to both profiles' skills.external_dirs."
                    ),
                )
            )

    def _doctor_storage(
        self, checks: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]] | None:
        root = self._storage_root
        try:
            metadata = root.lstat()
            path_exists = True
            path_safe = stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(
                metadata.st_mode
            )
        except OSError:
            metadata = None
            path_exists = False
            path_safe = False
        checks.append(
            _check(
                "storage.path",
                "pass" if path_safe else "fail",
                (
                    "Plugin storage directory exists"
                    if path_safe
                    else (
                        "Plugin storage path is unsafe"
                        if path_exists
                        else "Plugin storage directory is absent"
                    )
                ),
                {
                    "exists": path_exists,
                    "safe_directory": path_safe,
                    "namespace": PLUGIN_ID,
                },
                remediation=(
                    None
                    if path_safe
                    else "Start the enabled plugin once after setup; doctor will not create storage."
                ),
            )
        )
        if not path_safe:
            checks.extend(
                (
                    _check(
                        "storage.ownership",
                        "fail",
                        "Storage ownership cannot be verified",
                        {"owner_matches_process": False},
                    ),
                    _check(
                        "storage.permissions",
                        "fail",
                        "Storage write permissions cannot be verified",
                        {"writable_by_owner_mode": False},
                    ),
                    _check(
                        "storage.database",
                        "fail",
                        "Registry database is absent",
                        {"read_only_open": False},
                    ),
                )
            )
            return None
        try:
            assert metadata is not None
            owner_matches = (
                not hasattr(os, "geteuid") or metadata.st_uid == os.geteuid()
            )
            mode = stat.S_IMODE(metadata.st_mode)
            writable = bool(mode & stat.S_IWUSR) and bool(mode & stat.S_IXUSR)
        except OSError:
            owner_matches = False
            writable = False
        checks.append(
            _check(
                "storage.ownership",
                "pass" if owner_matches else "fail",
                (
                    "Plugin storage is owned by the Hermes process user"
                    if owner_matches
                    else "Plugin storage ownership does not match the Hermes process user"
                ),
                {"owner_matches_process": owner_matches},
                remediation=(
                    None
                    if owner_matches
                    else "Correct storage ownership outside doctor, then rerun the check."
                ),
            )
        )
        checks.append(
            _check(
                "storage.permissions",
                "pass" if writable else "fail",
                (
                    "Plugin storage has owner write permission"
                    if writable
                    else "Plugin storage is not writable by its owner mode"
                ),
                {"writable_by_owner_mode": writable},
                remediation=(
                    None
                    if writable
                    else "Grant the owning user write and directory traversal permission outside doctor."
                ),
            )
        )
        database = root / "registry.db"
        registry: dict[str, list[dict[str, Any]]] | None = None
        try:
            database_metadata = database.lstat()
            if (
                not stat.S_ISREG(database_metadata.st_mode)
                or stat.S_ISLNK(database_metadata.st_mode)
                or database_metadata.st_nlink != 1
                or (hasattr(os, "geteuid") and database_metadata.st_uid != os.geteuid())
            ):
                raise OSError("missing")
            uri = database.as_uri() + "?mode=ro&immutable=1"
            with sqlite3.connect(uri, uri=True) as connection:
                connection.row_factory = sqlite3.Row
                metadata_row = connection.execute(
                    "SELECT schema_version FROM plugin_metadata WHERE namespace = ?",
                    (PLUGIN_ID,),
                ).fetchone()
                if metadata_row is None:
                    raise sqlite3.DatabaseError("metadata missing")
                projects = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT project_id, project_url FROM ceo_projects ORDER BY project_id"
                    )
                ]
                maps = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT binding.map_id, binding.project_id, projection.repository
                        FROM map_bindings AS binding
                        JOIN map_projections AS projection USING(map_id)
                        ORDER BY binding.map_id
                        """
                    )
                ]
                registry = {"projects": projects, "maps": maps}
            database_ready = True
            schema_version = int(metadata_row["schema_version"])
        except (OSError, sqlite3.Error, TypeError, ValueError):
            database_ready = False
            schema_version = None
        checks.append(
            _check(
                "storage.database",
                "pass" if database_ready else "fail",
                (
                    "Registry opens through SQLite immutable read-only mode"
                    if database_ready
                    else "Registry is absent, unreadable, or malformed"
                ),
                {
                    "read_only_open": database_ready,
                    "schema_version": schema_version,
                },
                remediation=(
                    None
                    if database_ready
                    else "Start or repair the plugin through an explicit operator workflow; doctor will not initialize SQLite."
                ),
            )
        )
        return registry

    @staticmethod
    def project_command(
        *, executable: str, hostname: str, owner: str, owner_type: str, number: int
    ) -> list[str]:
        query = (
            _PROJECT_QUERY_ORGANIZATION
            if owner_type == "organization"
            else _PROJECT_QUERY_USER
        )
        return [
            executable,
            "api",
            "--hostname",
            hostname,
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"number={number}",
        ]

    def _run(self, arguments: Sequence[str]) -> CommandResult:
        return self._runner.run(arguments, timeout=self._command_timeout)

    @staticmethod
    def _json_result(result: CommandResult) -> tuple[dict[str, Any] | None, str]:
        outcome = _command_outcome(result)
        if outcome != "success":
            return None, outcome
        try:
            payload = json.loads(result.stdout)
        except (TypeError, ValueError):
            return None, "invalid_json"
        return (
            (payload, "success")
            if isinstance(payload, dict)
            else (None, "invalid_json")
        )

    def _doctor_github(
        self,
        checks: list[dict[str, Any]],
        desired: Mapping[str, Any] | None,
    ) -> str | None:
        if desired is None:
            checks.extend(
                (
                    _check(
                        "github.auth",
                        "fail",
                        "GitHub auth context is not configured",
                        {"outcome": "not_configured"},
                        remediation="Apply GitHub hostname and selected resource configuration; credentials remain in gh/keychain.",
                    ),
                    _check(
                        "github.projects",
                        "fail",
                        "GitHub Project capabilities cannot be verified",
                        {"configured": False, "verified": False},
                    ),
                    _check(
                        "github.repositories",
                        "fail",
                        "Repository access cannot be verified",
                        {"configured": False, "verified": False},
                    ),
                    _check(
                        "authority.separation",
                        "fail",
                        "Worker and publisher authority classes cannot be verified",
                        {"configured": False, "distinct": False},
                    ),
                    _check(
                        "authority.worker",
                        "fail",
                        "Execution worker authority cannot be verified",
                        {"configured": False, "verified": False},
                    ),
                    _check(
                        "authority.publisher",
                        "warning",
                        "Publisher authority is not configured",
                        {"configured": False, "required": False, "verified": False},
                    ),
                )
            )
            return None
        github = desired["github"]
        host = str(github["hostname"])
        auth_arguments = [
            "gh",
            "auth",
            "status",
            "--active",
            "--hostname",
            host,
            "--json",
            "hosts",
        ]
        payload, outcome = self._json_result(self._run(auth_arguments))
        account = None
        scopes: list[str] = []
        if payload is not None:
            hosts = payload.get("hosts")
            accounts = hosts.get(host) if isinstance(hosts, Mapping) else None
            active = next(
                (
                    item
                    for item in accounts or []
                    if isinstance(item, Mapping) and item.get("active") is True
                ),
                None,
            )
            if active is None or active.get("state") != "success":
                outcome = "denied"
            else:
                account = str(active.get("login") or "") or None
                raw_scopes = active.get("scopes")
                if isinstance(raw_scopes, str):
                    scopes = sorted(
                        item.strip() for item in raw_scopes.split(",") if item.strip()
                    )
        auth_ready = outcome == "success" and account is not None
        checks.append(
            _check(
                "github.auth",
                "pass" if auth_ready else "fail",
                (
                    "GitHub CLI has an active authenticated account"
                    if auth_ready
                    else "GitHub CLI authentication is unavailable"
                ),
                {
                    "hostname": host,
                    "account": account,
                    "scopes": scopes,
                    "outcome": outcome,
                },
                remediation=(
                    None
                    if auth_ready
                    else "Authenticate gh explicitly for the configured host; doctor does not change credentials."
                ),
                command=(
                    None if auth_ready else ["gh", "auth", "login", "--hostname", host]
                ),
            )
        )

        for project in github["projects"]:
            arguments = self.project_command(
                executable="gh",
                hostname=host,
                owner=str(project["owner"]),
                owner_type=str(project["owner_type"]),
                number=int(project["number"]),
            )
            project_payload, project_outcome = self._json_result(self._run(arguments))
            owner_field = (
                "organization" if project["owner_type"] == "organization" else "user"
            )
            resource = None
            if project_payload is not None:
                owner_payload = project_payload.get("data", {}).get(owner_field)
                resource = (
                    owner_payload.get("projectV2")
                    if isinstance(owner_payload, Mapping)
                    else None
                )
            identity_matches = (
                isinstance(resource, Mapping)
                and resource.get("id") == project["id"]
                and str(resource.get("url", "")).rstrip("/") == project["url"]
            )
            readable = project_outcome == "success" and identity_matches
            can_update = (
                readable
                and isinstance(resource, Mapping)
                and resource.get("viewerCanUpdate") is True
            )
            checks.append(
                _check(
                    f"github.project.{project['id']}.read",
                    "pass" if readable else "fail",
                    (
                        "Selected GitHub Project identity is readable"
                        if readable
                        else "Selected GitHub Project is unreadable or changed identity"
                    ),
                    {
                        "project_id": project["id"],
                        "outcome": project_outcome,
                        "identity_matches": bool(identity_matches),
                    },
                    remediation=(
                        None
                        if readable
                        else "Grant the configured governance account Project read access and verify the binding coordinates."
                    ),
                )
            )
            write_required = project["required_capability"] == "write"
            checks.append(
                _check(
                    f"github.project.{project['id']}.write",
                    "pass" if can_update or not write_required else "fail",
                    (
                        "Selected GitHub Project grants update capability"
                        if can_update
                        else (
                            "Selected GitHub Project update capability is not required"
                            if not write_required
                            else "Selected GitHub Project does not grant update capability"
                        )
                    ),
                    {
                        "project_id": project["id"],
                        "required": write_required,
                        "viewer_can_update": bool(can_update),
                    },
                    remediation=(
                        None
                        if can_update or not write_required
                        else "Grant Project write capability to the governance account."
                    ),
                )
            )

        permission_levels = {
            "READ": 1,
            "TRIAGE": 1,
            "WRITE": 2,
            "MAINTAIN": 2,
            "ADMIN": 3,
        }
        issue_write_permissions = {"TRIAGE", "WRITE", "MAINTAIN", "ADMIN"}
        worker_ref = desired["authorities"]["worker"]["credential_ref"]
        publisher = desired["authorities"]["publisher"]
        separate_authority = worker_ref != publisher["credential_ref"]
        checks.append(
            _check(
                "authority.separation",
                "pass" if separate_authority else "fail",
                (
                    "Worker and publisher authority classes use distinct references"
                    if separate_authority
                    else "Worker and publisher authority references are merged"
                ),
                {
                    "worker_kind": desired["authorities"]["worker"]["kind"],
                    "publisher_kind": publisher["kind"],
                    "distinct": separate_authority,
                },
            )
        )
        worker_context_matches = (
            desired["authorities"]["worker"]["kind"] == "local_git"
            and worker_ref == "local-git"
        )
        checks.append(
            _check(
                "authority.worker",
                "pass" if worker_context_matches else "fail",
                (
                    "Execution worker uses the local Git authority class"
                    if worker_context_matches
                    else "Execution worker authority class is invalid"
                ),
                {
                    "kind": desired["authorities"]["worker"]["kind"],
                    "local_git_reference": worker_context_matches,
                    "remote_publication_required": False,
                },
            )
        )
        publisher_context_matches = (
            publisher["kind"] == "gh"
            and publisher["credential_ref"] == f"gh:{host}:{publisher['account']}"
            and bool(account)
            and publisher["account"] == account
        )
        publisher_required = bool(publisher["required"])
        checks.append(
            _check(
                "authority.publisher",
                (
                    "pass"
                    if publisher_context_matches
                    else "fail"
                    if publisher_required
                    else "warning"
                ),
                (
                    "Publisher provider reference matches the active gh account"
                    if publisher_context_matches
                    else (
                        "Required publisher provider reference is unavailable"
                        if publisher_required
                        else "Publisher provider verification is not required"
                    )
                ),
                {
                    "kind": publisher["kind"],
                    "provider_reference": publisher["credential_ref"],
                    "expected_account": publisher["account"] or None,
                    "active_account_matches": publisher_context_matches,
                    "required": publisher_required,
                },
                remediation=(
                    None
                    if publisher_context_matches
                    else (
                        "Activate the referenced gh host/account before publication; doctor never copies its credential."
                        if publisher_required
                        else "No action is needed until publication authority is explicitly required."
                    )
                ),
            )
        )
        for repository in github["repositories"]:
            coordinate = str(repository["coordinate"])
            repo_payload, repo_outcome = self._json_result(
                self._run(
                    [
                        "gh",
                        "repo",
                        "view",
                        f"{host}/{coordinate}",
                        "--json",
                        "nameWithOwner,url,viewerPermission,isArchived",
                    ]
                )
            )
            permission = (
                str(repo_payload.get("viewerPermission") or "").upper()
                if repo_payload is not None
                else ""
            )
            remote_read = (
                repo_outcome == "success"
                and repo_payload is not None
                and str(repo_payload.get("nameWithOwner", "")).casefold()
                == coordinate.casefold()
                and repo_payload.get("isArchived") is False
                and permission_levels.get(permission, 0) >= 1
            )
            checks.append(
                _check(
                    f"repository.{coordinate}.read",
                    "pass" if remote_read else "fail",
                    (
                        "Selected repository is readable"
                        if remote_read
                        else "Selected repository is unreadable, archived, or changed identity"
                    ),
                    {
                        "coordinate": coordinate,
                        "outcome": repo_outcome,
                        "permission": permission or None,
                    },
                )
            )
            governance_write = remote_read and permission in issue_write_permissions
            checks.append(
                _check(
                    f"repository.{coordinate}.governance",
                    "pass" if governance_write else "fail",
                    (
                        "Governance account can update repository Issues"
                        if governance_write
                        else "Governance account lacks repository Issue write authority"
                    ),
                    {
                        "coordinate": coordinate,
                        "issue_write_capability": governance_write,
                    },
                    remediation=(
                        None
                        if governance_write
                        else "Grant Issue write authority to the governance account; do not reuse worker credentials."
                    ),
                )
            )
            path = Path(str(repository["path"]))
            local_ready = path.is_dir()
            owner_writable = False
            top_matches = False
            remote_matches = False
            if local_ready:
                try:
                    mode = stat.S_IMODE(path.stat().st_mode)
                    owner_writable = bool(mode & stat.S_IWUSR) and (
                        not hasattr(os, "geteuid") or path.stat().st_uid == os.geteuid()
                    )
                except OSError:
                    owner_writable = False
                top = self._run(
                    ["git", "-C", str(path), "rev-parse", "--show-toplevel"]
                )
                remote = self._run(
                    ["git", "-C", str(path), "remote", "get-url", "origin"]
                )
                try:
                    top_matches = (
                        top.returncode == 0
                        and Path(top.stdout.strip()).resolve() == path.resolve()
                    )
                except OSError:
                    top_matches = False
                remote_host, remote_coordinate = self._repository_identity(
                    remote.stdout.strip()
                )
                remote_matches = (
                    remote.returncode == 0
                    and remote_host == host.casefold()
                    and remote_coordinate.casefold() == coordinate.casefold()
                )
            worker_ready = (
                local_ready
                and top_matches
                and remote_matches
                and (owner_writable if repository["worker_write_required"] else True)
            )
            checks.append(
                _check(
                    f"repository.{coordinate}.worker",
                    "pass" if worker_ready else "fail",
                    (
                        "Execution worker has verified local Git write authority"
                        if worker_ready
                        else "Execution worker local Git authority is unavailable"
                    ),
                    {
                        "coordinate": coordinate,
                        "local_repository": local_ready and top_matches,
                        "origin_matches": remote_matches,
                        "owner_write_permission": owner_writable,
                        "remote_publication_required": False,
                    },
                    remediation=(
                        None
                        if worker_ready
                        else "Restore the selected local repository coordinate and owner write permission; no publisher credential is needed."
                    ),
                )
            )
            publication_required = bool(repository["publication_required"])
            publisher_ready = (
                publication_required
                and separate_authority
                and publisher_context_matches
                and publisher["required"] is True
                and permission_levels.get(permission, 0) >= 2
            )
            if publication_required:
                publisher_status = "pass" if publisher_ready else "fail"
                publisher_summary = (
                    "Publisher has separate remote publication authority"
                    if publisher_ready
                    else "Required publisher authority is unavailable or unverified"
                )
            else:
                publisher_status = "warning"
                publisher_summary = (
                    "Remote publication is not required for execution readiness"
                )
            checks.append(
                _check(
                    f"repository.{coordinate}.publisher",
                    publisher_status,
                    publisher_summary,
                    {
                        "coordinate": coordinate,
                        "required": publication_required,
                        "separate_authority": separate_authority,
                        "verified": publisher_ready,
                    },
                    remediation=(
                        None
                        if publisher_ready
                        else (
                            "Configure and verify a distinct publisher auth reference before publication."
                            if publication_required
                            else "No action is needed until an explicit publication workflow is authorized."
                        )
                    ),
                )
            )
        return account

    @staticmethod
    def _repository_identity(remote: str) -> tuple[str | None, str]:
        value = remote.strip()
        if "://" not in value and ":" in value:
            authority, value = value.split(":", 1)
            host = authority.rsplit("@", 1)[-1].casefold()
        else:
            parsed = urlparse(value)
            host = parsed.hostname.casefold() if parsed.hostname else None
            value = parsed.path.lstrip("/") if parsed.scheme else value
        return host, value.removesuffix(".git").strip("/")

    def _doctor_bindings(
        self,
        checks: list[dict[str, Any]],
        desired: Mapping[str, Any] | None,
        registry: Mapping[str, list[dict[str, Any]]] | None,
    ) -> None:
        if desired is None or registry is None:
            checks.extend(
                (
                    _check(
                        "bindings.projects",
                        "fail",
                        "Project bindings cannot be cross-validated",
                        {"cross_validated": False},
                    ),
                    _check(
                        "bindings.repositories",
                        "fail",
                        "Repository coordinates cannot be cross-validated",
                        {"cross_validated": False},
                    ),
                )
            )
            return
        configured_projects = {
            (str(item["id"]), str(item["url"]))
            for item in desired["github"]["projects"]
        }
        registry_projects = {
            (str(item["project_id"]), str(item["project_url"]))
            for item in registry["projects"]
        }
        project_match = configured_projects == registry_projects
        checks.append(
            _check(
                "bindings.projects",
                "pass" if project_match else "fail",
                (
                    "Configured Projects exactly match the plugin registry"
                    if project_match
                    else "Configured Projects and registry bindings differ"
                ),
                {
                    "configured_count": len(configured_projects),
                    "registry_count": len(registry_projects),
                    "cross_validated": project_match,
                },
                remediation=(
                    None
                    if project_match
                    else "Explicitly configure or remove the mismatched Project binding; doctor will not bind it."
                ),
            )
        )
        configured_repositories = {
            str(item["coordinate"]).casefold()
            for item in desired["github"]["repositories"]
        }
        registry_repositories = {
            str(item["repository"]).casefold() for item in registry["maps"]
        }
        repository_match = registry_repositories <= configured_repositories
        checks.append(
            _check(
                "bindings.repositories",
                "pass" if repository_match else "fail",
                (
                    "Every bound Map repository is selected in plugin config"
                    if repository_match
                    else "A bound Map repository is absent from plugin config"
                ),
                {
                    "configured_count": len(configured_repositories),
                    "bound_count": len(registry_repositories),
                    "cross_validated": repository_match,
                },
                remediation=(
                    None
                    if repository_match
                    else "Select the existing bound repository in a new setup plan; doctor will not alter bindings."
                ),
            )
        )

    def _doctor_herdr(
        self,
        checks: list[dict[str, Any]],
        desired: Mapping[str, Any] | None,
    ) -> None:
        executable = str(desired["herdr"]["executable"]) if desired else "herdr"
        version_result = self._run([executable, "--version"])
        version_outcome = _command_outcome(version_result)
        version_match = (
            re.fullmatch(
                r"herdr ([0-9][A-Za-z0-9.+-]*)",
                version_result.stdout.strip(),
                flags=re.IGNORECASE,
            )
            if version_outcome == "success"
            else None
        )
        version_line = f"herdr {version_match.group(1)}" if version_match else None
        version_ready = version_match is not None
        checks.append(
            _check(
                "herdr.binary",
                "pass" if version_ready else "fail",
                (
                    "Herdr binary is available"
                    if version_ready
                    else "Herdr binary is missing or returned malformed version output"
                ),
                {"outcome": version_outcome, "version": version_line},
                remediation=(
                    None
                    if version_ready
                    else "Install Herdr or select its executable explicitly in setup."
                ),
            )
        )
        status_result = self._run([executable, "integration", "status"])
        status_outcome = _command_outcome(status_result)
        integrations: dict[str, str] = {}
        if status_outcome == "success":
            for line in status_result.stdout.splitlines():
                match = re.match(
                    r"^([a-z0-9_-]+):\s+(current|outdated|not installed)(?:\s+\([^()\r\n]*\)){0,2}$",
                    line.strip(),
                )
                if match:
                    integrations[match.group(1)] = match.group(2)
            if not integrations:
                status_outcome = "malformed"
        policy = str(desired["routing"]["policy"]) if desired else None
        required = (
            ROUTING_INTEGRATIONS[policy]
            if policy in ROUTING_INTEGRATIONS
            else frozenset(("hermes",))
        )
        for integration in ("hermes", "codex", "claude"):
            state = integrations.get(integration, "unknown")
            needed = integration in required
            if state == "current":
                check_status = "pass"
                summary = f"Herdr {integration} integration is current"
            elif state == "not installed" and not needed:
                check_status = "warning"
                summary = (
                    f"Herdr {integration} integration is not needed by routing policy"
                )
            elif state == "outdated":
                check_status = "fail" if needed else "warning"
                summary = f"Herdr {integration} integration is outdated"
            else:
                check_status = "fail" if needed else "warning"
                summary = (
                    f"Herdr {integration} integration is required but unavailable"
                    if needed
                    else f"Herdr {integration} integration is unavailable but optional"
                )
            checks.append(
                _check(
                    f"herdr.integration.{integration}",
                    check_status,
                    summary,
                    {
                        "integration": integration,
                        "state": state,
                        "required_by_routing": needed,
                        "probe_outcome": status_outcome,
                    },
                    remediation=(
                        None
                        if state == "current"
                        else "Run this explicit install action if the integration is required."
                    ),
                    command=(
                        None
                        if state == "current"
                        else [executable, "integration", "install", integration]
                    ),
                )
            )
