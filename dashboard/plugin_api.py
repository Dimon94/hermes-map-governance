"""Thin dashboard REST adapter for Map Governance."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from map_governance.runtime import (  # noqa: E402
    ProfileResolutionError,
    application_for_profile,
)
from map_governance import (  # noqa: E402
    CEOSessionRepairRequired,
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
    except CEOSessionRepairRequired as error:
        raise HTTPException(status_code=409, detail=error.as_dict()) from error
    except MapBindingError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except TrackerError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


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
    profile: str = Query(min_length=1),
):
    return await asyncio.to_thread(
        _operation,
        profile,
        "transition_map",
        map_id=request.map_id,
        expected_stage=request.expected_stage,
        requested_stage=request.requested_stage,
    )
