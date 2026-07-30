"""Purchase search, manual payment verification and re-delivery."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.deps import CheckoutDep, ContainerDep, CurrentAdmin, PageDep
from app.api.schemas.common import PageMeta, PageResponse
from app.api.schemas.purchases import (
    DeliveryAttemptResponse,
    PurchaseRecordResponse,
    VerificationResponse,
)
from app.domain.enums import PurchaseStatus
from app.domain.pagination import PurchaseFilters

router = APIRouter(prefix="/purchases", tags=["purchases"])


@router.get(
    "",
    response_model=PageResponse[PurchaseRecordResponse],
    summary="Search purchases",
)
async def search_purchases(
    admin: CurrentAdmin,
    container: ContainerDep,
    page: PageDep,
    search: Annotated[str | None, Query(max_length=255)] = None,
    status_filter: Annotated[list[PurchaseStatus] | None, Query(alias="status")] = None,
) -> PageResponse[PurchaseRecordResponse]:
    """Search by Telegram id, username, product title or slug, invoice or charge id."""
    del admin
    statuses = tuple(item.value for item in status_filter or ())
    result = await container.stats.search_purchases(
        PurchaseFilters(search=search, statuses=statuses),
        page,
    )
    return PageResponse[PurchaseRecordResponse](
        items=[PurchaseRecordResponse.from_domain(record) for record in result.items],
        meta=PageMeta(
            total=result.total,
            limit=result.limit,
            offset=result.offset,
            has_more=result.has_more,
        ),
    )


@router.post(
    "/{purchase_id}/verify",
    response_model=VerificationResponse,
    summary="Check a payment manually and finish it if needed",
)
async def verify_payment(
    purchase_id: UUID,
    admin: CurrentAdmin,
    checkout: CheckoutDep,
) -> VerificationResponse:
    """Re-check one purchase against the payment provider.

    For the support case "I paid but got no link". Idempotent:

    * already delivered → reports that, sends nothing;
    * paid but undelivered → retries delivery;
    * pending → asks CryptoBot for the invoice state, or uses the stored Telegram
      charge id, and settles the payment when it is confirmed;
    * unpaid, expired or refunded → reported as-is, never "fixed".

    Raises:
        PurchaseNotFoundError: no purchase with this id.
        LockBusyError: a payment or delivery for it is already being processed.
    """
    del admin
    report = await checkout.verify_payment(purchase_id)
    return VerificationResponse.from_domain(report)


@router.post(
    "/{purchase_id}/resend",
    response_model=DeliveryAttemptResponse,
    summary="Send the purchased link again",
)
async def resend_delivery(
    purchase_id: UUID,
    admin: CurrentAdmin,
    checkout: CheckoutDep,
) -> DeliveryAttemptResponse:
    """Re-send the link of an already paid purchase.

    Raises:
        PurchaseNotFoundError: no purchase with this id.
        ConflictError: the purchase is not paid.
        LockBusyError: a delivery for it is already running.
    """
    del admin
    result = await checkout.redeliver(purchase_id)
    return DeliveryAttemptResponse(
        status=result.status,
        attempts=result.attempts,
        error=result.error,
    )
