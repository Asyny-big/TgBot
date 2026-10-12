"""Referral relationships and bonus balances, for the admin panel.

Read only, deliberately. The referral programme is configured through the
environment like the rest of the shop, so there is nothing here to PATCH: the
settings endpoint reports what the running process was started with, and the
listing answers "who invited whom, and what have they earned".

No funnels, no cohorts, no analytics. Two endpoints.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import ContainerDep, CurrentAdmin, PageDep
from app.api.schemas.common import PageMeta, PageResponse
from app.api.schemas.referrals import ReferralRecordResponse, ReferralSettingsResponse
from app.domain.pagination import ReferralFilters

router = APIRouter(prefix="/referrals", tags=["referrals"])


@router.get(
    "/settings",
    response_model=ReferralSettingsResponse,
    summary="Referral programme configuration",
)
async def read_settings(
    admin: CurrentAdmin,
    container: ContainerDep,
) -> ReferralSettingsResponse:
    """The numbers this process is running with.

    Changing them is a deploy, not a form: they are validated once on start-up,
    which is what keeps a bad value from surfacing in the middle of a checkout.
    """
    del admin
    return ReferralSettingsResponse.from_settings(container.settings.referral)


@router.get(
    "",
    response_model=PageResponse[ReferralRecordResponse],
    summary="List referral relationships",
)
async def list_referrals(
    admin: CurrentAdmin,
    container: ContainerDep,
    page: PageDep,
    search: Annotated[str | None, Query(max_length=255)] = None,
) -> PageResponse[ReferralRecordResponse]:
    """Relationships newest first. ``search`` matches either party."""
    del admin
    result = await container.referrals.search(ReferralFilters(search=search), page)
    return PageResponse[ReferralRecordResponse](
        items=[ReferralRecordResponse.from_domain(record) for record in result.items],
        meta=PageMeta(
            total=result.total,
            limit=result.limit,
            offset=result.offset,
            has_more=result.has_more,
        ),
    )
