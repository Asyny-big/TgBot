"""The admin API's Daily Content endpoints, over HTTP, PostgreSQL and Redis.

Authentication, validation, the error envelope and the wiring are real; so
are the attributions (``admin:<username>``), the versions and the keys.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
import pytest

from app.core.config import DailySettings
from app.domain.commands import DailyContentDraft, DailyDeliveryDraft, DailyRequestDraft
from app.domain.daily import LibraryAction, window_for
from app.domain.enums import DailyContentKind, DailyContentStatus, DailyDeliveryTrigger
from app.services.daily_library import DailyLibraryService
from tests.api_harness import ADMIN_USERNAME, ApiHarness, api_settings, build_api_harness
from tests.concurrency import all_at_once, warm_pool
from tests.daily_bot_harness import seed_buyer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from redis.asyncio import Redis

    from app.infrastructure.cache.locks import RedisLockManager
    from app.infrastructure.db.engine import Database

BUYER = 9981
OWNER = 9980
NOW = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
ADMIN = f"admin:{ADMIN_USERNAME}"


@pytest.fixture
async def daily_api(
    live_database: Database,
    live_locks: RedisLockManager,
    redis_client: Redis,
    migrated_database: str,
) -> AsyncIterator[ApiHarness]:
    """A logged-in admin API with Daily Content on and one owner account."""
    settings = api_settings(
        migrated_database,
        daily=DailySettings(enabled=True, admin_telegram_ids=(OWNER,)),
    )
    harness, client = build_api_harness(
        settings=settings,
        database=live_database,
        locks=live_locks,
        redis=redis_client,
    )
    await harness.authenticate()
    try:
        yield harness
    finally:
        await client.aclose()
        await harness.bot.session.close()


def _url(api: ApiHarness, path: str) -> str:
    return f"{api.prefix}/daily{path}"


async def _get(api: ApiHarness, path: str, **params: Any) -> httpx.Response:
    return await api.client.get(_url(api, path), params=params)


def _error(response: httpx.Response) -> str:
    return str(response.json()["error"]["code"])


async def _subscriber(api: ApiHarness, user_id: int = BUYER, completed: int = 20) -> None:
    factory = api.container.uow_factory
    await seed_buyer(factory, user_id, completed=completed)
    async with factory() as uow:
        await uow.daily_subscriptions.record_first_interaction(user_id, at=NOW)
        await uow.daily_subscriptions.activate(user_id, at=NOW, anchor_minute=720)


async def _item(api: ApiHarness, number: int = 1) -> int:
    async with api.container.uow_factory() as uow:
        item, _ = await uow.daily_content.add(
            DailyContentDraft(
                kind=DailyContentKind.VIDEO,
                source_chat_id=OWNER,
                source_message_id=number,
                added_by=OWNER,
                body=f"video {number}",
                file_id=f"secret-file-id-{number}",
                file_unique_id=f"secret-unique-{number}",
                file_name=f"clip-{number}.mp4",
                duration=45,
            )
        )
    return item.id


async def _request(api: ApiHarness) -> tuple[int, list[int]]:
    """A buyer's request with a text and a photo; its id and its items' ids."""
    await _subscriber(api)
    async with api.container.uow_factory() as uow:
        request, _ = await uow.daily_requests.create(
            DailyRequestDraft(
                user_id=BUYER,
                client_draft_id=uuid4(),
                purchases_at_submit=20,
                items=(
                    DailyContentDraft(
                        kind=DailyContentKind.TEXT,
                        source_chat_id=BUYER,
                        source_message_id=1,
                        added_by=BUYER,
                        body="More yoga",
                    ),
                    DailyContentDraft(
                        kind=DailyContentKind.PHOTO,
                        source_chat_id=BUYER,
                        source_message_id=2,
                        added_by=BUYER,
                        file_id="secret-photo-id",
                        file_unique_id="secret-photo-unique",
                    ),
                ),
            ),
            at=NOW,
        )
        items = await uow.daily_requests.items_of(request.id)
    return request.id, [item.id for item in items]


# --------------------------------------------------------------------------- #
# Authentication                                                              #
# --------------------------------------------------------------------------- #


_ENDPOINTS = [
    ("GET", "/overview"),
    ("GET", "/alerts"),
    ("GET", "/library"),
    ("GET", "/library/1"),
    ("POST", "/library/1/status"),
    ("POST", "/library/1/send-to-me"),
    ("GET", "/subscribers"),
    ("GET", "/subscribers/1"),
    ("PUT", "/subscribers/1/requests-ban"),
    ("GET", "/deliveries"),
    ("GET", "/deliveries/1"),
    ("GET", "/requests"),
    ("GET", "/requests/1"),
    ("PATCH", "/requests/1"),
    ("POST", "/requests/1/replies"),
    ("POST", "/requests/1/items/1/send-to-me"),
]


@pytest.mark.parametrize(("method", "path"), _ENDPOINTS)
async def test_every_daily_endpoint_requires_an_admin(
    api: ApiHarness,
    method: str,
    path: str,
) -> None:
    response = await api.client.request(method, _url(api, path), json={})

    assert response.status_code == httpx.codes.UNAUTHORIZED
    assert _error(response) == "invalid_token"


# --------------------------------------------------------------------------- #
# Overview and library                                                        #
# --------------------------------------------------------------------------- #


async def test_the_overview_counts_everything_at_once(daily_api: ApiHarness) -> None:
    await _request(daily_api)
    await _item(daily_api)

    response = await _get(daily_api, "/overview")

    assert response.status_code == httpx.codes.OK
    body = response.json()
    assert body["enabled"] is True
    assert body["subscriptions"] == {
        "inactive": 0,
        "active": 1,
        "paused": 0,
        "blocked": 0,
        "banned": 0,
    }
    assert body["requests"] == {"new": 1, "in_review": 0, "completed": 0, "rejected": 0}
    assert body["library"]["active"] == 1
    assert body["library"]["active_by_kind"] == [{"kind": "video", "count": 1}]
    assert body["alerts"] == []


async def test_the_library_shows_what_an_item_is_never_its_file(daily_api: ApiHarness) -> None:
    item_id = await _item(daily_api)

    listing = await _get(daily_api, "/library", search="clip-1", status="active")
    one = await _get(daily_api, f"/library/{item_id}")
    missing = await _get(daily_api, f"/library/{item_id + 1000}")

    assert listing.status_code == httpx.codes.OK
    assert listing.json()["meta"] == {"total": 1, "limit": 20, "offset": 0, "has_more": False}
    (row,) = listing.json()["items"]
    assert (row["id"], row["kind"], row["file_name"], row["duration"]) == (
        item_id,
        "video",
        "clip-1.mp4",
        45,
    )
    assert row["sent_count"] == 0
    assert one.json() == row
    assert "secret" not in listing.text
    assert "file_id" not in json.dumps(row)
    assert missing.status_code == httpx.codes.NOT_FOUND
    assert _error(missing) == "daily_content_not_found"


async def test_an_admins_library_change_is_attributed_to_them(daily_api: ApiHarness) -> None:
    item_id = await _item(daily_api)
    url = _url(daily_api, f"/library/{item_id}/status")

    disabled = await daily_api.client.post(url, json={"action": "disable"})
    again = await daily_api.client.post(url, json={"action": "disable"})
    refused = await daily_api.client.post(url, json={"action": "restore"})
    unknown = await daily_api.client.post(url, json={"action": "undo"})

    assert disabled.status_code == httpx.codes.OK
    assert disabled.json()["status"] == "disabled"
    assert disabled.json()["status_reason"] == f"{ADMIN}:disable"
    assert again.status_code == httpx.codes.OK
    assert refused.status_code == httpx.codes.CONFLICT
    assert _error(refused) == "daily_library_action_not_allowed"
    assert unknown.status_code == httpx.codes.UNPROCESSABLE_ENTITY


async def test_send_to_me_is_queued_once_per_click(daily_api: ApiHarness) -> None:
    item_id = await _item(daily_api)
    url = _url(daily_api, f"/library/{item_id}/send-to-me")
    key = str(uuid4())

    first = await daily_api.client.post(url, json={"idempotency_key": key})
    again = await daily_api.client.post(url, json={"idempotency_key": key})
    malformed = await daily_api.client.post(url, json={"idempotency_key": "not-a-uuid"})

    assert first.status_code == again.status_code == httpx.codes.ACCEPTED
    assert (first.json()["created"], again.json()["created"]) == (True, False)
    (message,) = first.json()["messages"]
    assert (message["kind"], message["chat_id"], message["status"]) == (
        "library_preview",
        OWNER,
        "pending",
    )
    assert again.json()["messages"] == first.json()["messages"]
    assert malformed.status_code == httpx.codes.UNPROCESSABLE_ENTITY


async def test_send_to_me_without_an_owner_account_is_refused(
    live_database: Database,
    live_locks: RedisLockManager,
    redis_client: Redis,
    migrated_database: str,
) -> None:
    settings = api_settings(migrated_database, daily=DailySettings(enabled=True))
    harness, client = build_api_harness(
        settings=settings,
        database=live_database,
        locks=live_locks,
        redis=redis_client,
    )
    try:
        await harness.authenticate()
        item_id = await _item(harness)
        response = await client.post(
            _url(harness, f"/library/{item_id}/send-to-me"),
            json={"idempotency_key": str(uuid4())},
        )
    finally:
        await client.aclose()
        await harness.bot.session.close()

    assert response.status_code == httpx.codes.CONFLICT
    assert _error(response) == "daily_no_owner"


async def test_the_owner_in_telegram_and_an_admin_in_the_panel_act_at_once(
    daily_api: ApiHarness,
) -> None:
    item_id = await _item(daily_api)
    factory = daily_api.container.uow_factory
    async with factory() as uow:
        await uow.daily_content.set_status(item_id, DailyContentStatus.ARCHIVED, at=NOW)
    owner = DailyLibraryService(uow_factory=factory, settings=daily_api.settings.daily)
    await warm_pool(factory, 2)

    owner_result, response = await all_at_once(
        owner.change_status(OWNER, item_id, LibraryAction.RESTORE),
        daily_api.client.post(
            _url(daily_api, f"/library/{item_id}/status"),
            json={"action": "restore"},
        ),
    )

    assert response.status_code == httpx.codes.OK
    one = await _get(daily_api, f"/library/{item_id}")
    assert one.json()["status"] == "active"
    assert one.json()["status_reason"] in {"owner:restore", f"{ADMIN}:restore"}
    assert owner_result.item.status is DailyContentStatus.ACTIVE


# --------------------------------------------------------------------------- #
# Subscribers and deliveries                                                  #
# --------------------------------------------------------------------------- #


async def test_a_ban_is_set_by_the_admin_and_shows_in_the_listing(daily_api: ApiHarness) -> None:
    await _subscriber(daily_api)
    await _subscriber(daily_api, BUYER + 1, completed=15)
    url = _url(daily_api, f"/subscribers/{BUYER}/requests-ban")

    banned = await daily_api.client.put(url, json={"banned": True})
    listing = await _get(daily_api, "/subscribers", banned="true")
    lifted = await daily_api.client.put(url, json={"banned": False})
    nobody = await daily_api.client.put(
        _url(daily_api, "/subscribers/1/requests-ban"), json={"banned": True}
    )

    assert banned.status_code == httpx.codes.OK
    subscriber = banned.json()["subscriber"]
    assert subscriber["requests_banned_by"] == ADMIN
    assert subscriber["can_suggest"] is False
    assert [row["user_id"] for row in listing.json()["items"]] == [BUYER]
    assert lifted.json()["subscriber"]["requests_banned_at"] is None
    assert lifted.json()["subscriber"]["can_suggest"] is True
    assert nobody.status_code == httpx.codes.NOT_FOUND
    assert _error(nobody) == "daily_subscriber_not_found"


async def test_a_delivery_is_listed_and_read_with_its_attempts(daily_api: ApiHarness) -> None:
    await _subscriber(daily_api)
    item_id = await _item(daily_api)
    window = window_for(720, NOW.date(), recovery=timedelta(hours=12))
    async with daily_api.container.uow_factory() as uow:
        delivery = await uow.daily_deliveries.open(
            DailyDeliveryDraft(
                user_id=BUYER,
                trigger=DailyDeliveryTrigger.SCHEDULE,
                window=window,
                first_attempt_at=window.center,
            )
        )
        assert delivery is not None
        attempt = await uow.daily_deliveries.start_attempt(
            delivery.id,
            content_id=item_id,
            now=window.center,
            lease_until=window.center + timedelta(minutes=5),
        )
        await uow.daily_deliveries.finish_sent(
            attempt.lease_token, at=window.center, telegram_message_id=7
        )

    listing = await _get(daily_api, "/deliveries", date=str(NOW.date()), user_id=BUYER)
    one = await _get(daily_api, f"/deliveries/{delivery.id}")
    missing = await _get(daily_api, "/deliveries/999999")

    (row,) = listing.json()["items"]
    assert (row["id"], row["status"], row["content_id"]) == (delivery.id, "sent", item_id)
    detail = one.json()
    assert [entry["outcome"] for entry in detail["attempts"]] == ["sent"]
    assert detail["item"]["id"] == item_id
    assert missing.status_code == httpx.codes.NOT_FOUND
    assert _error(missing) == "daily_delivery_not_found"


# --------------------------------------------------------------------------- #
# Requests                                                                    #
# --------------------------------------------------------------------------- #


async def test_a_request_is_read_with_its_messages_but_no_files(daily_api: ApiHarness) -> None:
    request_id, _ = await _request(daily_api)

    listing = await _get(daily_api, "/requests", status="new")
    detail = await _get(daily_api, f"/requests/{request_id}")
    missing = await _get(daily_api, "/requests/999999")

    (row,) = listing.json()["items"]
    assert (row["id"], row["version"], row["item_count"], row["reply_count"]) == (
        request_id,
        1,
        2,
        0,
    )
    body = detail.json()
    assert [(item["kind"], item["has_file"]) for item in body["items"]] == [
        ("text", False),
        ("photo", True),
    ]
    assert [event["kind"] for event in body["events"]] == ["submitted"]
    assert "secret" not in detail.text
    assert missing.status_code == httpx.codes.NOT_FOUND
    assert _error(missing) == "daily_request_not_found"


async def test_a_change_from_a_stale_tab_is_refused_with_the_current_version(
    daily_api: ApiHarness,
) -> None:
    request_id, _ = await _request(daily_api)
    url = _url(daily_api, f"/requests/{request_id}")

    first = await daily_api.client.patch(
        url, json={"version": 1, "status": "in_review", "admin_note": "checking"}
    )
    stale = await daily_api.client.patch(url, json={"version": 1, "status": "rejected"})
    cleared = await daily_api.client.patch(url, json={"version": 2, "admin_note": None})
    null_status = await daily_api.client.patch(url, json={"version": 3, "status": None})
    no_version = await daily_api.client.patch(url, json={"status": "completed"})

    assert first.status_code == httpx.codes.OK
    changed = first.json()["request"]
    assert (changed["version"], changed["status"], changed["admin_note"]) == (
        2,
        "in_review",
        "checking",
    )
    assert [event["actor"] for event in first.json()["events"][1:]] == [ADMIN, ADMIN]
    assert stale.status_code == httpx.codes.CONFLICT
    assert _error(stale) == "daily_request_version_conflict"
    assert stale.json()["error"]["details"]["current_version"] == 2
    assert cleared.json()["request"]["admin_note"] is None
    assert cleared.json()["request"]["version"] == 3
    assert null_status.status_code == httpx.codes.UNPROCESSABLE_ENTITY
    assert no_version.status_code == httpx.codes.UNPROCESSABLE_ENTITY


async def test_two_tabs_saving_at_once_one_is_refused(daily_api: ApiHarness) -> None:
    request_id, _ = await _request(daily_api)
    await warm_pool(daily_api.container.uow_factory, 2)
    url = _url(daily_api, f"/requests/{request_id}")

    responses = await all_at_once(
        daily_api.client.patch(url, json={"version": 1, "status": "completed"}),
        daily_api.client.patch(url, json={"version": 1, "status": "rejected"}),
    )

    assert sorted(response.status_code for response in responses) == [200, 409]


async def test_an_answer_is_queued_once_per_key(daily_api: ApiHarness) -> None:
    request_id, _ = await _request(daily_api)
    url = _url(daily_api, f"/requests/{request_id}/replies")
    key = str(uuid4())

    first = await daily_api.client.post(
        url, json={"text": "Added, thanks!", "idempotency_key": key}
    )
    again = await daily_api.client.post(
        url, json={"text": "Added, thanks!", "idempotency_key": key}
    )
    reused = await daily_api.client.post(url, json={"text": "Other", "idempotency_key": key})
    too_long = await daily_api.client.post(
        url, json={"text": "x" * 3501, "idempotency_key": str(uuid4())}
    )
    detail = await _get(daily_api, f"/requests/{request_id}")

    assert first.status_code == httpx.codes.CREATED
    assert again.status_code == httpx.codes.OK
    assert (first.json()["created"], again.json()["created"]) == (True, False)
    reply = first.json()["reply"]
    assert reply["state"] == "queued"
    assert reply["event"]["actor"] == ADMIN
    assert reply["message"]["chat_id"] == BUYER
    assert again.json()["reply"]["event"]["id"] == reply["event"]["id"]
    assert reused.status_code == httpx.codes.CONFLICT
    assert _error(reused) == "idempotency_key_reused"
    assert too_long.status_code == httpx.codes.UNPROCESSABLE_ENTITY
    assert len(detail.json()["replies"]) == 1


async def test_an_attachment_is_sent_to_the_owner_on_request(daily_api: ApiHarness) -> None:
    request_id, (text_id, photo_id) = await _request(daily_api)

    sent = await daily_api.client.post(
        _url(daily_api, f"/requests/{request_id}/items/{photo_id}/send-to-me"),
        json={"idempotency_key": str(uuid4())},
    )
    foreign = await daily_api.client.post(
        _url(daily_api, f"/requests/{request_id + 1}/items/{text_id}/send-to-me"),
        json={"idempotency_key": str(uuid4())},
    )

    assert sent.status_code == httpx.codes.ACCEPTED
    (message,) = sent.json()["messages"]
    assert (message["kind"], message["chat_id"]) == ("request_attachment", OWNER)
    assert foreign.status_code == httpx.codes.NOT_FOUND
    assert _error(foreign) == "daily_request_not_found"
