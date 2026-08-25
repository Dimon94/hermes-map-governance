"""Content-bound acceptance and remote-publication contracts."""

from __future__ import annotations

import hashlib
import grp
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .approvals import normalized_hash, normalized_json


PUBLICATION_ACTIONS = frozenset({"push", "pull_request", "merge", "release"})
PUBLICATION_STATUSES = frozenset({"succeeded", "repair_required", "aborted"})
PUBLICATION_TARGET_FIELDS = {
    "push": frozenset({"repository", "ref"}),
    "pull_request": frozenset({"repository", "head", "base", "title", "body"}),
    "merge": frozenset({"repository", "pull_request", "method"}),
    "release": frozenset({"repository", "tag", "title", "notes"}),
}
_PUBLISHER_ENVIRONMENT_KEYS = (
    "LANG",
    "LC_ALL",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
)
_PUBLISHER_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def publisher_subprocess_environment(
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the credential-free base environment for publisher subprocesses."""
    environment = {
        key: os.environ[key] for key in _PUBLISHER_ENVIRONMENT_KEYS if key in os.environ
    }
    environment["PATH"] = _PUBLISHER_PATH
    environment.update(dict(overrides or {}))
    return environment


def _text(value: Any, *, name: str, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    normalized = value.strip()
    if maximum is not None and len(normalized) > maximum:
        raise ValueError(f"{name} must not exceed {maximum} characters")
    return normalized


def _strings(value: Any, *, name: str, required: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list of strings")
    normalized = tuple(_text(item, name=f"{name} item", maximum=500) for item in value)
    if required and not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _object(value: Any, *, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{name} must be a non-empty object")
    try:
        normalized = json.loads(normalized_json(dict(value)))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be JSON serializable") from error
    if _contains_sensitive_key(normalized):
        raise ValueError(f"{name} must not contain publisher credentials")
    return normalized


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            any(
                sensitive in str(key).casefold()
                for sensitive in ("credential", "secret", "token", "password")
            )
            or _contains_sensitive_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _timestamp(value: Any, *, name: str) -> str:
    normalized = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be RFC 3339") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return normalized


def _positive_integer(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{name} must be a positive integer")
    try:
        normalized = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if normalized < 1 or str(normalized) != str(value):
        raise ValueError(f"{name} must be a positive integer")
    return normalized


@dataclass(frozen=True)
class PublicationAction:
    """The one remote action and exact target requested at acceptance."""

    action: str
    target: dict[str, Any]

    def __post_init__(self) -> None:
        action = _text(self.action, name="publication action", maximum=64)
        if action not in PUBLICATION_ACTIONS:
            raise ValueError("publication action is not supported")
        object.__setattr__(self, "action", action)
        target = _object(self.target, name="publication target")
        if set(target) != PUBLICATION_TARGET_FIELDS[action]:
            required = ", ".join(sorted(PUBLICATION_TARGET_FIELDS[action]))
            raise ValueError(
                f"{action} publication target must contain exactly: {required}"
            )
        object.__setattr__(self, "target", target)

    def payload(self) -> dict[str, Any]:
        return {"action": self.action, "target": self.target}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PublicationAction":
        return cls(
            action=_text(payload.get("action"), name="publication action"),
            target=_object(payload.get("target"), name="publication target"),
        )


@dataclass(frozen=True)
class AcceptanceEvidence:
    """Chairman-facing outcome evidence for one exact local revision."""

    revision: str
    delivered_scope: tuple[str, ...]
    validations: tuple[str, ...]
    known_limitations: tuple[str, ...]
    rollback_considerations: tuple[str, ...]
    requested_publication_action: PublicationAction

    def __post_init__(self) -> None:
        revision = _text(self.revision, name="acceptance revision", maximum=40)
        if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise ValueError("acceptance revision must be a full lowercase Git commit")
        object.__setattr__(self, "revision", revision)
        object.__setattr__(
            self,
            "delivered_scope",
            _strings(self.delivered_scope, name="delivered scope"),
        )
        object.__setattr__(
            self, "validations", _strings(self.validations, name="validations")
        )
        object.__setattr__(
            self,
            "known_limitations",
            _strings(self.known_limitations, name="known limitations", required=False),
        )
        object.__setattr__(
            self,
            "rollback_considerations",
            _strings(self.rollback_considerations, name="rollback considerations"),
        )
        if not isinstance(self.requested_publication_action, PublicationAction):
            raise TypeError("requested_publication_action must be a PublicationAction")

    @property
    def content_hash(self) -> str:
        return normalized_hash(self.payload())

    def payload(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "delivered_scope": list(self.delivered_scope),
            "validations": list(self.validations),
            "known_limitations": list(self.known_limitations),
            "rollback_considerations": list(self.rollback_considerations),
            "requested_publication_action": self.requested_publication_action.payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AcceptanceEvidence":
        action = payload.get("requested_publication_action")
        if not isinstance(action, Mapping):
            raise ValueError("acceptance requested publication action is required")
        return cls(
            revision=_text(payload.get("revision"), name="acceptance revision"),
            delivered_scope=_strings(
                payload.get("delivered_scope"), name="delivered scope"
            ),
            validations=_strings(payload.get("validations"), name="validations"),
            known_limitations=_strings(
                payload.get("known_limitations"),
                name="known limitations",
                required=False,
            ),
            rollback_considerations=_strings(
                payload.get("rollback_considerations"),
                name="rollback considerations",
            ),
            requested_publication_action=PublicationAction.from_payload(action),
        )


@dataclass(frozen=True)
class ApprovedPublicationAction:
    """Minimal exact action released to the privileged publisher boundary."""

    action_id: str
    map_id: str
    revision: str
    action: str
    target: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "action_id",
            _text(self.action_id, name="publication action id", maximum=128),
        )
        object.__setattr__(
            self,
            "map_id",
            _text(self.map_id, name="publication Map id", maximum=256),
        )
        publication = PublicationAction(action=self.action, target=self.target)
        object.__setattr__(self, "action", publication.action)
        object.__setattr__(self, "target", publication.target)
        revision = _text(self.revision, name="publication revision", maximum=40)
        if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise ValueError("publication revision must be a full lowercase Git commit")
        object.__setattr__(self, "revision", revision)

    def payload(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "map_id": self.map_id,
            "revision": self.revision,
            "action": self.action,
            "target": self.target,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ApprovedPublicationAction":
        return cls(
            action_id=_text(payload.get("action_id"), name="publication action id"),
            map_id=_text(payload.get("map_id"), name="publication Map id"),
            revision=_text(payload.get("revision"), name="publication revision"),
            action=_text(payload.get("action"), name="publication action"),
            target=_object(payload.get("target"), name="publication target"),
        )


@dataclass(frozen=True)
class RemotePublicationEvidence:
    """Immutable provider readback for one exact publication action."""

    action_id: str
    revision: str
    action: str
    target: dict[str, Any]
    provider: str
    remote_id: str
    remote_url: str
    published_at: str

    def __post_init__(self) -> None:
        action = ApprovedPublicationAction(
            action_id=self.action_id,
            map_id="evidence",
            revision=self.revision,
            action=self.action,
            target=self.target,
        )
        object.__setattr__(self, "action_id", action.action_id)
        object.__setattr__(self, "revision", action.revision)
        object.__setattr__(self, "action", action.action)
        object.__setattr__(self, "target", action.target)
        object.__setattr__(
            self,
            "provider",
            _text(self.provider, name="publication provider", maximum=128),
        )
        object.__setattr__(
            self,
            "remote_id",
            _text(self.remote_id, name="remote publication id", maximum=256),
        )
        object.__setattr__(
            self,
            "remote_url",
            _text(self.remote_url, name="remote publication URL", maximum=2048),
        )
        if not self.remote_url.startswith("https://"):
            raise ValueError("remote publication URL must use HTTPS")
        object.__setattr__(
            self,
            "published_at",
            _timestamp(self.published_at, name="publication timestamp"),
        )

    def payload(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "revision": self.revision,
            "action": self.action,
            "target": self.target,
            "provider": self.provider,
            "remote_id": self.remote_id,
            "remote_url": self.remote_url,
            "published_at": self.published_at,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RemotePublicationEvidence":
        return cls(**dict(payload))


@dataclass(frozen=True)
class PublicationRecord:
    """Tracker-owned success evidence or repair-required governance incident."""

    record_id: str
    action_id: str
    map_id: str
    approval_request_id: str
    status: str
    revision: str
    action: str
    target: dict[str, Any]
    occurred_at: str
    evidence: RemotePublicationEvidence | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        action = ApprovedPublicationAction(
            action_id=self.action_id,
            map_id=self.map_id,
            revision=self.revision,
            action=self.action,
            target=self.target,
        )
        object.__setattr__(
            self,
            "record_id",
            _text(self.record_id, name="publication record id", maximum=256),
        )
        object.__setattr__(self, "action_id", action.action_id)
        object.__setattr__(self, "map_id", action.map_id)
        object.__setattr__(self, "revision", action.revision)
        object.__setattr__(self, "action", action.action)
        object.__setattr__(self, "target", action.target)
        object.__setattr__(
            self,
            "approval_request_id",
            _text(
                self.approval_request_id,
                name="publication approval request id",
                maximum=128,
            ),
        )
        if self.status not in PUBLICATION_STATUSES:
            raise ValueError("publication record status is not supported")
        object.__setattr__(
            self,
            "occurred_at",
            _timestamp(self.occurred_at, name="publication record timestamp"),
        )
        if self.status == "succeeded":
            if not isinstance(self.evidence, RemotePublicationEvidence):
                raise ValueError("successful publication requires remote evidence")
            if (
                self.evidence.action_id != self.action_id
                or self.evidence.revision != self.revision
                or self.evidence.action != self.action
                or self.evidence.target != self.target
            ):
                raise ValueError(
                    "remote evidence belongs to another revision or publication action"
                )
            if self.reason is not None:
                raise ValueError(
                    "successful publication must not include a failure reason"
                )
        else:
            if self.evidence is not None:
                raise ValueError(
                    "non-successful publication must not claim success evidence"
                )
            object.__setattr__(
                self,
                "reason",
                _text(self.reason, name="publication incident reason", maximum=1000),
            )

    def payload(self) -> dict[str, Any]:
        payload = {
            "record_id": self.record_id,
            "action_id": self.action_id,
            "map_id": self.map_id,
            "approval_request_id": self.approval_request_id,
            "status": self.status,
            "revision": self.revision,
            "action": self.action,
            "target": self.target,
            "occurred_at": self.occurred_at,
        }
        if self.evidence is not None:
            payload["evidence"] = self.evidence.payload()
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PublicationRecord":
        values = dict(payload)
        evidence = values.get("evidence")
        if isinstance(evidence, Mapping):
            values["evidence"] = RemotePublicationEvidence.from_payload(evidence)
        return cls(**values)


class PublisherBoundary(Protocol):
    """Structural documentation for the privileged provider boundary."""

    authority_ref: str
    profile_name: str

    def validate_authority(self) -> None: ...

    def validate_action(self, action: ApprovedPublicationAction) -> None: ...

    def readback(
        self, action: ApprovedPublicationAction
    ) -> RemotePublicationEvidence | None:  # pragma: no cover - protocol shape
        ...

    def confirms_absence(self, action: ApprovedPublicationAction) -> bool: ...

    def execute(
        self, action: ApprovedPublicationAction
    ) -> None:  # pragma: no cover - protocol shape
        ...


class PublicationHandoff(Protocol):
    """Worker-side producer for passive publication artifacts."""

    def prepare(self, evidence: AcceptanceEvidence) -> None: ...


class GitBundlePublicationHandoff:
    """Atomically materialize one exact worker-owned Git bundle for publication."""

    def __init__(
        self,
        *,
        repositories: Mapping[str, Path],
        handoff_root: Path,
        worker_os_user: str,
        control_group: str,
        git_executable: Path,
        runner: PublisherCommandRunner | None = None,
    ) -> None:
        try:
            self._worker_uid = pwd.getpwnam(
                _text(worker_os_user, name="worker OS user", maximum=128)
            ).pw_uid
        except KeyError as error:
            raise ValueError("Worker publication OS user does not exist") from error
        if os.geteuid() != self._worker_uid:
            raise ValueError(
                "Worker publication handoff must run under its configured OS identity"
            )
        try:
            self._control_gid = grp.getgrnam(
                _text(control_group, name="publication control group", maximum=128)
            ).gr_gid
        except KeyError as error:
            raise ValueError("Publication control group does not exist") from error
        self._repositories = {
            _text(coordinate, name="worker repository", maximum=256): Path(path)
            for coordinate, path in repositories.items()
        }
        self._handoff_root = Path(os.path.abspath(os.fspath(handoff_root)))
        self._runner = runner or SubprocessPublisherCommandRunner()
        self._git_executable = GitHubPublisherBoundary._publisher_executable(
            git_executable,
            name="worker Git",
            validate=runner is None,
        )

    def prepare(self, evidence: AcceptanceEvidence) -> None:
        action = evidence.requested_publication_action
        if action.action != "push":
            return
        repository = self._repositories.get(str(action.target.get("repository")))
        if repository is None:
            raise ValueError("Acceptance repository is outside worker handoff boundary")
        if repository.resolve(strict=True) != repository:
            raise ValueError("Worker handoff repository must not be a symlink")
        self._prepare_directory(self._handoff_root)
        handoff = (
            self._handoff_root
            / hashlib.sha256(str(action.target["repository"]).encode()).hexdigest()
        )
        self._prepare_directory(handoff)
        metadata = handoff.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != self._worker_uid
            or metadata.st_gid != self._control_gid
            or stat.S_IMODE(metadata.st_mode) != 0o750
        ):
            raise ValueError("Worker publication handoff directory is unsafe")
        destination = handoff / f"{evidence.revision}.bundle"
        if destination.exists():
            self._verify_bundle(destination=destination, revision=evidence.revision)
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{evidence.revision}.", suffix=".tmp", dir=handoff
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        staging = Path(tempfile.mkdtemp(prefix=".git-stage.", dir=handoff))
        os.chmod(staging, 0o700)
        try:
            temporary.unlink()
            self._runner.run(
                (
                    self._git_executable,
                    "init",
                    "--bare",
                    str(staging),
                )
            )
            self._runner.run(
                (
                    self._git_executable,
                    "-c",
                    "protocol.file.allow=always",
                    "--git-dir",
                    str(staging),
                    "fetch",
                    "--no-tags",
                    "--force",
                    str(repository),
                    f"{evidence.revision}:refs/heads/approved",
                )
            )
            self._runner.run(
                (
                    self._git_executable,
                    "--git-dir",
                    str(staging),
                    "bundle",
                    "create",
                    str(temporary),
                    "refs/heads/approved",
                )
            )
            os.chown(temporary, -1, self._control_gid)
            os.chmod(temporary, 0o600)
            self._verify_bundle(destination=temporary, revision=evidence.revision)
            os.chown(temporary, -1, self._control_gid)
            os.chmod(temporary, 0o440)
            os.replace(temporary, destination)
            directory = os.open(handoff, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)

    def _prepare_directory(self, path: Path) -> None:
        path.mkdir(mode=0o750, exist_ok=True)
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError("Worker publication handoff directory is unsafe")
        if metadata.st_uid == self._worker_uid:
            os.chown(path, -1, self._control_gid)
            os.chmod(path, 0o750)
            metadata = path.lstat()
        if (
            metadata.st_uid != self._worker_uid
            or metadata.st_gid != self._control_gid
            or stat.S_IMODE(metadata.st_mode) != 0o750
        ):
            raise ValueError("Worker publication handoff directory is unsafe")

    def _verify_bundle(self, *, destination: Path, revision: str) -> None:
        metadata = destination.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != self._worker_uid
            or metadata.st_gid != self._control_gid
            or stat.S_IMODE(metadata.st_mode) not in {0o600, 0o440}
            or metadata.st_nlink != 1
        ):
            raise ValueError("Worker revision bundle is unsafe")
        heads = self._runner.run(
            (
                self._git_executable,
                "bundle",
                "list-heads",
                str(destination),
            )
        )
        revisions = {
            line.split(maxsplit=1)[0] for line in heads.splitlines() if line.strip()
        }
        if revision not in revisions:
            raise ValueError("Worker revision bundle does not contain exact revision")


class PublicationPartialFailure(RuntimeError):
    """A remote action changed state but cannot be proven fully successful."""

    retryable = False

    def __init__(self, reason: str) -> None:
        normalized = _text(reason, name="publication partial failure", maximum=1000)
        if re.search(
            r"(?i)(credential|secret|password|access[_ -]?token|github_pat_|ghp_)",
            normalized,
        ):
            normalized = (
                "Privileged publisher reported a partial remote failure; inspect "
                "the isolated publisher audit boundary."
            )
        self.reason = normalized
        super().__init__(self.reason)


class PublisherCommandRunner(Protocol):
    """Privileged command transport kept outside PM and worker composition."""

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> str: ...


class SubprocessPublisherCommandRunner:
    """Run explicit publisher commands without materializing credentials."""

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> str:
        process_environment = publisher_subprocess_environment(environment)
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=process_environment,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"Publisher command failed with exit status {completed.returncode}"
            )
        return completed.stdout


class GitHubPublisherBoundary:
    """Provider boundary that executes only one validated GitHub action."""

    def __init__(
        self,
        *,
        hostname: str,
        account: str,
        authority_ref: str,
        repositories: Mapping[str, Path],
        handoff_root: Path,
        worker_os_user: str,
        control_group: str,
        gh_config_dir: Path,
        git_executable: Path,
        gh_executable: Path,
        profile_name: str = "publisher",
        runner: PublisherCommandRunner | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.hostname = _text(hostname, name="publisher hostname", maximum=253)
        self.account = _text(account, name="publisher account", maximum=128)
        self.authority_ref = _text(
            authority_ref, name="publisher authority reference", maximum=256
        )
        self.profile_name = _text(profile_name, name="publisher profile", maximum=128)
        expected_ref = f"gh:{self.hostname}:{self.account}"
        if self.authority_ref != expected_ref:
            raise ValueError("Publisher authority does not match host and account")
        self._repositories = {
            _text(coordinate, name="publisher repository", maximum=256): Path(path)
            for coordinate, path in repositories.items()
        }
        try:
            self._worker_uid = pwd.getpwnam(
                _text(worker_os_user, name="worker OS user", maximum=128)
            ).pw_uid
        except KeyError as error:
            raise ValueError("Publisher worker OS user does not exist") from error
        if self._worker_uid == os.geteuid():
            raise ValueError(
                "Publisher and worker must use distinct real OS identities"
            )
        try:
            self._control_gid = grp.getgrnam(
                _text(control_group, name="publication control group", maximum=128)
            ).gr_gid
        except KeyError as error:
            raise ValueError("Publication control group does not exist") from error
        self._handoff_root = Path(os.path.abspath(os.fspath(handoff_root)))
        config_dir = Path(gh_config_dir)
        if not config_dir.is_absolute() or not config_dir.is_dir():
            raise ValueError("Publisher GH_CONFIG_DIR must be an existing directory")
        resolved_config_dir = config_dir.resolve()
        if resolved_config_dir != config_dir:
            raise ValueError("Publisher GH_CONFIG_DIR must not be a symlink")
        stat_result = resolved_config_dir.stat()
        if stat_result.st_uid != os.geteuid() or stat_result.st_mode & 0o077:
            raise ValueError(
                "Publisher GH_CONFIG_DIR must be owned by this OS user and mode 0700"
            )
        self._gh_environment = {
            "GH_CONFIG_DIR": str(resolved_config_dir),
            "GH_HOST": self.hostname,
            "HOME": str(resolved_config_dir),
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        self._production_runner = runner is None
        self._runner = runner or SubprocessPublisherCommandRunner()
        self._git_executable = self._publisher_executable(
            git_executable, name="Git", validate=runner is None
        )
        self._gh_executable = self._publisher_executable(
            gh_executable, name="GitHub CLI", validate=runner is None
        )
        self._staging_root = resolved_config_dir / "publication-staging"
        self._staging_root.mkdir(mode=0o700, exist_ok=True)
        staging_metadata = self._staging_root.lstat()
        if (
            stat.S_ISLNK(staging_metadata.st_mode)
            or not stat.S_ISDIR(staging_metadata.st_mode)
            or self._staging_root.resolve() != self._staging_root
            or self._staging_root.parent != resolved_config_dir
            or staging_metadata.st_uid != os.geteuid()
            or staging_metadata.st_mode & 0o077
        ):
            raise ValueError(
                "Publisher staging must be owned by this OS user and mode 0700"
            )
        self._prepared_pushes: dict[str, Path] = {}
        self._garbage_collect_staging()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def tracker_executable(self) -> str:
        """Return the same trusted GitHub CLI selected for publication."""
        return self._gh_executable

    @property
    def tracker_environment(self) -> dict[str, str]:
        """Return the same isolated, credential-reference-only CLI environment."""
        return publisher_subprocess_environment(self._gh_environment)

    @staticmethod
    def _publisher_executable(path: Path, *, name: str, validate: bool) -> str:
        candidate = Path(path)
        if not candidate.is_absolute():
            raise ValueError(f"Publisher {name} executable must be absolute")
        if any(character.isspace() for character in str(candidate)):
            raise ValueError(
                f"Publisher {name} executable path must not contain whitespace"
            )
        if not validate:
            return str(candidate)
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
        if not resolved.is_file() or metadata.st_uid not in {0, os.geteuid()}:
            raise ValueError(f"Publisher {name} executable has untrusted ownership")
        if metadata.st_mode & 0o022:
            raise ValueError(f"Publisher {name} executable is group/world writable")
        return str(resolved)

    def _gh(self, *arguments: str) -> str:
        if arguments and arguments[0] == "api":
            return self._runner.run(
                (
                    self._gh_executable,
                    "api",
                    *arguments[1:],
                    "--hostname",
                    self.hostname,
                ),
                environment=self._gh_environment,
            )
        return self._runner.run(
            (self._gh_executable, *arguments), environment=self._gh_environment
        )

    def _push_staging_path(self, action: ApprovedPublicationAction) -> Path:
        identity = hashlib.sha256(action.action_id.encode()).hexdigest()
        path = self._staging_root / identity
        path.mkdir(mode=0o700, exist_ok=True)
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or path.resolve() != path
            or path.parent != self._staging_root
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ValueError("Publisher action staging is unsafe")
        return path

    def _garbage_collect_staging(self) -> None:
        """Bound crash leftovers without touching recent concurrent actions."""
        cutoff = time.time() - 24 * 60 * 60
        for candidate in self._staging_root.iterdir():
            try:
                metadata = candidate.lstat()
            except OSError:
                continue
            if (
                not re.fullmatch(r"[0-9a-f]{64}", candidate.name)
                or not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mtime >= cutoff
            ):
                continue
            shutil.rmtree(candidate)

    def _cleanup_push_staging(self, action_id: str) -> None:
        staging = self._prepared_pushes.pop(action_id, None)
        if staging is None or staging.parent != self._staging_root:
            return
        try:
            metadata = staging.lstat()
        except OSError:
            return
        if (
            stat.S_ISDIR(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
        ):
            shutil.rmtree(staging)

    def _approved_bundle(
        self, *, repository: str, staging: Path, revision: str
    ) -> Path:
        """Copy a passive worker-owned bundle into publisher-owned staging."""
        handoff = self._handoff_root / hashlib.sha256(repository.encode()).hexdigest()
        source = handoff / f"{revision}.bundle"
        if not self._production_runner:
            return source
        try:
            root_metadata = self._handoff_root.lstat()
            resolved_root = self._handoff_root.resolve(strict=True)
            resolved_handoff = handoff.resolve(strict=True)
            handoff_metadata = handoff.lstat()
        except OSError as error:
            raise ValueError("Approved worker bundle handoff is unavailable") from error
        if (
            resolved_root != self._handoff_root
            or stat.S_ISLNK(root_metadata.st_mode)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != self._worker_uid
            or root_metadata.st_gid != self._control_gid
            or stat.S_IMODE(root_metadata.st_mode) != 0o750
            or resolved_handoff != handoff
            or stat.S_ISLNK(handoff_metadata.st_mode)
            or not stat.S_ISDIR(handoff_metadata.st_mode)
            or handoff_metadata.st_uid != self._worker_uid
            or handoff_metadata.st_gid != self._control_gid
            or stat.S_IMODE(handoff_metadata.st_mode) != 0o750
        ):
            raise ValueError("Approved worker bundle handoff is not worker-owned")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(source, flags)
        except OSError as error:
            raise ValueError(
                "Approved worker revision bundle is unavailable"
            ) from error
        destination = staging / "approved-revision.bundle"
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self._worker_uid
                or metadata.st_gid != self._control_gid
                or stat.S_IMODE(metadata.st_mode) != 0o440
                or metadata.st_nlink != 1
            ):
                raise ValueError(
                    "Approved worker revision bundle has unsafe ownership or mode"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as source_handle:
                with destination.open("wb") as destination_handle:
                    os.chmod(destination, 0o600)
                    shutil.copyfileobj(source_handle, destination_handle)
        finally:
            os.close(descriptor)
        return destination

    def _repository_argument(self, coordinate: str) -> str:
        return (
            coordinate
            if self.hostname == "github.com"
            else f"{self.hostname}/{coordinate}"
        )

    def _assert_account(self) -> None:
        active = self._gh("api", "user", "--jq", ".login").strip()
        if active != self.account:
            raise PublicationPartialFailure(
                "Active GitHub publisher account does not match configured authority"
            )
        for coordinate in self._repositories:
            payload = self._json(
                self._gh(
                    "repo",
                    "view",
                    self._repository_argument(coordinate),
                    "--json",
                    "nameWithOwner,viewerPermission,isArchived",
                ),
                subject="publisher repository capability",
            )
            if (
                not isinstance(payload, Mapping)
                or payload.get("nameWithOwner") != coordinate
                or payload.get("isArchived") is not False
                or payload.get("viewerPermission") not in {"WRITE", "MAINTAIN", "ADMIN"}
            ):
                raise PublicationPartialFailure(
                    "Publisher repository capability is unavailable"
                )

    def validate_authority(self) -> None:
        """Prove the active provider account before approval consumption."""
        self._assert_account()

    @staticmethod
    def _json(content: str, *, subject: str) -> Any:
        try:
            return json.loads(content)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"{subject} returned invalid JSON") from error

    def _repository(self, action: ApprovedPublicationAction) -> tuple[str, Path]:
        coordinate = _text(
            action.target.get("repository"), name="publication repository", maximum=256
        )
        path = self._repositories.get(coordinate)
        if path is None:
            raise PublicationPartialFailure(
                "Approved repository is outside the publisher boundary"
            )
        return coordinate, path

    @staticmethod
    def _coordinate_parts(coordinate: str) -> tuple[str, str]:
        owner, separator, name = coordinate.partition("/")
        if not separator or not owner or not name:
            raise ValueError("Publisher repository coordinate is invalid")
        return owner, name

    def _repository_ref(
        self, *, coordinate: str, qualified_name: str
    ) -> dict[str, Any] | None:
        owner, name = self._coordinate_parts(coordinate)
        query = (
            "query($owner:String!,$name:String!,$qualifiedName:String!){"
            "repository(owner:$owner,name:$name){"
            "ref(qualifiedName:$qualifiedName){target{"
            "__typename oid ... on Commit{committedDate} "
            "... on Tag{target{oid ... on Commit{committedDate}}}"
            "}}}}"
        )
        payload = self._json(
            self._gh(
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"qualifiedName={qualified_name}",
            ),
            subject="repository ref readback",
        )
        if payload.get("errors"):
            raise RuntimeError("repository ref readback returned provider errors")
        data = payload.get("data")
        repository = data.get("repository") if isinstance(data, Mapping) else None
        if not isinstance(repository, Mapping) or "ref" not in repository:
            raise RuntimeError("repository ref readback is incomplete")
        reference = repository.get("ref")
        if reference is not None and not isinstance(reference, Mapping):
            raise RuntimeError("repository ref readback returned invalid content")
        return dict(reference) if isinstance(reference, Mapping) else None

    @staticmethod
    def _ref_commit(reference: Mapping[str, Any]) -> tuple[str, str | None]:
        target = reference.get("target")
        if not isinstance(target, Mapping):
            return "", None
        if target.get("__typename") == "Tag":
            nested = target.get("target")
            target = nested if isinstance(nested, Mapping) else {}
        return str(target.get("oid") or ""), (
            str(target["committedDate"])
            if target.get("committedDate") is not None
            else None
        )

    def _pull_request(self, *, repository: str, number: str) -> dict[str, Any]:
        payload = self._json(
            self._gh(
                "pr",
                "view",
                number,
                "--repo",
                self._repository_argument(repository),
                "--json",
                "state,mergeCommit,url,mergedAt,headRefOid",
            ),
            subject="pull request readback",
        )
        if not isinstance(payload, Mapping):
            raise RuntimeError("pull request readback returned invalid content")
        return dict(payload)

    def validate_action(self, action: ApprovedPublicationAction) -> None:
        """Fail closed before a privileged mutation can target drifted state."""
        repository, _path = self._repository(action)
        target = action.target
        if action.action == "push":
            ref = _text(target.get("ref"), name="publication ref", maximum=512)
            if not ref.startswith("refs/heads/"):
                raise ValueError("push publication ref must be under refs/heads")
            return
        if action.action == "pull_request":
            head = _text(target.get("head"), name="pull request head", maximum=256)
            base_owner, repository_name = self._coordinate_parts(repository)
            if ":" in head:
                head_owner, head_branch = head.split(":", 1)
            else:
                head_owner, head_branch = base_owner, head
            reference = self._repository_ref(
                coordinate=f"{head_owner}/{repository_name}",
                qualified_name=f"refs/heads/{head_branch}",
            )
            revision, _committed_at = (
                self._ref_commit(reference) if reference is not None else ("", None)
            )
            if revision != action.revision:
                raise ValueError(
                    "pull request head no longer matches approved revision"
                )
            return
        if action.action == "merge":
            number = str(
                _positive_integer(
                    target.get("pull_request"), name="pull request number"
                )
            )
            method = _text(target.get("method"), name="merge method", maximum=16)
            if method != "merge":
                raise ValueError("Only a provable merge-commit method is supported")
            pull_request = self._pull_request(repository=repository, number=number)
            if (
                pull_request.get("state") != "OPEN"
                or pull_request.get("headRefOid") != action.revision
            ):
                raise ValueError(
                    "pull request no longer matches approved merge revision"
                )
            return
        revision = self._json(
            self._gh(
                "api",
                f"repos/{repository}/commits/{action.revision}",
            ),
            subject="release revision preflight",
        )
        if not isinstance(revision, Mapping) or revision.get("sha") != action.revision:
            raise ValueError("release revision is unavailable on approved repository")
        tag = _text(target.get("tag"), name="release tag", maximum=256)
        owner, name = self._coordinate_parts(repository)
        query = (
            "query($owner:String!,$name:String!,$tag:String!){"
            "repository(owner:$owner,name:$name){release(tagName:$tag){tagName}}}"
        )
        payload = self._json(
            self._gh(
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"tag={tag}",
            ),
            subject="release target preflight",
        )
        if payload.get("errors"):
            raise ValueError("release target preflight returned provider errors")
        data = payload.get("data")
        repository_payload = (
            data.get("repository") if isinstance(data, Mapping) else None
        )
        if (
            not isinstance(repository_payload, Mapping)
            or "release" not in repository_payload
        ):
            raise ValueError("release target preflight is incomplete")
        if repository_payload.get("release") is not None:
            raise ValueError("release target already exists")
        reference = self._repository_ref(
            coordinate=repository, qualified_name=f"refs/tags/{tag}"
        )
        if reference is not None:
            tag_revision, _committed_at = self._ref_commit(reference)
            if tag_revision != action.revision:
                raise ValueError("release tag no longer matches approved revision")

    def prepare_action(self, action: ApprovedPublicationAction) -> None:
        """Prepare passive local push state only inside the claimed action fence."""
        if action.action != "push":
            return
        repository, _path = self._repository(action)
        staging = self._push_staging_path(action)
        try:
            bundle = self._approved_bundle(
                repository=repository,
                staging=staging,
                revision=action.revision,
            )
            self._runner.run(
                (self._git_executable, "init", "--bare", str(staging)),
                environment=self._gh_environment,
            )
            self._runner.run(
                (
                    self._git_executable,
                    "-c",
                    "protocol.file.allow=always",
                    "--git-dir",
                    str(staging),
                    "fetch",
                    "--no-tags",
                    "--force",
                    str(bundle),
                    action.revision,
                ),
                environment=self._gh_environment,
            )
            self._runner.run(
                (
                    self._git_executable,
                    "--git-dir",
                    str(staging),
                    "cat-file",
                    "-e",
                    f"{action.revision}^{{commit}}",
                ),
                environment=self._gh_environment,
            )
            self._prepared_pushes[action.action_id] = staging
        except Exception:
            self._prepared_pushes[action.action_id] = staging
            self._cleanup_push_staging(action.action_id)
            raise

    def abort_action(self, action: ApprovedPublicationAction) -> None:
        """Discard local preparation when the final marker fence rejects an action."""
        if action.action == "push":
            self._cleanup_push_staging(action.action_id)

    def execute(self, action: ApprovedPublicationAction) -> None:
        """Mutate only after the adapter has fenced and prevalidated this action."""
        repository, _path = self._repository(action)
        repository_argument = self._repository_argument(repository)
        target = action.target
        try:
            if action.action == "push":
                ref = _text(target.get("ref"), name="publication ref", maximum=512)
                staging = self._prepared_pushes.get(action.action_id)
                if staging is None:
                    raise RuntimeError(
                        "Publisher push has no prevalidated staging repo"
                    )
                try:
                    self._runner.run(
                        (
                            self._git_executable,
                            "--git-dir",
                            str(staging),
                            "-c",
                            "core.hooksPath=/dev/null",
                            "-c",
                            "credential.helper=",
                            "-c",
                            (
                                "credential.helper=!"
                                f"{self._gh_executable} auth git-credential"
                            ),
                            "-c",
                            "http.sslVerify=true",
                            "-c",
                            "protocol.ext.allow=never",
                            "-c",
                            "protocol.file.allow=never",
                            "-c",
                            "protocol.https.allow=always",
                            "push",
                            "--no-verify",
                            f"https://{self.hostname}/{repository}.git",
                            f"{action.revision}:{ref}",
                        ),
                        environment=self._gh_environment,
                    )
                finally:
                    self._cleanup_push_staging(action.action_id)
                return
            if action.action == "pull_request":
                head = _text(target.get("head"), name="pull request head", maximum=256)
                base = _text(target.get("base"), name="pull request base", maximum=256)
                title = _text(
                    target.get("title"), name="pull request title", maximum=256
                )
                body = _text(target.get("body"), name="pull request body", maximum=5000)
                self._gh(
                    "pr",
                    "create",
                    "--repo",
                    repository_argument,
                    "--head",
                    head,
                    "--base",
                    base,
                    "--title",
                    title,
                    "--body",
                    body,
                )
                return
            if action.action == "merge":
                number = str(
                    _positive_integer(
                        target.get("pull_request"), name="pull request number"
                    )
                )
                self._gh(
                    "pr",
                    "merge",
                    number,
                    "--repo",
                    repository_argument,
                    "--merge",
                    "--match-head-commit",
                    action.revision,
                )
                return
            tag = _text(target.get("tag"), name="release tag", maximum=256)
            title = _text(target.get("title"), name="release title", maximum=256)
            notes = _text(target.get("notes"), name="release notes", maximum=5000)
            self._gh(
                "release",
                "create",
                tag,
                "--repo",
                repository_argument,
                "--target",
                action.revision,
                "--title",
                title,
                "--notes",
                notes,
            )
        except PublicationPartialFailure:
            raise
        except Exception as error:
            raise PublicationPartialFailure(
                f"Publisher command returned an unknown remote outcome: {error}"
            ) from error

    def readback(
        self, action: ApprovedPublicationAction
    ) -> RemotePublicationEvidence | None:
        self.validate_authority()
        repository, _path = self._repository(action)
        repository_argument = self._repository_argument(repository)
        target = action.target
        if action.action == "push":
            ref = _text(target.get("ref"), name="publication ref", maximum=512)
            reference = self._repository_ref(coordinate=repository, qualified_name=ref)
            if reference is None:
                return None
            remote_revision, _committed_at = self._ref_commit(reference)
            if remote_revision != action.revision:
                return None
            remote_id = ref
            remote_url = (
                f"https://{self.hostname}/{repository}/commit/{action.revision}"
            )
            published_at = self._now()
        elif action.action == "pull_request":
            head = _text(target.get("head"), name="pull request head", maximum=256)
            base = _text(target.get("base"), name="pull request base", maximum=256)
            title = _text(target.get("title"), name="pull request title", maximum=256)
            body = _text(target.get("body"), name="pull request body", maximum=5000)
            payload = self._json(
                self._gh(
                    "pr",
                    "list",
                    "--repo",
                    repository_argument,
                    "--head",
                    head,
                    "--state",
                    "all",
                    "--limit",
                    "1000",
                    "--json",
                    "number,url,headRefOid,baseRefName,createdAt,title,body",
                ),
                subject="pull request readback",
            )
            if not isinstance(payload, list) or any(
                not isinstance(item, Mapping) for item in payload
            ):
                raise RuntimeError("pull request readback returned invalid content")
            matches = [
                item
                for item in payload
                if item.get("headRefOid") == action.revision
                and item.get("baseRefName") == base
                and item.get("title") == title
                and item.get("body") == body
            ]
            if len(matches) != 1:
                return None
            match = matches[0]
            remote_id = f"pr:{match['number']}"
            remote_url = str(match["url"])
            published_at = str(match["createdAt"])
        elif action.action == "merge":
            number = str(
                _positive_integer(
                    target.get("pull_request"), name="pull request number"
                )
            )
            payload = self._pull_request(repository=repository, number=number)
            if (
                payload.get("state") != "MERGED"
                or payload.get("headRefOid") != action.revision
            ):
                return None
            remote_id = str(payload.get("mergeCommit", {}).get("oid") or "")
            commit = self._json(
                self._gh("api", f"repos/{repository}/commits/{remote_id}"),
                subject="merge commit readback",
            )
            parents = commit.get("parents") if isinstance(commit, Mapping) else None
            if not isinstance(parents, list) or len(parents) != 2:
                return None
            remote_url = str(payload["url"])
            published_at = str(payload["mergedAt"])
        else:
            tag = _text(target.get("tag"), name="release tag", maximum=256)
            title = _text(target.get("title"), name="release title", maximum=256)
            notes = _text(target.get("notes"), name="release notes", maximum=5000)
            owner, name = self._coordinate_parts(repository)
            query = (
                "query($owner:String!,$name:String!,$tag:String!){"
                "repository(owner:$owner,name:$name){"
                "release(tagName:$tag){tagName url publishedAt name description}"
                "}}"
            )
            payload = self._json(
                self._gh(
                    "api",
                    "graphql",
                    "-f",
                    f"query={query}",
                    "-F",
                    f"owner={owner}",
                    "-F",
                    f"name={name}",
                    "-F",
                    f"tag={tag}",
                ),
                subject="release readback",
            )
            if payload.get("errors"):
                raise RuntimeError("release readback returned provider errors")
            data = payload.get("data")
            repository_payload = (
                data.get("repository") if isinstance(data, Mapping) else None
            )
            if (
                not isinstance(repository_payload, Mapping)
                or "release" not in repository_payload
            ):
                raise RuntimeError("release readback is incomplete")
            release = repository_payload.get("release")
            if not isinstance(release, Mapping):
                return None
            reference = self._repository_ref(
                coordinate=repository, qualified_name=f"refs/tags/{tag}"
            )
            revision, _committed_at = (
                self._ref_commit(reference) if reference is not None else ("", None)
            )
            if (
                revision != action.revision
                or release.get("name") != title
                or release.get("description") != notes
            ):
                return None
            remote_id = f"release:{release['tagName']}"
            remote_url = str(release["url"])
            published_at = str(release["publishedAt"])
        evidence = RemotePublicationEvidence(
            action_id=action.action_id,
            revision=action.revision,
            action=action.action,
            target=action.target,
            provider=f"github:{self.hostname}",
            remote_id=remote_id,
            remote_url=remote_url,
            published_at=published_at,
        )
        if action.action == "push":
            self._cleanup_push_staging(action.action_id)
        return evidence

    def confirms_absence(self, action: ApprovedPublicationAction) -> bool:
        """Return true only when the provider proves no target artifact exists."""
        self.validate_authority()
        repository, _path = self._repository(action)
        target = action.target
        if action.action == "push":
            ref = _text(target.get("ref"), name="publication ref", maximum=512)
            return (
                self._repository_ref(coordinate=repository, qualified_name=ref) is None
            )
        if action.action == "pull_request":
            head = _text(target.get("head"), name="pull request head", maximum=256)
            base = _text(target.get("base"), name="pull request base", maximum=256)
            payload = self._json(
                self._gh(
                    "pr",
                    "list",
                    "--repo",
                    self._repository_argument(repository),
                    "--head",
                    head,
                    "--state",
                    "all",
                    "--limit",
                    "1000",
                    "--json",
                    "number,headRefOid,baseRefName,title,body",
                ),
                subject="pull request absence readback",
            )
            if not isinstance(payload, list) or any(
                not isinstance(item, Mapping) for item in payload
            ):
                raise RuntimeError(
                    "pull request absence readback returned invalid content"
                )
            required_fields = {
                "number": int,
                "headRefOid": str,
                "baseRefName": str,
                "title": str,
                "body": str,
            }
            if any(
                isinstance(item.get("number"), bool)
                or any(
                    field not in item or not isinstance(item[field], field_type)
                    for field, field_type in required_fields.items()
                )
                for item in payload
            ):
                raise RuntimeError(
                    "pull request absence readback returned incomplete content"
                )
            related = [item for item in payload if item.get("baseRefName") == base]
            return not related
        if action.action == "merge":
            number = str(
                _positive_integer(
                    target.get("pull_request"), name="pull request number"
                )
            )
            payload = self._pull_request(repository=repository, number=number)
            return (
                payload.get("state") == "OPEN"
                and payload.get("headRefOid") == action.revision
            )
        tag = _text(target.get("tag"), name="release tag", maximum=256)
        owner, name = self._coordinate_parts(repository)
        query = (
            "query($owner:String!,$name:String!,$tag:String!){"
            "repository(owner:$owner,name:$name){release(tagName:$tag){tagName}}}"
        )
        payload = self._json(
            self._gh(
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"tag={tag}",
            ),
            subject="release absence readback",
        )
        if payload.get("errors"):
            raise RuntimeError("release absence readback returned provider errors")
        data = payload.get("data")
        repository_payload = (
            data.get("repository") if isinstance(data, Mapping) else None
        )
        if (
            not isinstance(repository_payload, Mapping)
            or "release" not in repository_payload
        ):
            raise RuntimeError("release absence readback is incomplete")
        release = repository_payload.get("release")
        if release is not None and not isinstance(release, Mapping):
            raise RuntimeError("release absence readback returned invalid content")
        reference = self._repository_ref(
            coordinate=repository, qualified_name=f"refs/tags/{tag}"
        )
        return release is None and reference is None

    def _now(self) -> str:
        return (
            self._clock()
            .astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
