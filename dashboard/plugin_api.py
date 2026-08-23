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
from map_governance import MapBindingError  # noqa: E402
from map_governance.tracker import TrackerError  # noqa: E402


router = APIRouter()


class ConfigureProjectRequest(BaseModel):
    project_url: str = Field(min_length=1)


class BindMapRequest(BaseModel):
    project_id: str = Field(min_length=1)
    issue_url: str = Field(min_length=1)


class RefreshRequest(BaseModel):
    project_id: str | None = Field(default=None, min_length=1)


def _application(profile: str):
    try:
        return application_for_profile(profile)
    except ProfileResolutionError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def _operation(profile: str, method: str, **arguments):
    application = _application(profile)
    try:
        return getattr(application, method)(**arguments)
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
