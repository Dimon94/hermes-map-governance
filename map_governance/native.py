"""Thin Hermes-native adapters for Map Governance."""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser, Namespace

from .application import (
    ApprovalEnforcementError,
    ApprovalRequestConflict,
    CEOSessionRepairRequired,
    GovernanceRequestIdentity,
    GovernanceAuthorizationError,
    MapBindingError,
    MapTransitionError,
    StaleProjectionError,
)
from .coordinator import (
    CommissioningAuthorizationError,
    CommissioningPrerequisiteError,
    CoordinatorRuntimeError,
)
from .ceo_tool import register_ceo_capabilities
from .pm_tool import register_pm_capabilities
from .prerequisites import SetupApplyError
from .runtime import (
    application_for_profile,
    application_for_storage,
    prerequisite_application_for_storage,
)
from .tracker import TrackerError


def _setup_maps_command(parser: ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="maps_command", required=True)
    commands.add_parser("health", help="Check Map Governance readiness")
    commands.add_parser("doctor", help="Read all Map Governance prerequisite evidence")
    setup = commands.add_parser(
        "setup", help="Plan or apply explicit prerequisite configuration"
    )
    setup_commands = setup.add_subparsers(dest="setup_command", required=True)
    setup_plan = setup_commands.add_parser(
        "plan", help="Preview stable setup action IDs without changing config"
    )
    setup_plan.add_argument(
        "--file", required=True, help="JSON file containing desired prerequisites"
    )
    setup_apply = setup_commands.add_parser(
        "apply", help="Apply explicitly selected actions from a saved plan"
    )
    setup_apply.add_argument(
        "--file", required=True, help="JSON file containing the setup plan"
    )
    setup_apply.add_argument(
        "--action",
        action="append",
        required=True,
        dest="actions",
        help="Stable action ID to apply; repeat for each authorized action",
    )
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
    commission = commands.add_parser(
        "commission", help="Commission or resume the Map's one Hermes PM"
    )
    commission.add_argument("--map", required=True, help="Bound Map Issue node id")
    commission.add_argument("--profile", required=True, help="Explicit CEO profile")
    commission.add_argument(
        "--session", required=True, help="Request-scoped canonical CEO session id"
    )
    resume = commands.add_parser(
        "resume", help="Resume the Map's verified plugin-owned Hermes PM"
    )
    resume.add_argument("--map", required=True, help="Bound Map Issue node id")
    resume.add_argument("--profile", required=True, help="Explicit CEO profile")
    resume.add_argument(
        "--session", required=True, help="Request-scoped canonical CEO session id"
    )
    runtime = commands.add_parser(
        "runtime", help="Inspect plugin-owned PM runtime state"
    )
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_status = runtime_commands.add_parser(
        "status", help="Read commission/resume and repair evidence"
    )
    runtime_status.add_argument("--map", required=True, help="Bound Map Issue node id")
    runtime_status.add_argument("--profile", required=True, help="Explicit CEO profile")
    runtime_status.add_argument(
        "--session", required=True, help="Request-scoped canonical CEO session id"
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

    outbox = commands.add_parser("outbox", help="Inspect and recover durable effects")
    outbox_commands = outbox.add_subparsers(dest="outbox_command", required=True)
    status = outbox_commands.add_parser("status", help="Inspect one durable effect")
    status.add_argument("--effect", required=True, help="Stable external effect id")
    recover = outbox_commands.add_parser("recover", help="Dispatch due effects")
    recover.add_argument("--limit", type=int, default=100)
    repair = outbox_commands.add_parser("repair", help="Requeue a terminal effect")
    repair.add_argument("--effect", required=True, help="Stable external effect id")
    repair.add_argument("--repair-id", required=True, help="Stable repair audit id")
    repair.add_argument("--note", required=True, help="Operator repair rationale")


def register(ctx) -> None:
    """Register the native diagnostic capability with Hermes."""
    register_ceo_capabilities(ctx)
    register_pm_capabilities(ctx)
    get_config = getattr(ctx, "get_config", lambda _key, default=None: default)
    authority_settings = get_config("authority", {})
    outbox_settings = get_config("outbox", {})
    event_settings = get_config("events", {})
    application = None

    def build_operational_application():
        built = (
            application_for_storage(
                ctx.state.data_dir,
                authority_settings=authority_settings,
                outbox_settings=outbox_settings,
                event_settings=event_settings,
            )
            if authority_settings or outbox_settings or event_settings
            else application_for_storage(ctx.state.data_dir)
        )
        start_outbox_runtime = getattr(built, "start_outbox_runtime", None)
        if callable(start_outbox_runtime):
            start_outbox_runtime()
        return built

    def operational_application():
        nonlocal application
        if application is None:
            application = build_operational_application()
        return application

    def prerequisite_application():
        return prerequisite_application_for_storage(ctx.state.data_dir)

    def read_json_file(path: str) -> dict:
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except OSError:
            raise ValueError("Setup JSON file is unavailable") from None
        except json.JSONDecodeError:
            raise ValueError("Setup JSON file is invalid JSON") from None
        if not isinstance(payload, dict):
            raise ValueError("Setup JSON root must be an object")
        return payload

    def _handle_maps_command(args: Namespace) -> int:
        if args.maps_command == "doctor":
            report = prerequisite_application().doctor()
            print(json.dumps(report, sort_keys=True))
            return 0 if report["status"] == "pass" else 1
        if args.maps_command == "setup":
            try:
                payload = read_json_file(args.file)
                if args.setup_command == "plan":
                    report = prerequisite_application().setup_plan(desired=payload)
                else:
                    report = prerequisite_application().setup_apply(
                        plan=payload,
                        selected_action_ids=args.actions,
                    )
            except SetupApplyError as error:
                print(
                    json.dumps({"error": error.as_dict()}, sort_keys=True),
                    file=sys.stderr,
                )
                return 1
            except ValueError as error:
                print(
                    json.dumps(
                        {
                            "error": {
                                "type": "setup_input_error",
                                "reason": str(error),
                                "retryable": False,
                            }
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
                return 1
            print(json.dumps(report, sort_keys=True))
            return 0
        current_application = operational_application()
        if args.maps_command == "health":
            print(json.dumps(current_application.health(), sort_keys=True))
            return 0
        if args.maps_command == "board":
            print(json.dumps(current_application.board(), sort_keys=True))
            return 0
        if args.maps_command == "detail":
            report = current_application.map_detail(map_id=args.map)
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
        if args.maps_command in {"commission", "resume", "runtime"}:
            identity = GovernanceRequestIdentity(
                profile_name=args.profile,
                session_id=args.session,
            )
            try:
                selected = application_for_profile(args.profile)
                report = (
                    selected.commission_map(
                        map_id=args.map,
                        request_identity=identity,
                    )
                    if args.maps_command in {"commission", "resume"}
                    else selected.runtime_status(
                        map_id=args.map,
                        request_identity=identity,
                    )
                )
            except (
                CommissioningAuthorizationError,
                CommissioningPrerequisiteError,
                CoordinatorRuntimeError,
                GovernanceAuthorizationError,
            ) as error:
                print(
                    json.dumps({"error": error.as_dict()}, sort_keys=True),
                    file=sys.stderr,
                )
                return 1
            print(json.dumps(report, sort_keys=True))
            return 0
        if args.maps_command == "project" and args.project_command == "configure":
            report = current_application.configure_project(project_url=args.url)
            print(json.dumps(report, sort_keys=True))
            return 0
        if args.maps_command == "bind":
            report = current_application.bind_map(
                project_id=args.project,
                issue_url=args.issue,
            )
            print(json.dumps(report, sort_keys=True))
            return 0
        if args.maps_command == "refresh":
            report = current_application.refresh(project_id=args.project)
            print(json.dumps(report, sort_keys=True))
            return 0
        if args.maps_command == "outbox":
            try:
                if args.outbox_command == "status":
                    report = current_application.outbox_status(effect_id=args.effect)
                elif args.outbox_command == "recover":
                    report = current_application.recover_outbox(limit=args.limit)
                else:
                    report = current_application.repair_outbox(
                        effect_id=args.effect,
                        repair_id=args.repair_id,
                        note=args.note,
                    )
            except ValueError as error:
                print(
                    json.dumps(
                        {
                            "error": {
                                "type": "outbox_error",
                                "reason": str(error),
                                "retryable": False,
                            }
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
                return 1
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
                report = current_application.transition_map(
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

    def handle_maps_command(args: Namespace) -> int:
        try:
            return _handle_maps_command(args)
        except StaleProjectionError as error:
            print(
                json.dumps({"error": error.as_dict()}, sort_keys=True),
                file=sys.stderr,
            )
            return 1

    ctx.register_cli_command(
        name="maps",
        help="Inspect the Map Governance plugin",
        setup_fn=_setup_maps_command,
        handler_fn=handle_maps_command,
        description="Map Governance diagnostics",
    )
