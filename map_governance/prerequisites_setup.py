"""Secret-safe prerequisite setup planning and atomic config commits."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlparse

import yaml


PLUGIN_ID = "map-governance"
SETUP_SCHEMA_VERSION = 1
SETUP_PLAN_VERSION = 1
SETUP_PLAN_TTL = timedelta(minutes=15)
REQUIRED_PLUGIN_SKILLS = (
    "map-governance:ceo",
    "map-governance:pm",
)
REQUIRED_EXTERNAL_SKILLS = ("delivery-pipeline", "implement")
ROUTING_INTEGRATIONS = {
    "codex": frozenset(("hermes", "codex")),
    "claude": frozenset(("hermes", "claude")),
    "mixed": frozenset(("hermes", "codex", "claude")),
}
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_COORDINATE_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_EXECUTABLE_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_SENSITIVE_KEYS = frozenset(
    {
        "token",
        "secret",
        "password",
        "api_key",
        "private_key",
        "credential_body",
        "access_key",
    }
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?:"
    r"github_pat_[A-Za-z0-9_]{6,}"
    r"|gh[pousr]_[A-Za-z0-9_]{6,}"
    r"|xox[baprs]-[A-Za-z0-9-]{6,}"
    r"|sk-[A-Za-z0-9_-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?:token|secret|password|api[ _-]?key|access[ _-]?key|authorization|bearer)"
    r"\s*(?::|=|\s)\s*[^\s,;]{4,}"
    r")",
    re.IGNORECASE,
)


def _sensitive_key(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower())
    collapsed = normalized.replace("_", "")
    return normalized in _SENSITIVE_KEYS or any(
        marker in collapsed
        for marker in (
            "token",
            "secret",
            "password",
            "apikey",
            "privatekey",
            "credentialbody",
            "accesskey",
        )
    )


def _sensitive_value(value: Any) -> bool:
    return isinstance(value, str) and _SENSITIVE_VALUE_RE.search(value) is not None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _content_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _mapping(value: Any, *, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return {str(key): copy.deepcopy(item) for key, item in value.items()}


def _non_empty(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _reject_secret_values(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _sensitive_key(key) or _sensitive_value(key):
                raise ValueError(
                    "Setup configuration must reference an auth provider; "
                    "secret material is forbidden"
                )
            _reject_secret_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secret_values(item)
    elif _sensitive_value(value):
        raise ValueError(
            "Setup configuration must reference an auth provider; "
            "secret material is forbidden"
        )


def _normalize_path(value: Any, *, name: str, file_name: str | None = None) -> str:
    raw = _non_empty(value, name=name)
    if raw.startswith("~") or "$" in raw:
        raise ValueError(f"{name} must not depend on shell expansion")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    if file_name is not None and path.name != file_name:
        raise ValueError(f"{name} must point to {file_name}")
    return str(path)


def _normalize_desired(value: Mapping[str, Any]) -> dict[str, Any]:
    desired = _mapping(value, name="setup desired configuration")
    _reject_secret_values(desired)
    if desired.get("schema_version") != SETUP_SCHEMA_VERSION:
        raise ValueError(f"setup schema_version must be {SETUP_SCHEMA_VERSION}")

    profiles = _mapping(desired.get("profiles"), name="profiles")
    normalized_profiles: dict[str, str] = {}
    for role in ("ceo", "pm"):
        profile = _non_empty(profiles.get(role), name=f"profiles.{role}").lower()
        if profile != "default" and not _PROFILE_RE.fullmatch(profile):
            raise ValueError(f"profiles.{role} is not a valid Hermes profile id")
        normalized_profiles[role] = profile
    if normalized_profiles["ceo"] == normalized_profiles["pm"]:
        raise ValueError("profiles.ceo and profiles.pm must be distinct")

    skills = _mapping(desired.get("skills"), name="skills")
    plugin_skills = skills.get("plugin")
    if (
        not isinstance(plugin_skills, list)
        or tuple(plugin_skills) != REQUIRED_PLUGIN_SKILLS
    ):
        raise ValueError(
            "skills.plugin must explicitly list map-governance:ceo and "
            "map-governance:pm"
        )
    external = skills.get("external")
    if not isinstance(external, list):
        raise ValueError("skills.external must be a list")
    normalized_external: list[dict[str, str]] = []
    seen_external: set[str] = set()
    for index, item in enumerate(external):
        skill = _mapping(item, name=f"skills.external[{index}]")
        skill_id = _non_empty(skill.get("id"), name=f"skills.external[{index}].id")
        if skill_id in seen_external:
            raise ValueError("skills.external contains a duplicate Skill id")
        seen_external.add(skill_id)
        normalized_external.append(
            {
                "id": skill_id,
                "path": _normalize_path(
                    skill.get("path"),
                    name=f"skills.external[{index}].path",
                    file_name="SKILL.md",
                ),
            }
        )
    missing_external = set(REQUIRED_EXTERNAL_SKILLS) - seen_external
    if missing_external:
        raise ValueError(
            "skills.external is missing required owner Skills: "
            + ", ".join(sorted(missing_external))
        )

    routing = _mapping(desired.get("routing"), name="routing")
    policy = _non_empty(routing.get("policy"), name="routing.policy").lower()
    if policy not in ROUTING_INTEGRATIONS:
        raise ValueError("routing.policy must be codex, claude, or mixed")

    github = _mapping(desired.get("github"), name="github")
    hostname = _non_empty(github.get("hostname"), name="github.hostname").lower()
    if not _HOST_RE.fullmatch(hostname) or ".." in hostname:
        raise ValueError("github.hostname must be a hostname")
    projects = github.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ValueError("github.projects must select at least one Project")
    normalized_projects: list[dict[str, Any]] = []
    project_ids: set[str] = set()
    for index, item in enumerate(projects):
        project = _mapping(item, name=f"github.projects[{index}]")
        project_id = _non_empty(project.get("id"), name=f"github.projects[{index}].id")
        if project_id in project_ids:
            raise ValueError("github.projects contains a duplicate Project id")
        project_ids.add(project_id)
        owner_type = _non_empty(
            project.get("owner_type"),
            name=f"github.projects[{index}].owner_type",
        ).lower()
        if owner_type not in {"organization", "user"}:
            raise ValueError("GitHub Project owner_type must be organization or user")
        number = project.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError("GitHub Project number must be a positive integer")
        capability = _non_empty(
            project.get("required_capability"),
            name=f"github.projects[{index}].required_capability",
        ).lower()
        if capability not in {"read", "write"}:
            raise ValueError("GitHub Project capability must be read or write")
        url = _non_empty(project.get("url"), name=f"github.projects[{index}].url")
        parsed = urlparse(url)
        expected_prefix = "orgs" if owner_type == "organization" else "users"
        owner = _non_empty(project.get("owner"), name=f"github.projects[{index}].owner")
        if (
            parsed.scheme != "https"
            or parsed.netloc.lower() != hostname
            or parsed.path.rstrip("/")
            != f"/{expected_prefix}/{owner}/projects/{number}"
        ):
            raise ValueError("GitHub Project coordinates do not match its URL")
        normalized_projects.append(
            {
                "id": project_id,
                "url": url.rstrip("/"),
                "owner": owner,
                "owner_type": owner_type,
                "number": number,
                "required_capability": capability,
            }
        )

    repositories = github.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise ValueError("github.repositories must select at least one repository")
    normalized_repositories: list[dict[str, Any]] = []
    coordinates: set[str] = set()
    for index, item in enumerate(repositories):
        repository = _mapping(item, name=f"github.repositories[{index}]")
        coordinate = _non_empty(
            repository.get("coordinate"),
            name=f"github.repositories[{index}].coordinate",
        )
        if not _COORDINATE_RE.fullmatch(coordinate):
            raise ValueError("Repository coordinate must be OWNER/REPOSITORY")
        if coordinate.casefold() in {item.casefold() for item in coordinates}:
            raise ValueError("github.repositories contains a duplicate coordinate")
        coordinates.add(coordinate)
        for flag in ("worker_write_required", "publication_required"):
            if not isinstance(repository.get(flag), bool):
                raise ValueError(f"github.repositories[{index}].{flag} must be boolean")
        normalized_repositories.append(
            {
                "coordinate": coordinate,
                "path": _normalize_path(
                    repository.get("path"),
                    name=f"github.repositories[{index}].path",
                ),
                "worker_write_required": repository["worker_write_required"],
                "publication_required": repository["publication_required"],
            }
        )

    authorities = _mapping(desired.get("authorities"), name="authorities")
    worker = _mapping(authorities.get("worker"), name="authorities.worker")
    publisher = _mapping(authorities.get("publisher"), name="authorities.publisher")
    if _non_empty(worker.get("kind"), name="authorities.worker.kind") != "local_git":
        raise ValueError("worker authority must use local_git")
    worker_ref = _non_empty(
        worker.get("credential_ref"), name="authorities.worker.credential_ref"
    )
    if worker_ref != "local-git":
        raise ValueError("worker credential_ref must be the local-git authority class")
    publisher_kind = _non_empty(
        publisher.get("kind"), name="authorities.publisher.kind"
    )
    if publisher_kind not in {"gh", "none"}:
        raise ValueError("publisher authority kind must be gh or none")
    publisher_ref = _non_empty(
        publisher.get("credential_ref"),
        name="authorities.publisher.credential_ref",
    )
    publisher_required = publisher.get("required")
    if not isinstance(publisher_required, bool):
        raise ValueError("authorities.publisher.required must be boolean")
    publisher_account = str(publisher.get("account") or "").strip()
    if publisher_kind == "none":
        if publisher_ref != "none" or publisher_account or publisher_required:
            raise ValueError(
                "A disabled publisher must use credential_ref none, no account, and required false"
            )
    else:
        if not _ACCOUNT_RE.fullmatch(publisher_account):
            raise ValueError("A gh publisher needs a valid GitHub account reference")
        expected_publisher_ref = f"gh:{hostname}:{publisher_account}"
        if publisher_ref != expected_publisher_ref:
            raise ValueError(
                "publisher credential_ref must be gh:HOST:ACCOUNT and match the selected host/account"
            )
    if worker_ref == publisher_ref:
        raise ValueError("worker and publisher credential references must be distinct")
    if (
        any(item["publication_required"] for item in normalized_repositories)
        and not publisher_required
    ):
        raise ValueError(
            "publication-required repositories need required publisher authority"
        )

    herdr = _mapping(desired.get("herdr"), name="herdr")
    executable = _non_empty(herdr.get("executable"), name="herdr.executable")
    executable_path = Path(executable)
    if (
        not executable_path.is_absolute() and not _EXECUTABLE_RE.fullmatch(executable)
    ) or any(character.isspace() for character in executable):
        raise ValueError(
            "herdr.executable must be one executable name or absolute path"
        )

    return {
        "schema_version": SETUP_SCHEMA_VERSION,
        "profiles": normalized_profiles,
        "skills": {
            "plugin": list(REQUIRED_PLUGIN_SKILLS),
            "external": normalized_external,
        },
        "routing": {"policy": policy},
        "github": {
            "hostname": hostname,
            "projects": normalized_projects,
            "repositories": normalized_repositories,
        },
        "authorities": {
            "worker": {"kind": "local_git", "credential_ref": worker_ref},
            "publisher": {
                "kind": publisher_kind,
                "credential_ref": publisher_ref,
                "account": publisher_account,
                "required": publisher_required,
            },
        },
        "herdr": {"executable": executable},
    }


def _redact(value: Any) -> Any:
    """Redact secret-shaped data before it reaches an operator preview."""
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            safe_key = "[REDACTED]" if _sensitive_value(key) else str(key)
            redacted[safe_key] = "[REDACTED]" if _sensitive_key(key) else _redact(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if _sensitive_value(value):
        return "[REDACTED]"
    if value is None or isinstance(value, (bool, int, float, str)):
        return copy.deepcopy(value)
    return "[UNSUPPORTED VALUE]"


class SetupApplyError(RuntimeError):
    """A setup plan was rejected without applying its selected changes."""

    def __init__(self, *, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail)

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "setup_apply_error",
            "reason": self.reason,
            "detail": self.detail,
            "retryable": self.reason in {"stale_plan", "expired_plan"},
        }


class ConfigCommitError(RuntimeError):
    """The selected config actions did not complete with verified readback."""


class ConfigRepository(Protocol):
    def read(self) -> dict[str, Any]: ...

    def revision(self) -> str: ...

    def commit(
        self,
        *,
        expected_revision: str,
        actions: Sequence[Mapping[str, Any]],
    ) -> str: ...


class YamlConfigRepository:
    """Compare-and-replace repository for one plugin-owned config file."""

    def __init__(self, path: Path, *, storage_root: Path | None = None) -> None:
        self.path = Path(os.path.abspath(os.fspath(path)))
        configured_root = storage_root if storage_root is not None else self.path.parent
        self.storage_root = Path(os.path.abspath(os.fspath(configured_root)))

    def _assert_safe_parent(self) -> None:
        if self.path.parent != self.storage_root:
            raise ConfigCommitError("Plugin config path escapes plugin storage")
        try:
            metadata = self.storage_root.lstat()
        except FileNotFoundError as error:
            raise ConfigCommitError(
                "Plugin storage directory does not exist"
            ) from error
        except OSError as error:
            raise ConfigCommitError("Plugin storage boundary is unavailable") from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
        ):
            raise ConfigCommitError("Plugin storage boundary is unsafe")

    @staticmethod
    def _assert_safe_regular_file(metadata: os.stat_result, *, subject: str) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
            or metadata.st_nlink != 1
        ):
            raise ConfigCommitError(f"{subject} is unsafe")

    def _bytes(self) -> bytes | None:
        self._assert_safe_parent()
        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ConfigCommitError(
                "Plugin config file is unsafe or unreadable"
            ) from error
        try:
            metadata = os.fstat(descriptor)
            self._assert_safe_regular_file(metadata, subject="Plugin config file")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = None
                return handle.read()
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _decode(content: bytes | None) -> dict[str, Any]:
        if content is None or not content.strip():
            return {}
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as error:
            raise ConfigCommitError("Plugin config YAML is invalid") from error
        if parsed is None:
            return {}
        if not isinstance(parsed, dict):
            raise ConfigCommitError("Plugin config root must be an object")
        return parsed

    def read(self) -> dict[str, Any]:
        return copy.deepcopy(self._decode(self._bytes()))

    def revision(self) -> str:
        content = self._bytes()
        return self._revision_for(content)

    @staticmethod
    def _revision_for(content: bytes | None) -> str:
        material = b"missing\0" if content is None else b"present\0" + content
        return "sha256:" + hashlib.sha256(material).hexdigest()

    def commit(
        self,
        *,
        expected_revision: str,
        actions: Sequence[Mapping[str, Any]],
    ) -> str:
        self._assert_safe_parent()
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            lock_descriptor = os.open(lock_path, flags, 0o600)
        except OSError as error:
            raise ConfigCommitError(
                "Plugin config commit lock is unavailable"
            ) from error
        locked = False
        try:
            try:
                lock_metadata = os.fstat(lock_descriptor)
                self._assert_safe_regular_file(
                    lock_metadata, subject="Plugin config commit lock"
                )
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            except OSError as error:
                raise ConfigCommitError(
                    "Plugin config commit lock is unavailable"
                ) from error
            locked = True
            return self._commit_locked(
                expected_revision=expected_revision,
                actions=actions,
            )
        finally:
            if locked:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)

    def _commit_locked(
        self,
        *,
        expected_revision: str,
        actions: Sequence[Mapping[str, Any]],
    ) -> str:
        original = self._bytes()
        if self._revision_for(original) != expected_revision:
            raise ConfigCommitError("Plugin config changed after setup planning")
        config = self._decode(original)
        for action in actions:
            action_id = action.get("action_id")
            after = copy.deepcopy(action.get("after"))
            if action_id == "config.prerequisites":
                config["prerequisites"] = after
            else:  # guarded by application too; keep repository fail-closed
                raise ConfigCommitError(f"Unknown config action: {action_id}")

        serialized = yaml.safe_dump(
            config,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        ).encode("utf-8")
        written_revision = self._revision_for(serialized)
        try:
            self._atomic_write(
                serialized,
                original=original,
                expected_revision=expected_revision,
            )
            readback = self.read()
            for action in actions:
                if action["action_id"] == "config.prerequisites":
                    observed = readback.get("prerequisites")
                if observed != action["after"]:
                    raise ConfigCommitError(
                        f"Atomic config readback did not confirm {action['action_id']}"
                    )
        except Exception as error:
            observed_revision = self.revision()
            if observed_revision == written_revision:
                try:
                    self._restore(original)
                except Exception as rollback_error:
                    raise ConfigCommitError(
                        "Config commit failed and original content could not be restored"
                    ) from rollback_error
            elif observed_revision != expected_revision:
                raise ConfigCommitError(
                    "Config commit failed after a concurrent config change; the newer content was not overwritten"
                ) from error
            if isinstance(error, ConfigCommitError):
                raise
            raise ConfigCommitError("Atomic config commit failed") from error
        return self.revision()

    def _atomic_write(
        self,
        content: bytes,
        *,
        original: bytes | None,
        expected_revision: str | None = None,
    ) -> None:
        self._assert_safe_parent()
        original_mode = None
        if original is not None:
            try:
                metadata = self.path.lstat()
            except OSError as error:
                raise ConfigCommitError(
                    "Plugin config metadata is not readable"
                ) from error
            self._assert_safe_regular_file(metadata, subject="Plugin config file")
            original_mode = stat.S_IMODE(metadata.st_mode)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, original_mode or 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if expected_revision is not None and self.revision() != expected_revision:
                raise ConfigCommitError(
                    "Plugin config changed while the setup commit was prepared"
                )
            os.replace(temporary, self.path)
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            directory_descriptor = os.open(self.path.parent, directory_flags)
            try:
                directory_metadata = os.fstat(directory_descriptor)
                if not stat.S_ISDIR(directory_metadata.st_mode) or (
                    hasattr(os, "geteuid") and directory_metadata.st_uid != os.geteuid()
                ):
                    raise ConfigCommitError("Plugin storage boundary is unsafe")
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _restore(self, original: bytes | None) -> None:
        if original is None:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return
        self._atomic_write(original, original=self._bytes())


def _prerequisite_settings(config: Mapping[str, Any]) -> dict[str, Any] | None:
    if "prerequisites" not in config:
        return None
    return {"prerequisites": copy.deepcopy(config.get("prerequisites"))}


class SetupWorkflow:
    """Plan and apply the explicit prerequisite configuration action."""

    def __init__(
        self,
        *,
        config_repository: ConfigRepository,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config_repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def setup_plan(self, *, desired: Mapping[str, Any]) -> dict[str, Any]:
        """Preview stable config actions without changing any state."""
        normalized = _normalize_desired(desired)
        try:
            before_revision = self._config.revision()
            current = self._config.read()
            config_revision = self._config.revision()
        except (ConfigCommitError, OSError) as error:
            raise ValueError("Plugin config is unavailable or invalid") from error
        if before_revision != config_revision:
            raise ValueError("Plugin config changed while setup planning")
        current_settings = _prerequisite_settings(current) or {}
        before_prerequisites = current_settings.get("prerequisites")
        created_at = self._clock().astimezone(timezone.utc)
        plan: dict[str, Any] = {
            "plan_version": SETUP_PLAN_VERSION,
            "status": "planned",
            "config_revision": config_revision,
            "created_at": _timestamp(created_at),
            "expires_at": _timestamp(created_at + SETUP_PLAN_TTL),
            "actions": [
                {
                    "action_id": "config.prerequisites",
                    "description": "Store Map Governance behavioral prerequisites",
                    "authority": "operator:config.write",
                    "before": _redact(before_prerequisites),
                    "after": _redact(normalized),
                },
            ],
        }
        plan["plan_id"] = "setup-plan:" + _content_hash(plan)
        return plan

    def setup_apply(
        self,
        *,
        plan: Mapping[str, Any],
        selected_action_ids: Sequence[str],
    ) -> dict[str, Any]:
        """Apply exactly the selected plan actions as one verified commit."""
        supplied = copy.deepcopy(dict(plan)) if isinstance(plan, Mapping) else {}
        supplied_plan_id = supplied.pop("plan_id", None)
        if not isinstance(supplied_plan_id, str) or supplied_plan_id != (
            "setup-plan:" + _content_hash(supplied)
        ):
            raise SetupApplyError(
                reason="payload_drift",
                detail="Setup plan content does not match its stable identity",
            )
        if supplied.get("plan_version") != SETUP_PLAN_VERSION:
            raise SetupApplyError(
                reason="unknown_plan",
                detail="Setup plan version is not supported",
            )
        if supplied.get("status") != "planned":
            raise SetupApplyError(
                reason="payload_drift",
                detail="Setup plan status changed after planning",
            )
        try:
            created_at = _parse_timestamp(supplied.get("created_at"))
            expires_at = _parse_timestamp(supplied.get("expires_at"))
            now = self._clock().astimezone(timezone.utc)
            expired = expires_at <= now
        except (TypeError, ValueError):
            raise SetupApplyError(
                reason="payload_drift", detail="Setup plan expiry is invalid"
            ) from None
        if created_at > now or expires_at - created_at != SETUP_PLAN_TTL:
            raise SetupApplyError(
                reason="payload_drift",
                detail="Setup plan lifetime changed after planning",
            )
        if expired:
            raise SetupApplyError(
                reason="expired_plan",
                detail="Setup plan expired; generate a new preview",
            )
        if not isinstance(selected_action_ids, Sequence) or isinstance(
            selected_action_ids, (str, bytes)
        ):
            raise SetupApplyError(
                reason="unknown_action", detail="Selected actions must be a list"
            )
        selected = [str(item) for item in selected_action_ids]
        if not selected or len(selected) != len(set(selected)):
            raise SetupApplyError(
                reason="unknown_action",
                detail="Select one or more unique setup action IDs",
            )
        actions = supplied.get("actions")
        if not isinstance(actions, list):
            raise SetupApplyError(
                reason="payload_drift", detail="Setup plan actions are invalid"
            )
        expected_action_ids = ["config.prerequisites"]
        if [
            action.get("action_id") if isinstance(action, Mapping) else None
            for action in actions
        ] != expected_action_ids:
            raise SetupApplyError(
                reason="unknown_plan",
                detail="Setup plan does not contain the known action contract",
            )
        expected_descriptions = ["Store Map Governance behavioral prerequisites"]
        if [action.get("description") for action in actions] != expected_descriptions:
            raise SetupApplyError(
                reason="payload_drift",
                detail="Setup plan action description changed after planning",
            )
        if any(
            action.get("authority") != "operator:config.write"
            for action in actions
            if isinstance(action, Mapping)
        ):
            raise SetupApplyError(
                reason="payload_drift",
                detail="Setup plan action authority changed after planning",
            )
        try:
            normalized_after = _normalize_desired(actions[0].get("after"))
        except (TypeError, ValueError) as error:
            raise SetupApplyError(
                reason="payload_drift",
                detail=f"Setup prerequisite payload is invalid: {error}",
            ) from error
        if normalized_after != actions[0].get("after"):
            raise SetupApplyError(
                reason="payload_drift",
                detail="Setup prerequisite payload is not normalized",
            )
        try:
            current = self._config.read()
        except (ConfigCommitError, OSError) as error:
            raise SetupApplyError(
                reason="commit_failed",
                detail="Plugin config is unavailable or invalid",
            ) from error
        current_settings = _prerequisite_settings(current) or {}
        if actions[0].get("before") != _redact(current_settings.get("prerequisites")):
            raise SetupApplyError(
                reason="stale_plan",
                detail="Setup plan before-state no longer matches plugin config",
            )
        by_id = {
            action.get("action_id"): action
            for action in actions
            if isinstance(action, Mapping)
        }
        unknown = [action_id for action_id in selected if action_id not in by_id]
        if unknown:
            raise SetupApplyError(
                reason="unknown_action",
                detail="The setup selection contains an unknown action ID",
            )
        expected_revision = supplied.get("config_revision")
        try:
            current_revision = self._config.revision()
        except (ConfigCommitError, OSError) as error:
            raise SetupApplyError(
                reason="commit_failed",
                detail="Plugin config revision cannot be read",
            ) from error
        if expected_revision != current_revision:
            raise SetupApplyError(
                reason="stale_plan",
                detail="Plugin config changed after setup planning",
            )
        selected_actions = [by_id[action_id] for action_id in selected]
        try:
            revision = self._config.commit(
                expected_revision=str(expected_revision),
                actions=selected_actions,
            )
        except ConfigCommitError as error:
            raise SetupApplyError(
                reason="commit_failed",
                detail=str(error),
            ) from error
        not_selected = [
            str(action["action_id"])
            for action in actions
            if action.get("action_id") not in set(selected)
        ]
        return {
            "status": "applied",
            "plan_id": supplied_plan_id,
            "applied_action_ids": selected,
            "not_selected_action_ids": not_selected,
            "config_revision": revision,
            "readback": "confirmed",
        }
