"""Thin dashboard REST adapter for Map Governance."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from map_governance.runtime import (  # noqa: E402
    ProfileResolutionError,
    application_for_profile,
)


router = APIRouter()


def _application(profile: str):
    try:
        return application_for_profile(profile)
    except ProfileResolutionError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/health")
async def health(profile: str = Query(min_length=1)):
    return _application(profile).health()


@router.get("/board")
async def board(profile: str = Query(min_length=1)):
    return _application(profile).board()
