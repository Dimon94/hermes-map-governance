"""Thin Hermes-native adapters for Map Governance."""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser, Namespace

from .application import (
    ApprovalEnforcementError,
    ApprovalRequestConflict,
    CEOSessionRepairRequired,
    MapBindingError,
    MapTransitionError,
)
from .ceo_tool import register_ceo_capabilities
from .pm_tool import register_pm_capabilities
from .runtime import application_for_profile, application_for_storage
from .tracker import TrackerError


def _setup_maps_command(parser: ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="maps_command", required=True)
    commands.add_parser("health", help="Check Map Governance readiness")
    commands.add_parser("board", help="Read the current Maps board")
    detail = commands.add_parser("detail", help="Read one Map detail projection")
    detail.add_argument("--map", required=True, help="Bound Map Issue node id")
    open_session = commands.add_parser(
        "open", help="Resolve and bootstrap a Map's canonical CEO session"
    )
    open_session.add_argument("--map", required=True, help="Bound Map Issue node id")
    open_session.add_argument(
        "--profile", required=True, help="Explicit Hermes CEO profile"
    )

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

    transition = commands.add_parser(
        "transition",
        help="Request a governed Map stage transition",
    )
    transition.add_argument("--map", required=True, help="Bound Map Issue node id")
    transition.add_argument(
        "--from",
        dest="expected_stage",
        required=True,
        help="Stage shown when the transition was requested",
    )
    transition.add_argument("--stage", required=True, help="Requested executive stage")
    transition.add_argument(
        "--approval-request",
        help="Stable approval request id for a chairman-protected transition",
    )
    transition.add_argument(
        "--mutation-id",
        help="Stable idempotency identity for a protected transition attempt",
    )


def register(ctx) -> None:
    """Register the native diagnostic capability with Hermes."""
    register_ceo_capabilities(ctx)
    register_pm_capabilities(ctx)
    get_config = getattr(ctx, "get_config", lambda _key, default=None: default)
    authority_settings = get_config("authority", {})
    application = (
        application_for_storage(
            ctx.state.data_dir,
            authority_settings=authority_settings,
        )
        if authority_settings
        else application_for_storage(ctx.state.data_dir)
    )

    def handle_maps_command(args: Namespace) -> int:
        if args.maps_command == "health":
            print(json.dumps(application.health(), sort_keys=True))
            return 0
        if args.maps_command == "board":
            print(json.dumps(application.board(), sort_keys=True))
            return 0
        if args.maps_command == "detail":
            report = application.map_detail(map_id=args.map)
            print(json.dumps(report, sort_keys=True))
            return 0
        if args.maps_command == "open":
            try:
                report = application_for_profile(args.profile).open_map(map_id=args.map)
            except CEOSessionRepairRequired as error:
                print(
                    json.dumps({"error": error.as_dict()}, sort_keys=True),
                    file=sys.stderr,
                )
                return 1
            print(json.dumps(report, sort_keys=True))
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
        if args.maps_command == "transition":
            try:
                transition_arguments = {
                    "map_id": args.map,
                    "expected_stage": args.expected_stage,
                    "requested_stage": args.stage,
                }
                if args.approval_request is not None:
                    transition_arguments["approval_request_id"] = args.approval_request
                if args.mutation_id is not None:
                    transition_arguments["mutation_id"] = args.mutation_id
                report = application.transition_map(
                    **transition_arguments,
                )
            except (ApprovalEnforcementError, ApprovalRequestConflict) as error:
                print(
                    json.dumps({"error": error.as_dict()}, sort_keys=True),
                    file=sys.stderr,
                )
                return 1
            except MapTransitionError as error:
                print(
                    json.dumps({"error": error.as_dict()}, sort_keys=True),
                    file=sys.stderr,
                )
                return 1
            except MapBindingError as error:
                print(
                    json.dumps(
                        {
                            "error": {
                                "type": "binding_error",
                                "reason": str(error),
                                "retryable": False,
                            }
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
                return 1
            except TrackerError as error:
                print(
                    json.dumps(
                        {
                            "error": {
                                "type": "tracker_error",
                                "reason": str(error),
                                "retryable": True,
                            }
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
                return 1
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
