"""Composition root shared by native and dashboard adapters."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Mapping

from .application import GovernanceRequestIdentity, MapGovernanceApplication
from .coordinator import CoordinatorRuntime
from .approvals import AuthorityEnvelopePolicy
from .outbox import OutboxSettings
from .events import BoardEventSettings
from .prerequisites import PrerequisiteApplication, YamlConfigRepository
from .sessions import HermesSessionAdapter, HermesSessionDatabaseBackend
from .storage import PluginStorage


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "map-governance"
_PROFILE_APPLICATIONS: dict[tuple[str, str], MapGovernanceApplication] = {}
_PROFILE_APPLICATIONS_LOCK = Lock()


class ProfileResolutionError(ValueError):
    """Raised when a request does not identify an existing Hermes profile."""


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
) -> MapGovernanceApplication:
    """Build the application for an explicitly selected storage directory."""
    session_runner = (
        HermesSessionAdapter(HermesSessionDatabaseBackend(state_database))
        if profile_name is not None and state_database is not None
        else None
    )
    application = MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        session_runner=session_runner,
        profile_name=profile_name,
        authority_policy=AuthorityEnvelopePolicy.from_settings(authority_settings),
        outbox_settings=OutboxSettings(**dict(outbox_settings or {})),
        event_settings=BoardEventSettings(**dict(event_settings or {})),
        commissioning_prerequisites=commissioning_prerequisites,
        coordinator_runtime=coordinator_runtime,
        coordinator_resume=coordinator_runtime,
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

    cache_key = (canonical_profile, str(storage_root.resolve()))
    with _PROFILE_APPLICATIONS_LOCK:
        existing = _PROFILE_APPLICATIONS.get(cache_key)
        if existing is not None:
            return existing
        prerequisites = PrerequisiteApplication(
            plugin_root=PLUGIN_ROOT,
            storage_root=storage_root,
            config_repository=YamlConfigRepository(
                storage_root / "prerequisites.yaml", storage_root=storage_root
            ),
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
