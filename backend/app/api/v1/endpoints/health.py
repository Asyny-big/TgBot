"""Liveness and readiness probes."""

from __future__ import annotations

from http import HTTPStatus
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel

from app import __version__
from app.api.deps import ResourcesDep

router = APIRouter(tags=["health"])

ComponentStatus = Literal["ok", "fail"]


class LivenessResponse(BaseModel):
    """Answered as long as the process can serve HTTP."""

    status: Literal["alive"]
    version: str


class ReadinessResponse(BaseModel):
    """Aggregated availability of every infrastructure dependency."""

    status: Literal["ready", "degraded"]
    version: str
    environment: str
    components: dict[str, ComponentStatus]


@router.get("/health/live", response_model=LivenessResponse, summary="Liveness probe")
async def liveness() -> LivenessResponse:
    """Report that the process is running."""
    return LivenessResponse(status="alive", version=__version__)


@router.get("/health/ready", response_model=ReadinessResponse, summary="Readiness probe")
async def readiness(resources: ResourcesDep, response: Response) -> ReadinessResponse:
    """Probe PostgreSQL and Redis, answering 503 when any of them is down."""
    checks = await resources.check()
    components: dict[str, ComponentStatus] = {
        name: ("ok" if healthy else "fail") for name, healthy in checks.items()
    }
    healthy = all(checks.values())
    if not healthy:
        response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ready" if healthy else "degraded",
        version=__version__,
        environment=resources.settings.app.environment.value,
        components=components,
    )
