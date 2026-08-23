"""Thin Hermes-native adapters for Map Governance."""

from __future__ import annotations

import json
from argparse import ArgumentParser, Namespace

from .runtime import application_for_storage


def _setup_maps_command(parser: ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="maps_command", required=True)
    commands.add_parser("health", help="Check Map Governance readiness")


def register(ctx) -> None:
    """Register the native diagnostic capability with Hermes."""
    application = application_for_storage(ctx.state.data_dir)

    def handle_maps_command(args: Namespace) -> int:
        if args.maps_command == "health":
            print(json.dumps(application.health(), sort_keys=True))
            return 0
        return 2

    ctx.register_cli_command(
        name="maps",
        help="Inspect the Map Governance plugin",
        setup_fn=_setup_maps_command,
        handler_fn=handle_maps_command,
        description="Map Governance diagnostics",
    )
