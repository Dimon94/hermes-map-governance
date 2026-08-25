"""Composition root shared by native and dashboard adapters."""

from __future__ import annotations

import os
import grp
import pwd
import stat
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Mapping

from .application import GovernanceRequestIdentity, MapGovernanceApplication
from .coordinator import CoordinatorRuntime
from .approvals import AuthorityEnvelopePolicy
from .outbox import OutboxSettings
from .events import BoardEventSettings
from .publication import (
    GitBundlePublicationHandoff,
    GitHubPublisherBoundary,
    PublicationHandoff,
    PublisherBoundary,
)
from .prerequisites import PrerequisiteApplication, YamlConfigRepository
from .sessions import HermesSessionAdapter, HermesSessionDatabaseBackend
from .storage import PluginStorage
from .tracker import GitHubTrackerAdapter, SubprocessCommandRunner, TrackerAdapter


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "map-governance"
_PROFILE_APPLICATIONS: dict[tuple[str, str, str], MapGovernanceApplication] = {}
_PROFILE_APPLICATIONS_LOCK = Lock()


def _default_tracker_for_project(_project_url: str) -> TrackerAdapter:
    """Create one ambient-auth prototype client for one project namespace."""
    return GitHubTrackerAdapter()


class ProfileResolutionError(ValueError):
    """Raised when a request does not identify an existing Hermes profile."""


def _publisher_control_configuration(storage_root: Path) -> dict[str, Any]:
    """Read worker-owned control config through the explicit shared-group seam."""
    root = storage_root.lstat()
    config_path = storage_root / "prerequisites.yaml"
    descriptor: int | None = None
    try:
        descriptor = os.open(config_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            decoded = YamlConfigRepository._decode(handle.read())
    except OSError as error:
        raise ProfileResolutionError(
            "Publisher control configuration is unavailable"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    config = decoded.get("prerequisites") if isinstance(decoded, dict) else None
    authorities = config.get("authorities") if isinstance(config, dict) else None
    worker = authorities.get("worker") if isinstance(authorities, dict) else None
    publisher = authorities.get("publisher") if isinstance(authorities, dict) else None
    if not isinstance(worker, dict) or not isinstance(publisher, dict):
        raise ProfileResolutionError("Publisher control identities are unavailable")
    try:
        worker_account = pwd.getpwnam(str(worker.get("os_user") or ""))
        publisher_account = pwd.getpwnam(str(publisher.get("os_user") or ""))
        control_group = grp.getgrnam(str(publisher.get("control_group") or ""))
    except KeyError as error:
        raise ProfileResolutionError(
            "Publisher control identities or group do not exist"
        ) from error

    def group_ids(account: Any) -> set[int]:
        return {account.pw_gid} | {
            item.gr_gid for item in grp.getgrall() if account.pw_name in item.gr_mem
        }

    root_mode = stat.S_IMODE(root.st_mode)
    config_mode = stat.S_IMODE(metadata.st_mode)
    database_path = storage_root / "registry.db"
    try:
        database = database_path.lstat()
    except OSError as error:
        raise ProfileResolutionError(
            "Publisher control database is unavailable"
        ) from error
    safe = (
        stat.S_ISDIR(root.st_mode)
        and not stat.S_ISLNK(root.st_mode)
        and root.st_uid == worker_account.pw_uid
        and root.st_gid == control_group.gr_gid
        and root_mode == 0o2770
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and metadata.st_uid == worker_account.pw_uid
        and metadata.st_gid == control_group.gr_gid
        and config_mode == 0o640
        and stat.S_ISREG(database.st_mode)
        and database.st_nlink == 1
        and database.st_uid == worker_account.pw_uid
        and database.st_gid == control_group.gr_gid
        and stat.S_IMODE(database.st_mode) == 0o660
        and control_group.gr_gid in group_ids(worker_account)
        and control_group.gr_gid in group_ids(publisher_account)
        and os.geteuid() == publisher_account.pw_uid
    )
    if not safe:
        raise ProfileResolutionError(
            "Publisher control storage must use worker ownership and shared-group ACL"
        )
    return decoded


def _require_configured_process_identity(
    storage_root: Path, *, publisher_process: bool
) -> int | None:
    """Bind worker and publisher compositions to distinct real OS identities."""
    config_path = storage_root / "prerequisites.yaml"
    if not storage_root.is_dir() or not config_path.is_file():
        return None
    stored = (
        _publisher_control_configuration(storage_root)
        if publisher_process
        else YamlConfigRepository(config_path, storage_root=storage_root).read()
    )
    config = stored.get("prerequisites") if isinstance(stored, dict) else None
    authorities = config.get("authorities") if isinstance(config, dict) else None
    github = config.get("github") if isinstance(config, dict) else None
    repositories = github.get("repositories") if isinstance(github, dict) else None
    worker = authorities.get("worker") if isinstance(authorities, dict) else None
    publisher = authorities.get("publisher") if isinstance(authorities, dict) else None
    publication_enabled = isinstance(publisher, dict) and (
        publisher.get("required") is True
        or (
            isinstance(repositories, list)
            and any(
                isinstance(item, dict) and item.get("publication_required") is True
                for item in repositories
            )
        )
    )
    if not publication_enabled:
        return None
    if not isinstance(worker, dict) or not isinstance(publisher, dict):
        raise ProfileResolutionError(
            "Publication service identities are not configured"
        )
    try:
        worker_account = pwd.getpwnam(str(worker.get("os_user") or ""))
        publisher_account = pwd.getpwnam(str(publisher.get("os_user") or ""))
        control_group = grp.getgrnam(str(publisher.get("control_group") or ""))
    except KeyError as error:
        raise ProfileResolutionError(
            "Publication service identities must resolve to real OS users"
        ) from error
    worker_uid = worker_account.pw_uid
    publisher_uid = publisher_account.pw_uid
    if worker_uid == publisher_uid:
        raise ProfileResolutionError(
            "Worker and publisher must use distinct OS service identities"
        )
    expected_uid = publisher_uid if publisher_process else worker_uid
    if os.geteuid() != expected_uid:
        role = "Publisher" if publisher_process else "Worker/PM"
        raise ProfileResolutionError(
            f"{role} composition must run under its configured OS service identity"
        )
    member_names = set(control_group.gr_mem)
    if not all(
        account.pw_gid == control_group.gr_gid or account.pw_name in member_names
        for account in (worker_account, publisher_account)
    ):
        raise ProfileResolutionError(
            "Worker and publisher must both belong to the control group"
        )
    if not publisher_process:
        os.chown(config_path, -1, control_group.gr_gid)
        os.chmod(config_path, 0o640)
    return control_group.gr_gid


def application_for_storage(
    storage_root: Path,
    *,
    profile_name: str | None = None,
    state_database: Path | None = None,
    authority_settings: Mapping[str, Any] | None = None,
    outbox_settings: Mapping[str, Any] | None = None,
    recover_pending: bool = True,
    event_settings: Mapping[str, Any] | None = None,
    commissioning_prerequisites: PrerequisiteApplication | None = None,
    coordinator_runtime: CoordinatorRuntime | None = None,
    publisher: PublisherBoundary | None = None,
    tracker: TrackerAdapter | None = None,
    tracker_for_project: Callable[[str], TrackerAdapter] | None = None,
) -> MapGovernanceApplication:
    """Build the application for an explicitly selected storage directory."""
    resolved_tracker_for_project = tracker_for_project
    if tracker is None and resolved_tracker_for_project is None:
        resolved_tracker_for_project = _default_tracker_for_project
    shared_gid = _require_configured_process_identity(
        storage_root, publisher_process=publisher is not None
    )
    publication_handoff: PublicationHandoff | None = None
    publication_authority_ref = (
        publisher.authority_ref if publisher is not None else None
    )
    if publisher is None:
        config_path = storage_root / "prerequisites.yaml"
        if storage_root.is_dir() and config_path.is_file():
            stored = YamlConfigRepository(config_path, storage_root=storage_root).read()
            prerequisites = (
                stored.get("prerequisites") if isinstance(stored, dict) else None
            )
            authorities = (
                prerequisites.get("authorities")
                if isinstance(prerequisites, dict)
                else None
            )
            github = (
                prerequisites.get("github") if isinstance(prerequisites, dict) else None
            )
            worker = (
                authorities.get("worker") if isinstance(authorities, dict) else None
            )
            publication = (
                authorities.get("publisher") if isinstance(authorities, dict) else None
            )
            if isinstance(publication, dict):
                configured_ref = publication.get("credential_ref")
                if isinstance(configured_ref, str) and configured_ref != "none":
                    publication_authority_ref = configured_ref
            repositories = (
                github.get("repositories") if isinstance(github, dict) else None
            )
            publication_paths = {
                str(item["coordinate"]): Path(str(item["path"]))
                for item in repositories or []
                if isinstance(item, dict) and item.get("publication_required") is True
            }
            if (
                publication_paths
                and isinstance(worker, dict)
                and isinstance(publication, dict)
            ):
                publication_handoff = GitBundlePublicationHandoff(
                    repositories=publication_paths,
                    handoff_root=storage_root / "publication-handoffs",
                    worker_os_user=str(worker.get("os_user") or ""),
                    control_group=str(publication.get("control_group") or ""),
                    git_executable=Path(str(worker.get("git_executable") or "")),
                )
    session_runner = (
        HermesSessionAdapter(HermesSessionDatabaseBackend(state_database))
        if profile_name is not None and state_database is not None
        else None
    )
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        tracker=tracker,
        tracker_for_project=resolved_tracker_for_project,
        session_runner=session_runner,
        profile_name=profile_name,
        authority_policy=AuthorityEnvelopePolicy.from_settings(authority_settings),
        outbox_settings=OutboxSettings(**dict(outbox_settings or {})),
        event_settings=BoardEventSettings(**dict(event_settings or {})),
        commissioning_prerequisites=commissioning_prerequisites,
        coordinator_runtime=coordinator_runtime,
        coordinator_resume=coordinator_runtime,
        publisher=publisher,
        publication_handoff=publication_handoff,
        publication_authority_ref=publication_authority_ref,
        storage_group_id=shared_gid,
    )
    if recover_pending:
        if (
            session_runner is not None
            and commissioning_prerequisites is not None
            and coordinator_runtime is not None
        ):
            application.recover_restart()
        else:
            application.recover_outbox()
    return application


def publisher_application_for_storage(
    storage_root: Path,
    *,
    request_profile_name: str,
    control_profile_name: str | None = None,
    authority_settings: Mapping[str, Any] | None = None,
    outbox_settings: Mapping[str, Any] | None = None,
    event_settings: Mapping[str, Any] | None = None,
) -> MapGovernanceApplication:
    """Build the explicit privileged CLI composition from setup-owned config."""
    stored_config = _publisher_control_configuration(storage_root)
    config = (
        stored_config.get("prerequisites") if isinstance(stored_config, dict) else None
    )
    profiles = config.get("profiles") if isinstance(config, dict) else None
    authorities = config.get("authorities") if isinstance(config, dict) else None
    worker = authorities.get("worker") if isinstance(authorities, dict) else None
    publisher = authorities.get("publisher") if isinstance(authorities, dict) else None
    github = config.get("github") if isinstance(config, dict) else None
    repositories = github.get("repositories") if isinstance(github, dict) else None
    hostname = github.get("hostname") if isinstance(github, dict) else None
    if (
        not isinstance(profiles, dict)
        or (
            control_profile_name is not None
            and profiles.get("ceo") != control_profile_name
        )
        or profiles.get("publisher") != request_profile_name
        or not isinstance(publisher, dict)
        or not isinstance(worker, dict)
        or publisher.get("kind") != "gh"
        or not isinstance(hostname, str)
        or not isinstance(repositories, list)
    ):
        raise ProfileResolutionError(
            "Privileged GitHub publisher is not configured for this plugin storage"
        )
    try:
        worker_uid = pwd.getpwnam(str(worker.get("os_user") or "")).pw_uid
        publisher_uid = pwd.getpwnam(str(publisher.get("os_user") or "")).pw_uid
    except KeyError as error:
        raise ProfileResolutionError(
            "Publication service identities must resolve to real OS users"
        ) from error
    if publisher_uid != os.geteuid() or worker_uid == publisher_uid:
        raise ProfileResolutionError(
            "Publisher must run under its configured OS service identity, distinct "
            "from the worker identity"
        )
    paths = {
        str(item["coordinate"]): Path(str(item["path"]))
        for item in repositories
        if isinstance(item, dict)
        and isinstance(item.get("coordinate"), str)
        and isinstance(item.get("path"), str)
        and item.get("publication_required") is True
    }
    if not paths:
        raise ProfileResolutionError(
            "No publication-required repository is configured for the publisher"
        )
    gh_config_dir = Path(str(publisher.get("gh_config_dir") or ""))
    if not gh_config_dir.is_absolute():
        raise ProfileResolutionError(
            "Publisher GH_CONFIG_DIR must be an explicit absolute path"
        )
    resolved_gh_config = gh_config_dir.resolve()
    if any(
        resolved_gh_config == path.resolve()
        or resolved_gh_config.is_relative_to(path.resolve())
        for path in paths.values()
    ):
        raise ProfileResolutionError(
            "Publisher GH_CONFIG_DIR must be outside worker repository paths"
        )
    try:
        boundary = GitHubPublisherBoundary(
            hostname=hostname,
            account=str(publisher.get("account") or ""),
            authority_ref=str(publisher.get("credential_ref") or ""),
            repositories=paths,
            handoff_root=storage_root / "publication-handoffs",
            worker_os_user=str(worker.get("os_user") or ""),
            control_group=str(publisher.get("control_group") or ""),
            gh_config_dir=gh_config_dir,
            git_executable=Path(str(publisher.get("git_executable") or "")),
            gh_executable=Path(str(publisher.get("gh_executable") or "")),
            profile_name=request_profile_name,
        )
    except ValueError as error:
        raise ProfileResolutionError(str(error)) from error
    tracker = GitHubTrackerAdapter(
        runner=SubprocessCommandRunner(
            environment=boundary.tracker_environment,
            redact_errors=True,
        ),
        executable=boundary.tracker_executable,
    )
    return application_for_storage(
        storage_root,
        authority_settings=authority_settings,
        outbox_settings=outbox_settings,
        event_settings=event_settings,
        publisher=boundary,
        tracker=tracker,
        recover_pending=False,
    )


def publisher_application_for_profile(
    control_profile: str,
    *,
    request_profile_name: str,
) -> MapGovernanceApplication:
    """Resolve a publisher request to one explicit control-plane profile."""
    from hermes_cli.config import load_config_readonly
    from hermes_cli.plugins import PluginState
    from hermes_cli.profiles import (
        get_profile_dir,
        normalize_profile_name,
        profile_exists,
        validate_profile_name,
    )
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    try:
        canonical_control = normalize_profile_name(control_profile)
        validate_profile_name(canonical_control)
    except ValueError as error:
        raise ProfileResolutionError(str(error)) from error
    if not profile_exists(canonical_control):
        raise ProfileResolutionError(
            f"Hermes control profile {canonical_control!r} does not exist"
        )
    control_home = get_profile_dir(canonical_control)
    token = set_hermes_home_override(control_home)
    try:
        storage_root = PluginState(PLUGIN_ID).data_dir
        config = load_config_readonly() or {}
        plugins = config.get("plugins") if isinstance(config, dict) else None
        entries = plugins.get("entries") if isinstance(plugins, dict) else None
        entry = entries.get(PLUGIN_ID) if isinstance(entries, dict) else None
        settings = entry.get("settings") if isinstance(entry, dict) else None
        authority_settings = (
            settings.get("authority") if isinstance(settings, dict) else None
        )
        outbox_settings = settings.get("outbox") if isinstance(settings, dict) else None
        event_settings = settings.get("events") if isinstance(settings, dict) else None
    finally:
        reset_hermes_home_override(token)
    return publisher_application_for_storage(
        storage_root,
        request_profile_name=request_profile_name,
        control_profile_name=canonical_control,
        authority_settings=(
            authority_settings if isinstance(authority_settings, dict) else None
        ),
        outbox_settings=(
            outbox_settings if isinstance(outbox_settings, dict) else None
        ),
        event_settings=event_settings if isinstance(event_settings, dict) else None,
    )


def application_for_profile(profile: str) -> MapGovernanceApplication:
    """Build the application for an explicit dashboard request profile."""
    from hermes_cli.plugins import PluginState
    from hermes_cli.config import load_config_readonly
    from hermes_cli.profiles import (
        get_profile_dir,
        normalize_profile_name,
        profile_exists,
        validate_profile_name,
    )
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    try:
        canonical_profile = normalize_profile_name(profile)
        validate_profile_name(canonical_profile)
    except ValueError as error:
        raise ProfileResolutionError(str(error)) from error
    if not profile_exists(canonical_profile):
        raise ProfileResolutionError(
            f"Hermes profile {canonical_profile!r} does not exist"
        )

    profile_home = get_profile_dir(canonical_profile)
    token = set_hermes_home_override(profile_home)
    try:
        storage_root = PluginState(PLUGIN_ID).data_dir
        config = load_config_readonly() or {}
        plugins = config.get("plugins") if isinstance(config, dict) else None
        entries = plugins.get("entries") if isinstance(plugins, dict) else None
        entry = entries.get(PLUGIN_ID) if isinstance(entries, dict) else None
        settings = entry.get("settings") if isinstance(entry, dict) else None
        authority_settings = (
            settings.get("authority") if isinstance(settings, dict) else None
        )
        outbox_settings = settings.get("outbox") if isinstance(settings, dict) else None
        event_settings = settings.get("events") if isinstance(settings, dict) else None
    finally:
        reset_hermes_home_override(token)

    storage_root.mkdir(parents=True, exist_ok=True)
    resolved_storage = str(storage_root.resolve())
    config_repository = YamlConfigRepository(
        storage_root / "prerequisites.yaml", storage_root=storage_root
    )
    while True:
        config_revision = config_repository.revision()
        cache_key = (canonical_profile, resolved_storage, config_revision)
        with _PROFILE_APPLICATIONS_LOCK:
            existing = _PROFILE_APPLICATIONS.get(cache_key)
            if existing is not None:
                return existing
            prerequisites = PrerequisiteApplication(
                plugin_root=PLUGIN_ROOT,
                storage_root=storage_root,
                config_repository=config_repository,
                profile_resolver=get_profile_dir,
            )
            coordinator = CoordinatorRuntime(
                storage=PluginStorage(storage_root),
                clock=lambda: (
                    datetime.now(timezone.utc)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z")
                ),
            )
            application = application_for_storage(
                storage_root,
                profile_name=canonical_profile,
                state_database=profile_home / "state.db",
                authority_settings=(
                    authority_settings if isinstance(authority_settings, dict) else None
                ),
                outbox_settings=(
                    outbox_settings if isinstance(outbox_settings, dict) else None
                ),
                event_settings=(
                    event_settings if isinstance(event_settings, dict) else None
                ),
                commissioning_prerequisites=prerequisites,
                coordinator_runtime=coordinator,
            )
            if config_repository.revision() != config_revision:
                application.stop_outbox_runtime()
                continue
            stale_keys = [
                key
                for key in _PROFILE_APPLICATIONS
                if key[:2] == (canonical_profile, resolved_storage) and key != cache_key
            ]
            for stale_key in stale_keys:
                stale = _PROFILE_APPLICATIONS.pop(stale_key)
                stale.stop_outbox_runtime()
            application.start_outbox_runtime()
            _PROFILE_APPLICATIONS[cache_key] = application
            return application


def application_for_pm_request(
    profile: str, *, session_id: str
) -> MapGovernanceApplication:
    """Resolve a PM request to its request-scoped CEO control-plane registry."""
    from hermes_cli.plugins import PluginState
    from hermes_cli.profiles import (
        get_profile_dir,
        normalize_profile_name,
        profile_exists,
        validate_profile_name,
    )
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    try:
        canonical_profile = normalize_profile_name(profile)
        validate_profile_name(canonical_profile)
    except ValueError as error:
        raise ProfileResolutionError(str(error)) from error
    if not profile_exists(canonical_profile) or not session_id:
        raise ProfileResolutionError("PM request identity is unavailable")
    profile_home = get_profile_dir(canonical_profile)
    token = set_hermes_home_override(profile_home)
    try:
        storage_root = PluginState(PLUGIN_ID).data_dir
    finally:
        reset_hermes_home_override(token)
    binding = PluginStorage(storage_root).pm_control_plane_for_request(
        profile_name=canonical_profile,
        session_id=session_id,
    )
    if binding is None:
        return application_for_profile(canonical_profile)
    control_profile = str(binding["control_profile"])
    application = application_for_profile(control_profile)
    identity = GovernanceRequestIdentity(canonical_profile, session_id)
    if not application.accepts_pm_control_binding(
        request_identity=identity,
        map_id=str(binding["map_id"]),
        coordinator_id=str(binding["coordinator_id"]),
    ):
        raise ProfileResolutionError("PM control-plane binding is inconsistent")
    return application


def prerequisite_application_for_profile(profile: str) -> PrerequisiteApplication:
    """Build the setup/doctor seam without initializing plugin storage."""
    from hermes_cli.plugins import PluginState
    from hermes_cli.profiles import (
        get_profile_dir,
        normalize_profile_name,
        profile_exists,
        validate_profile_name,
    )
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    try:
        canonical_profile = normalize_profile_name(profile)
        validate_profile_name(canonical_profile)
    except ValueError as error:
        raise ProfileResolutionError(str(error)) from error
    if not profile_exists(canonical_profile):
        raise ProfileResolutionError(
            f"Hermes profile {canonical_profile!r} does not exist"
        )

    profile_home = get_profile_dir(canonical_profile)
    token = set_hermes_home_override(profile_home)
    try:
        storage_root = PluginState(PLUGIN_ID).data_dir
    finally:
        reset_hermes_home_override(token)
    return PrerequisiteApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        config_repository=YamlConfigRepository(
            storage_root / "prerequisites.yaml", storage_root=storage_root
        ),
        profile_resolver=get_profile_dir,
    )


def prerequisite_application_for_storage(
    storage_root: Path,
) -> PrerequisiteApplication:
    """Build the native seam from request-scoped PluginState coordinates."""
    scoped_storage = Path(os.path.abspath(os.fspath(storage_root)))
    if scoped_storage.parent.name != "plugin-data":
        raise ProfileResolutionError(
            "Map Governance plugin storage does not identify a Hermes profile"
        )
    profile_home = scoped_storage.parent.parent
    hermes_root = (
        profile_home.parent.parent
        if profile_home.parent.name == "profiles"
        else profile_home
    )

    def resolve_profile(profile: str) -> Path:
        return (
            hermes_root if profile == "default" else hermes_root / "profiles" / profile
        )

    return PrerequisiteApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=scoped_storage,
        config_repository=YamlConfigRepository(
            scoped_storage / "prerequisites.yaml", storage_root=scoped_storage
        ),
        profile_resolver=resolve_profile,
    )
