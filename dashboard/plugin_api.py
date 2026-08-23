"""Thin dashboard REST adapter for Map Governance."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status as http_status,
)
from pydantic import BaseModel, Field


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from map_governance.runtime import (  # noqa: E402
    ProfileResolutionError,
    application_for_profile,
    prerequisite_application_for_profile,
)
from map_governance import (  # noqa: E402
    ApprovalEnforcementError,
    ApprovalRequestConflict,
    CEOSessionRepairRequired,
    DecisionResumePendingError,
    GovernanceActorIdentity,
    GovernanceAuthorizationError,
    GovernanceRequestIdentity,
    MapBindingError,
    MapTransitionError,
    StaleProjectionError,
    SetupApplyError,
    CommissioningAuthorizationError,
    CommissioningPrerequisiteError,
    CoordinatorRuntimeError,
)
from map_governance.tracker import TrackerError  # noqa: E402


router = APIRouter()


def _ws_upgrade_authorized(ws: WebSocket) -> bool:
    """Delegate every dashboard auth mode to the installed canonical WS gate."""
    try:
        from hermes_cli import web_server
    except Exception:
        return False
    return bool(web_server._ws_auth_ok(ws))


class ConfigureProjectRequest(BaseModel):
    project_url: str = Field(min_length=1)


class BindMapRequest(BaseModel):
    project_id: str = Field(min_length=1)
    issue_url: str = Field(min_length=1)


class RefreshRequest(BaseModel):
    project_id: str | None = Field(default=None, min_length=1)


class TransitionMapRequest(BaseModel):
    map_id: str = Field(min_length=1)
    expected_stage: str = Field(min_length=1)
    requested_stage: str = Field(min_length=1)
    approval_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    mutation_id: str | None = Field(default=None, min_length=1, max_length=128)


class ApprovalDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected|revision)$")
    note: str = Field(min_length=1)


class OutboxRecoveryRequest(BaseModel):
    limit: int = Field(default=100, ge=1, le=1000)


class OutboxRepairRequest(BaseModel):
    repair_id: str = Field(min_length=1, max_length=256)
    note: str = Field(min_length=1)


class SetupPlanRequest(BaseModel):
    desired: dict[str, Any]


class SetupApplyRequest(BaseModel):
    plan: dict[str, Any]
    selected_action_ids: list[str] = Field(min_length=1)


class CommissionMapRequest(BaseModel):
    session_id: str = Field(min_length=1)


def _application(profile: str):
    try:
        return application_for_profile(profile)
    except ProfileResolutionError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def _operation(profile: str, method: str, **arguments):
    application = _application(profile)
    try:
        return getattr(application, method)(**arguments)
    except MapTransitionError as error:
        raise HTTPException(status_code=409, detail=error.as_dict()) from error
    except ApprovalEnforcementError as error:
        raise HTTPException(status_code=409, detail=error.as_dict()) from error
    except ApprovalRequestConflict as error:
        raise HTTPException(status_code=409, detail=error.as_dict()) from error
    except GovernanceAuthorizationError as error:
        raise HTTPException(status_code=403, detail=error.as_dict()) from error
    except CommissioningAuthorizationError as error:
        raise HTTPException(status_code=409, detail=error.as_dict()) from error
    except CommissioningPrerequisiteError as error:
        raise HTTPException(status_code=503, detail=error.as_dict()) from error
    except CoordinatorRuntimeError as error:
        status_code = 409 if error.repair_required else 503
        raise HTTPException(status_code=status_code, detail=error.as_dict()) from error
    except DecisionResumePendingError as error:
        raise HTTPException(status_code=503, detail=error.as_dict()) from error
    except CEOSessionRepairRequired as error:
        raise HTTPException(status_code=409, detail=error.as_dict()) from error
    except MapBindingError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except StaleProjectionError as error:
        raise HTTPException(status_code=503, detail=error.as_dict()) from error
    except TrackerError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


def _prerequisite_operation(profile: str, method: str, **arguments):
    try:
        application = prerequisite_application_for_profile(profile)
        return getattr(application, method)(**arguments)
    except ProfileResolutionError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except SetupApplyError as error:
        raise HTTPException(status_code=409, detail=error.as_dict()) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def _chairman_identity(request: Request, *, profile: str) -> GovernanceActorIdentity:
    session = getattr(request.state, "session", None)
    if session is None:
        raise HTTPException(
            status_code=403,
            detail="An authenticated interactive chairman session is required",
        )
    user_id = str(getattr(session, "user_id", "") or "")
    provider = str(getattr(session, "provider", "") or "")
    if not user_id or not provider:
        raise HTTPException(
            status_code=403,
            detail="The authenticated chairman identity is incomplete",
        )
    actor_id = f"{provider}:{user_id}"
    return GovernanceActorIdentity(
        role="chairman",
        profile_name=profile,
        actor_id=actor_id,
        session_id=f"dashboard:{actor_id}",
    )


@router.get("/health")
async def health(profile: str = Query(min_length=1)):
    return _application(profile).health()


@router.get("/doctor")
async def doctor(profile: str = Query(min_length=1)):
    return await asyncio.to_thread(_prerequisite_operation, profile, "doctor")


@router.post("/setup/plan")
async def setup_plan(
    request: SetupPlanRequest,
    profile: str = Query(min_length=1),
):
    return await asyncio.to_thread(
        _prerequisite_operation,
        profile,
        "setup_plan",
        desired=request.desired,
    )


@router.post("/setup/apply")
async def setup_apply(
    request: SetupApplyRequest,
    profile: str = Query(min_length=1),
):
    return await asyncio.to_thread(
        _prerequisite_operation,
        profile,
        "setup_apply",
        plan=request.plan,
        selected_action_ids=request.selected_action_ids,
    )


@router.get("/board")
async def board(profile: str = Query(min_length=1)):
    application = _application(profile)
    snapshot = getattr(application, "board_snapshot", None)
    return snapshot() if callable(snapshot) else application.board()


@router.websocket("/events")
async def stream_events(ws: WebSocket):
    """Catch up and tail the committed SQLite journal without an in-memory bus."""
    if not _ws_upgrade_authorized(ws):
        await ws.close(code=http_status.WS_1008_POLICY_VIOLATION)
        return
    profile = ws.query_params.get("profile", "")
    try:
        application = _application(profile)
        cursor = int(ws.query_params.get("cursor", "0"))
        if cursor < 0:
            raise ValueError
    except (ValueError, ProfileResolutionError):
        await ws.close(code=http_status.WS_1008_POLICY_VIOLATION)
        return
    settings = application.board_stream_settings
    send_timeout = float(getattr(settings, "send_timeout_seconds", 5.0))
    await ws.accept()
    opened = False
    try:
        while True:
            batch = await asyncio.to_thread(
                application.board_events,
                cursor=cursor,
                limit=settings.batch_size,
            )
            if batch["status"] == "refresh_required":
                await asyncio.wait_for(ws.send_json(batch), timeout=send_timeout)
                await ws.close(code=http_status.WS_1000_NORMAL_CLOSURE)
                return
            if not opened:
                await asyncio.wait_for(
                    ws.send_json(
                        {
                            "status": "open",
                            "cursor": cursor,
                            "latest_cursor": batch["latest_cursor"],
                        }
                    ),
                    timeout=send_timeout,
                )
                opened = True
            if batch["events"]:
                cursor = int(batch["cursor"])
                await asyncio.wait_for(ws.send_json(batch), timeout=send_timeout)
                if batch["has_more"]:
                    continue
            try:
                message = await asyncio.wait_for(
                    ws.receive(), timeout=float(settings.poll_seconds)
                )
                if message["type"] == "websocket.disconnect":
                    return
            except asyncio.TimeoutError:
                pass
    except (WebSocketDisconnect, asyncio.CancelledError):
        return
    except asyncio.TimeoutError:
        try:
            await ws.close(code=http_status.WS_1013_TRY_AGAIN_LATER)
        except Exception:
            pass
    except Exception:
        try:
            await ws.close(code=http_status.WS_1011_INTERNAL_ERROR)
        except Exception:
            pass


@router.get("/maps/{map_id}")
async def map_detail(map_id: str, profile: str = Query(min_length=1)):
    return _operation(profile, "map_detail", map_id=map_id)


@router.get("/outbox/{effect_id}")
async def outbox_status(effect_id: str, profile: str = Query(min_length=1)):
    return _operation(profile, "outbox_status", effect_id=effect_id)


@router.post("/outbox/recover")
async def recover_outbox(
    request: OutboxRecoveryRequest,
    profile: str = Query(min_length=1),
):
    return await asyncio.to_thread(
        _operation,
        profile,
        "recover_outbox",
        limit=request.limit,
    )


@router.post("/outbox/{effect_id}/repair")
async def repair_outbox(
    effect_id: str,
    request: OutboxRepairRequest,
    profile: str = Query(min_length=1),
):
    return await asyncio.to_thread(
        _operation,
        profile,
        "repair_outbox",
        effect_id=effect_id,
        repair_id=request.repair_id,
        note=request.note,
    )


@router.post("/maps/{map_id}/session")
async def open_map_session(map_id: str, profile: str = Query(min_length=1)):
    return await asyncio.to_thread(
        _operation,
        profile,
        "open_map",
        map_id=map_id,
    )


@router.post("/maps/{map_id}/commission")
async def commission_map(
    map_id: str,
    request: CommissionMapRequest,
    profile: str = Query(min_length=1),
):
    return await asyncio.to_thread(
        _operation,
        profile,
        "commission_map",
        map_id=map_id,
        request_identity=GovernanceRequestIdentity(profile, request.session_id),
    )


@router.post("/maps/{map_id}/resume")
async def resume_map_runtime(
    map_id: str,
    request: CommissionMapRequest,
    profile: str = Query(min_length=1),
):
    return await commission_map(map_id, request, profile)


@router.get("/maps/{map_id}/runtime")
async def map_runtime_status(
    map_id: str,
    profile: str = Query(min_length=1),
    session_id: str = Query(min_length=1),
):
    return await asyncio.to_thread(
        _operation,
        profile,
        "runtime_status",
        map_id=map_id,
        request_identity=GovernanceRequestIdentity(profile, session_id),
    )


@router.post("/projects")
async def configure_project(
    request: ConfigureProjectRequest,
    profile: str = Query(min_length=1),
):
    return await asyncio.to_thread(
        _operation,
        profile,
        "configure_project",
        project_url=request.project_url,
    )


@router.post("/bindings")
async def bind_map(
    request: BindMapRequest,
    profile: str = Query(min_length=1),
):
    return await asyncio.to_thread(
        _operation,
        profile,
        "bind_map",
        project_id=request.project_id,
        issue_url=request.issue_url,
    )


@router.post("/refresh")
async def refresh(
    request: RefreshRequest,
    profile: str = Query(min_length=1),
):
    return await asyncio.to_thread(
        _operation,
        profile,
        "refresh",
        project_id=request.project_id,
    )


@router.post("/transitions")
async def transition_map(
    request: TransitionMapRequest,
    http_request: Request,
    profile: str = Query(min_length=1),
):
    arguments: dict[str, Any] = {
        "map_id": request.map_id,
        "expected_stage": request.expected_stage,
        "requested_stage": request.requested_stage,
    }
    if request.approval_request_id is not None:
        arguments["approval_request_id"] = request.approval_request_id
    if request.mutation_id is not None:
        arguments["mutation_id"] = request.mutation_id
    if getattr(http_request.state, "session", None) is not None:
        arguments["actor_identity"] = _chairman_identity(
            http_request,
            profile=profile,
        )
    return await asyncio.to_thread(
        _operation,
        profile,
        "transition_map",
        **arguments,
    )


@router.post("/maps/{map_id}/approvals/{request_id}/decision")
async def decide_approval(
    map_id: str,
    request_id: str,
    request: ApprovalDecisionRequest,
    http_request: Request,
    profile: str = Query(min_length=1),
):
    return await asyncio.to_thread(
        _operation,
        profile,
        "decide_approval",
        map_id=map_id,
        request_id=request_id,
        actor_identity=_chairman_identity(http_request, profile=profile),
        decision=request.decision,
        note=request.note,
    )
