"""Composition root shared by native and dashboard adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .application import MapGovernanceApplication
from .approvals import AuthorityEnvelopePolicy
from .sessions import HermesSessionAdapter, HermesSessionDatabaseBackend


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "map-governance"


class ProfileResolutionError(ValueError):
    """Raised when a request does not identify an existing Hermes profile."""


def application_for_storage(
    storage_root: Path,
    *,
    profile_name: str | None = None,
    state_database: Path | None = None,
    authority_settings: Mapping[str, Any] | None = None,
) -> MapGovernanceApplication:
    """Build the application for an explicitly selected storage directory."""
    session_runner = (
        HermesSessionAdapter(HermesSessionDatabaseBackend(state_database))
        if profile_name is not None and state_database is not None
        else None
    )
    return MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
        session_runner=session_runner,
        profile_name=profile_name,
        authority_policy=AuthorityEnvelopePolicy.from_settings(authority_settings),
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
    finally:
        reset_hermes_home_override(token)

    return application_for_storage(
        storage_root,
        profile_name=canonical_profile,
        state_database=profile_home / "state.db",
        authority_settings=(
            authority_settings if isinstance(authority_settings, dict) else None
        ),
    )
