"""Daily Content in the admin panel: overview, library, subscribers, deliveries,
content requests and alerts.

Every change is attributed to the panel's user — ``admin:<username>`` — and
goes through the same service, locks and rules as the bot. Nothing returned
here carries a file, a file id or a token: "send it to me" has the Daily bot
send the item to the owner's own Telegram instead.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import ContainerDep, CurrentAdmin, PageDep
from app.api.schemas.common import PageMeta, PageResponse
from app.api.schemas.daily import (
    DailyAlertResponse,
    DailyContentResponse,
    DailyDeliveryDetailResponse,
    DailyDeliveryResponse,
    DailyLibraryStatusRequest,
    DailyOutboxMessageResponse,
    DailyOverviewResponse,
    DailyReplyRequest,
    DailyReplyResponse,
    DailyReplyResultResponse,
    DailyRequestDetailResponse,
    DailyRequestPatchRequest,
    DailyRequestResponse,
    DailyRequestsBanRequest,
    DailySendToOwnerRequest,
    DailySendToOwnerResponse,
    DailySubscriberDetailResponse,
    DailySubscriberResponse,
)
from app.domain.daily_reports import (
    DailyDeliveryFilters,
    DailyLibraryFilters,
    DailyRequestFilters,
    DailySubscriberFilters,
)
from app.domain.enums import (
    DailyContentKind,
    DailyContentStatus,
    DailyDeliveryStatus,
    DailyRequestStatus,
    DailySubscriptionStatus,
)
from app.domain.pagination import Page
from app.services.daily_admin import DailyAdminService, SendResult

router = APIRouter(prefix="/daily", tags=["daily"])


def get_daily_admin(container: ContainerDep) -> DailyAdminService:
    """The Daily admin service of this process."""
    return container.build_daily_admin()


DailyAdminDep = Annotated[DailyAdminService, Depends(get_daily_admin)]


def _meta(page: Page[object]) -> PageMeta:
    return PageMeta(total=page.total, limit=page.limit, offset=page.offset, has_more=page.has_more)


def _sent(result: SendResult) -> DailySendToOwnerResponse:
    return DailySendToOwnerResponse(
        messages=[DailyOutboxMessageResponse.from_domain(message) for message in result.messages],
        created=result.created,
    )


# --------------------------------------------------------------------------- #
# Overview                                                                    #
# --------------------------------------------------------------------------- #


@router.get("/overview", response_model=DailyOverviewResponse, summary="Daily Content overview")
async def overview(admin: CurrentAdmin, daily: DailyAdminDep) -> DailyOverviewResponse:
    """Subscriptions, today's deliveries (Moscow date), requests, the outbox,
    the library and the alerts."""
    del admin
    return DailyOverviewResponse.from_domain(await daily.overview())


@router.get("/alerts", response_model=list[DailyAlertResponse], summary="The owner's alerts")
async def alerts(admin: CurrentAdmin, daily: DailyAdminDep) -> list[DailyAlertResponse]:
    """Every alert evaluated so far, firing or resolved."""
    del admin
    return [DailyAlertResponse.from_domain(alert) for alert in await daily.alerts()]


# --------------------------------------------------------------------------- #
# Library                                                                     #
# --------------------------------------------------------------------------- #


@router.get(
    "/library",
    response_model=PageResponse[DailyContentResponse],
    summary="List the library",
)
async def list_library(  # noqa: PLR0913 — one parameter per filter
    admin: CurrentAdmin,
    daily: DailyAdminDep,
    page: PageDep,
    *,
    status_filter: Annotated[DailyContentStatus | None, Query(alias="status")] = None,
    kind: Annotated[DailyContentKind | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
) -> PageResponse[DailyContentResponse]:
    """Items, newest first. ``search`` matches the number, the text or caption,
    or the file name."""
    del admin
    result = await daily.library_page(
        DailyLibraryFilters(status=status_filter, kind=kind, search=search),
        page,
    )
    return PageResponse[DailyContentResponse](
        items=[DailyContentResponse.from_entry(entry) for entry in result.items],
        meta=_meta(result),
    )


@router.get(
    "/library/{content_id}",
    response_model=DailyContentResponse,
    summary="Read a library item",
)
async def get_library_item(
    content_id: int,
    admin: CurrentAdmin,
    daily: DailyAdminDep,
) -> DailyContentResponse:
    """Raises: DailyContentNotFoundError."""
    del admin
    return DailyContentResponse.from_entry(await daily.library_entry(content_id))


@router.post(
    "/library/{content_id}/status",
    response_model=DailyContentResponse,
    summary="Disable, enable, archive or restore a library item",
)
async def change_library_status(
    content_id: int,
    payload: DailyLibraryStatusRequest,
    admin: CurrentAdmin,
    daily: DailyAdminDep,
) -> DailyContentResponse:
    """Exactly what the owner's buttons do, under the same lock; attributed
    ``admin:<username>:<action>``. Repeating an action changes nothing.

    Raises:
        DailyContentNotFoundError: no such item.
        DailyLibraryActionError: the item's status does not allow the action.
        DailyContentDuplicateError: restoring it would duplicate a live item.
    """
    entry = await daily.change_library_status(admin.username, content_id, payload.action.action)
    return DailyContentResponse.from_entry(entry)


@router.post(
    "/library/{content_id}/send-to-me",
    response_model=DailySendToOwnerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Have the Daily bot send a library item to the owner",
)
async def send_library_item(
    content_id: int,
    payload: DailySendToOwnerRequest,
    admin: CurrentAdmin,
    daily: DailyAdminDep,
) -> DailySendToOwnerResponse:
    """Queued for every owner account; the worker sends it as buyers get it.

    Raises:
        DailyNoOwnerError: no owner account is configured.
        DailyContentNotFoundError: no such item.
    """
    result = await daily.send_library_item(admin.username, content_id, payload.idempotency_key)
    return _sent(result)


# --------------------------------------------------------------------------- #
# Subscribers                                                                 #
# --------------------------------------------------------------------------- #


@router.get(
    "/subscribers",
    response_model=PageResponse[DailySubscriberResponse],
    summary="List subscribers",
)
async def list_subscribers(  # noqa: PLR0913 — one parameter per filter
    admin: CurrentAdmin,
    daily: DailyAdminDep,
    page: PageDep,
    *,
    status_filter: Annotated[DailySubscriptionStatus | None, Query(alias="status")] = None,
    blocked: Annotated[bool | None, Query()] = None,
    banned: Annotated[bool | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
) -> PageResponse[DailySubscriberResponse]:
    """Subscribers, latest first contact first. ``search``: Telegram id or username."""
    del admin
    result = await daily.subscribers(
        DailySubscriberFilters(status=status_filter, blocked=blocked, banned=banned, search=search),
        page,
    )
    return PageResponse[DailySubscriberResponse](
        items=[DailySubscriberResponse.from_entry(entry) for entry in result.items],
        meta=_meta(result),
    )


@router.get(
    "/subscribers/{user_id}",
    response_model=DailySubscriberDetailResponse,
    summary="Read a subscriber",
)
async def get_subscriber(
    user_id: int,
    admin: CurrentAdmin,
    daily: DailyAdminDep,
) -> DailySubscriberDetailResponse:
    """With the latest deliveries and requests. Raises: DailySubscriberNotFoundError."""
    del admin
    return DailySubscriberDetailResponse.from_domain(await daily.subscriber(user_id))


@router.put(
    "/subscribers/{user_id}/requests-ban",
    response_model=DailySubscriberDetailResponse,
    summary="Ban a buyer from content requests, or lift the ban",
)
async def set_requests_ban(
    user_id: int,
    payload: DailyRequestsBanRequest,
    admin: CurrentAdmin,
    daily: DailyAdminDep,
) -> DailySubscriberDetailResponse:
    """Idempotent. A ban stops the buyer's next step and every send.

    Raises:
        DailySubscriberNotFoundError: the buyer never wrote to the Daily bot.
    """
    detail = await daily.set_requests_ban(admin.username, user_id, banned=payload.banned)
    return DailySubscriberDetailResponse.from_domain(detail)


# --------------------------------------------------------------------------- #
# Deliveries                                                                  #
# --------------------------------------------------------------------------- #


@router.get(
    "/deliveries",
    response_model=PageResponse[DailyDeliveryResponse],
    summary="List deliveries",
)
async def list_deliveries(  # noqa: PLR0913 — one parameter per filter
    admin: CurrentAdmin,
    daily: DailyAdminDep,
    page: PageDep,
    *,
    status_filter: Annotated[DailyDeliveryStatus | None, Query(alias="status")] = None,
    user_id: Annotated[int | None, Query(ge=1)] = None,
    delivery_date: Annotated[date | None, Query(alias="date")] = None,
) -> PageResponse[DailyDeliveryResponse]:
    """Deliveries, latest date first; ``date`` is a Moscow date."""
    del admin
    result = await daily.deliveries(
        DailyDeliveryFilters(status=status_filter, user_id=user_id, delivery_date=delivery_date),
        page,
    )
    return PageResponse[DailyDeliveryResponse](
        items=[DailyDeliveryResponse.from_entry(entry) for entry in result.items],
        meta=_meta(result),
    )


@router.get(
    "/deliveries/{delivery_id}",
    response_model=DailyDeliveryDetailResponse,
    summary="Read a delivery",
)
async def get_delivery(
    delivery_id: int,
    admin: CurrentAdmin,
    daily: DailyAdminDep,
) -> DailyDeliveryDetailResponse:
    """With its attempts and its item. Raises: DailyDeliveryNotFoundError."""
    del admin
    return DailyDeliveryDetailResponse.from_domain(await daily.delivery(delivery_id))


# --------------------------------------------------------------------------- #
# Content requests                                                            #
# --------------------------------------------------------------------------- #


@router.get(
    "/requests",
    response_model=PageResponse[DailyRequestResponse],
    summary="List content requests",
)
async def list_requests(
    admin: CurrentAdmin,
    daily: DailyAdminDep,
    page: PageDep,
    status_filter: Annotated[DailyRequestStatus | None, Query(alias="status")] = None,
    user_id: Annotated[int | None, Query(ge=1)] = None,
) -> PageResponse[DailyRequestResponse]:
    """Requests, newest first."""
    del admin
    result = await daily.requests(
        DailyRequestFilters(status=status_filter, user_id=user_id),
        page,
    )
    return PageResponse[DailyRequestResponse](
        items=[DailyRequestResponse.from_entry(entry) for entry in result.items],
        meta=_meta(result),
    )


@router.get(
    "/requests/{request_id}",
    response_model=DailyRequestDetailResponse,
    summary="Read a content request",
)
async def get_request(
    request_id: int,
    admin: CurrentAdmin,
    daily: DailyAdminDep,
) -> DailyRequestDetailResponse:
    """Its messages, history and answers. Raises: DailyRequestNotFoundError."""
    del admin
    return DailyRequestDetailResponse.from_domain(await daily.request(request_id))


@router.patch(
    "/requests/{request_id}",
    response_model=DailyRequestDetailResponse,
    summary="Change a content request's status or note",
)
async def update_request(
    request_id: int,
    payload: DailyRequestPatchRequest,
    admin: CurrentAdmin,
    daily: DailyAdminDep,
) -> DailyRequestDetailResponse:
    """Applied only to the ``version`` it names.

    Raises:
        DailyRequestNotFoundError: no such request.
        DailyRequestVersionConflictError: 409 — it changed since; ``details``
            carry ``current_version``. Reload, then decide again.
    """
    detail = await daily.update_request(
        admin.username,
        request_id,
        version=payload.version,
        status=payload.status_change,
        admin_note=payload.note_change,
    )
    return DailyRequestDetailResponse.from_domain(detail)


@router.post(
    "/requests/{request_id}/replies",
    response_model=DailyReplyResultResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Answer the buyer through the Daily bot",
)
async def reply(
    request_id: int,
    payload: DailyReplyRequest,
    admin: CurrentAdmin,
    daily: DailyAdminDep,
    response: Response,
) -> DailyReplyResultResponse:
    """Queued in the outbox, once per ``idempotency_key``: a retry with the same
    key returns the same answer (``200``, ``created: false``).

    Raises:
        DailyRequestNotFoundError: no such request.
        DailyIdempotencyKeyReusedError: 409 — the key came with another text.
        DailyReplyTooLongError: the formatted answer exceeds one message.
    """
    result = await daily.reply(
        admin.username,
        request_id,
        text=payload.text,
        client_key=payload.idempotency_key,
    )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return DailyReplyResultResponse(
        reply=DailyReplyResponse.from_domain(result.reply),
        created=result.created,
    )


@router.post(
    "/requests/{request_id}/items/{item_id}/send-to-me",
    response_model=DailySendToOwnerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Have the Daily bot send a request's attachment to the owner",
)
async def send_request_item(
    request_id: int,
    item_id: int,
    payload: DailySendToOwnerRequest,
    admin: CurrentAdmin,
    daily: DailyAdminDep,
) -> DailySendToOwnerResponse:
    """Queued for every owner account.

    Raises:
        DailyNoOwnerError: no owner account is configured.
        DailyRequestNotFoundError: no such request, or no such message in it.
    """
    result = await daily.send_request_item(
        admin.username,
        request_id,
        item_id,
        payload.idempotency_key,
    )
    return _sent(result)


__all__ = ["DailyAdminDep", "get_daily_admin", "router"]
