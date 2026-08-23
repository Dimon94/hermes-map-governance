"""Thin dashboard REST adapter for Map Governance."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from map_governance.runtime import (  # noqa: E402
    ProfileResolutionError,
    application_for_profile,
)
from map_governance import (  # noqa: E402
    ApprovalEnforcementError,
    ApprovalRequestConflict,
    CEOSessionRepairRequired,
    GovernanceActorIdentity,
    GovernanceAuthorizationError,
    MapBindingError,
    MapTransitionError,
)
from map_governance.tracker import TrackerError  # noqa: E402


router = APIRouter()


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
    except CEOSessionRepairRequired as error:
        raise HTTPException(status_code=409, detail=error.as_dict()) from error
    except MapBindingError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except TrackerError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


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


@router.get("/board")
async def board(profile: str = Query(min_length=1)):
    return _application(profile).board()


@router.get("/maps/{map_id}")
async def map_detail(map_id: str, profile: str = Query(min_length=1)):
    return _operation(profile, "map_detail", map_id=map_id)


@router.post("/maps/{map_id}/session")
async def open_map_session(map_id: str, profile: str = Query(min_length=1)):
    return await asyncio.to_thread(
        _operation,
        profile,
        "open_map",
        map_id=map_id,
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
