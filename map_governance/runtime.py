"""Composition root shared by native and dashboard adapters."""

from __future__ import annotations

from pathlib import Path

from .application import MapGovernanceApplication


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "map-governance"


class ProfileResolutionError(ValueError):
    """Raised when a request does not identify an existing Hermes profile."""


def application_for_storage(storage_root: Path) -> MapGovernanceApplication:
    """Build the application for an explicitly selected storage directory."""
    return MapGovernanceApplication(
        plugin_root=PLUGIN_ROOT,
        storage_root=storage_root,
    )


def application_for_profile(profile: str) -> MapGovernanceApplication:
    """Build the application for an explicit dashboard request profile."""
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

    return application_for_storage(storage_root)
