"""Thin Hermes-native adapters for Map Governance."""

from __future__ import annotations

import json
from argparse import ArgumentParser, Namespace

from .runtime import application_for_storage


def _setup_maps_command(parser: ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="maps_command", required=True)
    commands.add_parser("health", help="Check Map Governance readiness")
    commands.add_parser("board", help="Read the current Maps board")

    project = commands.add_parser("project", help="Manage CEO projects")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    configure = project_commands.add_parser(
        "configure",
        help="Configure an existing GitHub Project as a CEO project",
    )
    configure.add_argument("--url", required=True, help="GitHub Project URL")

    bind = commands.add_parser("bind", help="Bind an existing GitHub Issue as a Map")
    bind.add_argument("--project", required=True, help="Configured Project node id")
    bind.add_argument("--issue", required=True, help="Existing GitHub Issue URL")

    refresh = commands.add_parser("refresh", help="Refresh board projections")
    refresh.add_argument("--project", help="Refresh only this configured Project")


def register(ctx) -> None:
    """Register the native diagnostic capability with Hermes."""
    application = application_for_storage(ctx.state.data_dir)

    def handle_maps_command(args: Namespace) -> int:
        if args.maps_command == "health":
            print(json.dumps(application.health(), sort_keys=True))
            return 0
        if args.maps_command == "board":
            print(json.dumps(application.board(), sort_keys=True))
            return 0
        if args.maps_command == "project" and args.project_command == "configure":
            report = application.configure_project(project_url=args.url)
            print(json.dumps(report, sort_keys=True))
            return 0
        if args.maps_command == "bind":
            report = application.bind_map(
                project_id=args.project,
                issue_url=args.issue,
            )
            print(json.dumps(report, sort_keys=True))
            return 0
        if args.maps_command == "refresh":
            report = application.refresh(project_id=args.project)
            print(json.dumps(report, sort_keys=True))
            return 0
        return 2

    ctx.register_cli_command(
        name="maps",
        help="Inspect the Map Governance plugin",
        setup_fn=_setup_maps_command,
        handler_fn=handle_maps_command,
        description="Map Governance diagnostics",
    )
