"""Dashboard statistics."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import ContainerDep, CurrentAdmin
from app.api.schemas.stats import OverviewResponse

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/overview", response_model=OverviewResponse, summary="Dashboard overview")
async def overview(admin: CurrentAdmin, container: ContainerDep) -> OverviewResponse:
    """Today / week / month / all-time revenue, top products and last sales."""
    del admin
    return OverviewResponse.from_domain(await container.stats.overview())
