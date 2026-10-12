"""A buyer suggesting content through the Daily bot, end to end.

PostgreSQL holds the requests, Redis holds the drafts (the production FSM
storage, under its own prefix) and the locks; only Telegram is a recording
double. Every check the stage promises is driven through real updates:
repeated and simultaneous presses, a retry after a failure, the draft's
expiry, the limits, a ban, and a buyer under the milestone.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import (
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Sticker,
    Update,
)
from sqlalchemy import text

from app.daily_bot import texts
from app.daily_bot.factory import DRAFT_STORAGE_PREFIX, create_draft_storage
from app.daily_bot.keyboards import DraftButton, DraftCallback
from app.domain.commands import DailyContentDraft, DailyRequestDraft
from app.domain.daily_requests import REQUEST_DRAFT_TTL_SECONDS
from app.domain.enums import DailyContentKind, DailyRequestEventKind, DailyRequestStatus
from app.domain.patch import UNSET
from tests.concurrency import all_at_once
from tests.daily_bot_harness import (
    Clock,
    DailyHarness,
    bot_message,
    build_daily_harness,
    buyer,
    callback_update,
    message_update,
    owner_message,
    photo_sizes,
    seed_buyer,
    update_of,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.infrastructure.cache.locks import RedisLockManager
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory

BUYER = 9951
OWNER = 9950
T0 = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
THANKS = "Спасибо! Ваше пожелание принято. Мы рассмотрим его при формировании следующих подборок."

Factory = Callable[..., DailyHarness]


@pytest.fixture
async def make_daily(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
    redis_client: Redis,
) -> AsyncIterator[Factory]:
    """The Daily bot with its drafts in the test Redis."""
    built: list[DailyHarness] = []

    def build(*, ttl_seconds: int = REQUEST_DRAFT_TTL_SECONDS, **settings: Any) -> DailyHarness:
        harness = build_daily_harness(
            live_uow_factory,
            live_locks,
            clock=Clock(T0),
            owner=OWNER,
            default_user=BUYER,
            storage=create_draft_storage(redis_client, ttl_seconds=ttl_seconds),
            **settings,
        )
        built.append(harness)
        return harness

    yield build
    for harness in built:
        await harness.close()


@pytest.fixture
def daily(make_daily: Factory) -> DailyHarness:
    return make_daily()


# --------------------------------------------------------------------------- #
# Seeding, pressing, reading                                                  #
# --------------------------------------------------------------------------- #


async def _buyer(daily: DailyHarness, user_id: int = BUYER, *, completed: int = 20) -> None:
    """A subscriber with ``completed`` completed purchases."""
    await seed_buyer(daily.uow_factory, user_id, completed=completed)
    async with daily.uow_factory() as uow:
        await uow.daily_subscriptions.record_first_interaction(user_id, at=T0)
        await uow.daily_subscriptions.activate(user_id, at=T0, anchor_minute=720)


async def _say(daily: DailyHarness, words: str, user_id: int = BUYER) -> None:
    await daily.feed(message_update(words, user=buyer(user_id)))


async def _photo(daily: DailyHarness, tag: str, **fields: Any) -> None:
    await daily.feed(update_of(owner_message(user=buyer(BUYER), photo=photo_sizes(tag), **fields)))


def _texts(daily: DailyHarness) -> list[str]:
    return daily.bot.texts_to(BUYER)


def _last_draft_keyboard(daily: DailyHarness) -> InlineKeyboardMarkup:
    for call in reversed(daily.bot.calls):
        markup = getattr(call, "reply_markup", None)
        if isinstance(call, SendMessage) and isinstance(markup, InlineKeyboardMarkup):
            return markup
    message = "No draft keyboard was sent"
    raise AssertionError(message)


def _press(daily: DailyHarness, button: DraftButton, draft: str | None = None) -> Update:
    """The draft's button as the buyer last saw it (or under another draft id)."""
    if draft is None:
        for row in _last_draft_keyboard(daily).inline_keyboard:
            for key in row:
                data = DraftCallback.unpack(str(key.callback_data))
                if data.button is button:
                    draft = data.draft
    assert draft is not None
    return callback_update(
        user=buyer(BUYER),
        data=DraftCallback(button=button, draft=draft).pack(),
        message=bot_message(chat_id=BUYER, text=texts.DRAFT_STARTED),
    )


def _answers(daily: DailyHarness) -> list[str | None]:
    return [call.text for call in daily.bot.calls if isinstance(call, AnswerCallbackQuery)]


def _menu_labels(call: Any) -> list[str]:
    markup = getattr(call, "reply_markup", None)
    assert isinstance(markup, ReplyKeyboardMarkup)
    return [button.text for row in markup.keyboard for button in row]


async def _requests(daily: DailyHarness) -> list[Any]:
    async with daily.uow_factory() as uow:
        rows = await uow.session.execute(
            text("SELECT id, user_id, status FROM daily_requests ORDER BY id")
        )
        return list(rows.tuples())


async def _items(daily: DailyHarness, request_id: int) -> list[Any]:
    async with daily.uow_factory() as uow:
        return list(await uow.daily_requests.items_of(request_id))


async def _draft_keys(redis: Redis) -> list[str]:
    return sorted(await redis.keys(f"{DRAFT_STORAGE_PREFIX}:*"))


async def _seed_request(
    daily: DailyHarness,
    *,
    at: datetime = T0,
    status: DailyRequestStatus | None = None,
) -> None:
    async with daily.uow_factory() as uow:
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
                        body="seeded",
                    ),
                ),
            ),
            at=at,
        )
        if status is not None:
            await uow.daily_requests.update(
                request.id, expected_version=1, at=at, status=status, admin_note=UNSET
            )


# --------------------------------------------------------------------------- #
# The way it is meant to go                                                   #
# --------------------------------------------------------------------------- #


async def test_the_suggest_button_is_offered_from_twenty_purchases(
    make_daily: Factory,
) -> None:
    daily = make_daily()
    await _buyer(daily, BUYER, completed=20)
    await _buyer(daily, BUYER + 1, completed=19)

    await _say(daily, "/status")
    await _say(daily, "/status", BUYER + 1)

    (twenty,) = [call for call in daily.bot.calls if getattr(call, "chat_id", None) == BUYER]
    (nineteen,) = [call for call in daily.bot.calls if getattr(call, "chat_id", None) == BUYER + 1]
    assert texts.SUGGEST_BUTTON in _menu_labels(twenty)
    assert texts.SUGGEST_BUTTON not in _menu_labels(nineteen)


async def test_a_buyer_suggests_content_and_the_draft_becomes_one_request(
    daily: DailyHarness,
    redis_client: Redis,
) -> None:
    await _buyer(daily)

    await _say(daily, texts.SUGGEST_BUTTON)
    assert _texts(daily) == [texts.DRAFT_STARTED]
    assert await _draft_keys(redis_client)  # the draft lives in Redis

    await _say(daily, "More stretching, please")
    await _photo(daily, "idea", caption="like this")
    assert _texts(daily)[1:] == [texts.draft_added(1), texts.draft_added(2)]

    await daily.feed(_press(daily, DraftButton.SEND))

    ((request_id, user_id, status),) = await _requests(daily)
    assert (user_id, status) == (BUYER, "new")
    assert _texts(daily)[-1] == THANKS  # the approved text, word for word
    assert texts.SUGGEST_BUTTON in _menu_labels(daily.bot.calls[-1])
    items = await _items(daily, request_id)
    assert [(item.kind, item.body) for item in items] == [
        (DailyContentKind.TEXT, "More stretching, please"),
        (DailyContentKind.PHOTO, "like this"),
    ]
    assert items[1].file_id == "idea-1280"  # the largest size
    assert await _draft_keys(redis_client) == []  # forgotten in the same step


async def test_the_draft_lives_in_redis_for_thirty_minutes(
    daily: DailyHarness,
    redis_client: Redis,
) -> None:
    await _buyer(daily)
    await _say(daily, texts.SUGGEST_BUTTON)
    await _say(daily, "idea")

    keys = await _draft_keys(redis_client)

    assert keys
    assert all(key.startswith(f"{DRAFT_STORAGE_PREFIX}:{BUYER}:{BUYER}:") for key in keys)
    for key in keys:
        assert 0 < await redis_client.ttl(key) <= REQUEST_DRAFT_TTL_SECONDS


async def test_an_expired_draft_cannot_be_sent_and_takes_no_more_messages(
    make_daily: Factory,
    redis_client: Redis,
) -> None:
    daily = make_daily(ttl_seconds=1)
    await _buyer(daily)
    await _say(daily, texts.SUGGEST_BUTTON)
    await _say(daily, "idea")
    send = _press(daily, DraftButton.SEND)

    await asyncio.sleep(1.5)
    assert await _draft_keys(redis_client) == []
    await daily.feed(send)
    await _say(daily, "another idea")

    assert _answers(daily)[-1] == texts.DRAFT_EXPIRED
    assert await _requests(daily) == []
    assert _texts(daily)[-1].startswith("📊")  # an ordinary message again: the status


# --------------------------------------------------------------------------- #
# Twice, at once, after a failure                                             #
# --------------------------------------------------------------------------- #


async def test_pressing_send_again_makes_no_second_request(daily: DailyHarness) -> None:
    await _buyer(daily)
    await _say(daily, texts.SUGGEST_BUTTON)
    await _say(daily, "idea")
    send = _press(daily, DraftButton.SEND)

    await daily.feed(send)
    await daily.feed(send)

    assert len(await _requests(daily)) == 1
    assert _answers(daily)[-1] == texts.REQUEST_ALREADY_SENT
    assert sum(1 for line in _texts(daily) if line.startswith(THANKS)) == 1


async def test_two_presses_at_once_make_one_request(daily: DailyHarness) -> None:
    await _buyer(daily)
    await _say(daily, texts.SUGGEST_BUTTON)
    await _say(daily, "idea")
    send = _press(daily, DraftButton.SEND)

    results = await all_at_once(daily.feed(send), daily.feed(send))

    assert not [result for result in results if isinstance(result, BaseException)]
    assert len(await _requests(daily)) == 1
    assert sum(1 for line in _texts(daily) if line.startswith(THANKS)) == 1
    assert set(_answers(daily)) <= {None, texts.REQUEST_ALREADY_SENT, texts.REQUEST_BUSY}


async def test_a_send_whose_answer_was_lost_is_not_sent_again(daily: DailyHarness) -> None:
    """The request was saved, then Telegram failed: the buyer presses again."""
    await _buyer(daily)
    await _say(daily, texts.SUGGEST_BUTTON)
    await _say(daily, "idea")
    send = _press(daily, DraftButton.SEND)
    daily.bot.failures[SendMessage] = [TelegramNetworkError(method=None, message="timeout")]  # type: ignore[arg-type]

    with pytest.raises(TelegramNetworkError):
        await daily.feed(send)
    await daily.feed(send)

    assert len(await _requests(daily)) == 1
    assert _answers(daily)[-1] == texts.REQUEST_ALREADY_SENT


async def test_a_send_that_reached_the_database_but_not_the_draft_ends_the_draft(
    daily: DailyHarness,
    redis_client: Redis,
) -> None:
    """The process died between saving the request and forgetting the draft."""
    await _buyer(daily)
    await _say(daily, texts.SUGGEST_BUTTON)
    await _say(daily, "idea")
    send = _press(daily, DraftButton.SEND)
    draft = UUID(hex=DraftCallback.unpack(str(send.callback_query.data)).draft)  # type: ignore[union-attr]
    first = await daily.services.requests.submit(
        BUYER,
        client_draft_id=draft,
        items=(
            DailyContentDraft(
                kind=DailyContentKind.TEXT,
                source_chat_id=BUYER,
                source_message_id=1,
                added_by=BUYER,
                body="idea",
            ),
        ),
    )
    assert first.request is not None
    assert await _draft_keys(redis_client)  # the draft is still there

    await daily.feed(send)

    assert [row[0] for row in await _requests(daily)] == [first.request.id]
    assert _texts(daily)[-1] == THANKS
    assert await _draft_keys(redis_client) == []


async def test_a_button_of_another_draft_sends_nothing(daily: DailyHarness) -> None:
    await _buyer(daily)
    await _say(daily, texts.SUGGEST_BUTTON)
    await _say(daily, "idea")

    await daily.feed(_press(daily, DraftButton.SEND, draft=uuid4().hex))
    await daily.feed(_press(daily, DraftButton.SEND, draft="not-a-draft"))

    assert await _requests(daily) == []
    assert _answers(daily) == [texts.DRAFT_EXPIRED, texts.DRAFT_EXPIRED]


# --------------------------------------------------------------------------- #
# The draft's limits                                                          #
# --------------------------------------------------------------------------- #


async def test_a_draft_takes_ten_messages_of_up_to_2000_characters(daily: DailyHarness) -> None:
    await _buyer(daily)
    await _say(daily, texts.SUGGEST_BUTTON)

    await _say(daily, "x" * 2001)
    for number in range(10):
        await _say(daily, f"idea {number}")
    await _say(daily, "one too many")
    await daily.feed(
        update_of(
            owner_message(
                user=buyer(BUYER),
                sticker=Sticker(
                    file_id="sticker",
                    file_unique_id="sticker-unique",
                    type="regular",
                    width=512,
                    height=512,
                    is_animated=False,
                    is_video=False,
                ),
            )
        )
    )

    replies = _texts(daily)[1:]
    assert replies[0] == texts.DRAFT_TEXT_TOO_LONG
    assert replies[1:11] == [texts.draft_added(count) for count in range(1, 11)]
    assert replies[11] == texts.DRAFT_FULL
    assert replies[12].startswith("⛔ Не поддерживается: стикер")

    await daily.feed(_press(daily, DraftButton.SEND))
    ((request_id, _, _),) = await _requests(daily)
    assert len(await _items(daily, request_id)) == 10


async def test_commands_and_the_menu_keep_working_while_drafting(daily: DailyHarness) -> None:
    await _buyer(daily)
    await _say(daily, texts.SUGGEST_BUTTON)

    await _say(daily, texts.STATUS_BUTTON)
    await _say(daily, "/help")
    await _say(daily, texts.SUGGEST_BUTTON)
    await _say(daily, "idea")

    replies = _texts(daily)
    assert replies[1].startswith("📊")
    assert replies[2] == texts.HELP
    assert replies[3] == texts.draft_exists(0)
    assert replies[4] == texts.draft_added(1)  # the status and /help were not taken


async def test_cancel_forgets_the_draft(daily: DailyHarness, redis_client: Redis) -> None:
    await _buyer(daily)
    await _say(daily, texts.SUGGEST_BUTTON)
    await _say(daily, "idea")

    await daily.feed(_press(daily, DraftButton.CANCEL))
    assert await _draft_keys(redis_client) == []
    await _say(daily, texts.SUGGEST_BUTTON)
    await _say(daily, "/cancel")

    assert _texts(daily).count(texts.DRAFT_CANCELLED) == 2
    assert await _requests(daily) == []


@pytest.mark.parametrize(
    "throttle_seconds",
    [1.5, 0.0],
    ids=["throttled-messages-queue", "unthrottled-the-draft-lock-holds"],
)
async def test_an_album_is_answered_once_and_kept_in_order(
    make_daily: Factory,
    throttle_seconds: float,
) -> None:
    daily = make_daily(throttle_seconds=throttle_seconds)  # never turned away, never lost
    await _buyer(daily)
    await _say(daily, texts.SUGGEST_BUTTON)
    base = 10_000_000
    album = [
        update_of(
            owner_message(
                user=buyer(BUYER),
                message_id=base + number,
                media_group_id="album-1",
                photo=photo_sizes(f"album-{number}"),
            )
        )
        for number in (3, 1, 2)
    ]

    await all_at_once(*(daily.feed(update) for update in album))
    await daily.settle()

    assert _texts(daily)[1:] == [
        "📦 Из альбома добавлено: 3 · в черновике 3/10",
    ]
    await daily.feed(_press(daily, DraftButton.SEND))
    ((request_id, _, _),) = await _requests(daily)
    assert [item.source_message_id for item in await _items(daily, request_id)] == [
        base + 1,
        base + 2,
        base + 3,
    ]


# --------------------------------------------------------------------------- #
# Who may suggest                                                             #
# --------------------------------------------------------------------------- #


async def test_under_twenty_purchases_the_buyer_is_told_how_far(
    daily: DailyHarness,
    redis_client: Redis,
) -> None:
    await _buyer(daily, completed=19)

    await _say(daily, texts.SUGGEST_BUTTON)

    assert _texts(daily) == ["Предлагать контент можно с 20-й покупки. Сейчас у вас 19 из 20."]
    assert await _draft_keys(redis_client) == []


async def test_a_ban_stops_a_draft_at_its_next_step(daily: DailyHarness) -> None:
    await _buyer(daily)
    await _say(daily, texts.SUGGEST_BUTTON)
    await _say(daily, "idea")
    send = _press(daily, DraftButton.SEND)
    async with daily.uow_factory() as uow:
        await uow.daily_subscriptions.set_requests_ban(BUYER, banned_by="admin:root", at=T0)

    await _say(daily, "another idea")
    await daily.feed(send)
    await _say(daily, texts.SUGGEST_BUTTON)

    replies = _texts(daily)
    assert replies[-2] == texts.REQUEST_BANNED
    assert replies[-1] == texts.REQUEST_BANNED
    assert texts.SUGGEST_BUTTON not in _menu_labels(daily.bot.calls[-1])
    assert _answers(daily) == [texts.DRAFT_EXPIRED]  # the draft ended with the ban
    assert await _requests(daily) == []


async def test_three_open_requests_wait_before_a_new_draft(daily: DailyHarness) -> None:
    await _buyer(daily)
    for _ in range(3):
        await _seed_request(daily)

    await _say(daily, texts.SUGGEST_BUTTON)

    assert _texts(daily) == [texts.REQUEST_TOO_MANY_OPEN]
    assert texts.SUGGEST_BUTTON in _menu_labels(daily.bot.calls[-1])  # a limit, not a ban


async def test_five_requests_a_day_are_the_most(daily: DailyHarness) -> None:
    await _buyer(daily)
    for hours in range(5):
        await _seed_request(
            daily,
            at=T0 - timedelta(hours=hours),
            status=DailyRequestStatus.COMPLETED,
        )

    await _say(daily, texts.SUGGEST_BUTTON)

    assert _texts(daily) == [texts.REQUEST_RATE_LIMITED]


async def test_the_owner_never_drafts(daily: DailyHarness, redis_client: Redis) -> None:
    await seed_buyer(daily.uow_factory, OWNER, completed=30)

    await daily.feed(message_update(texts.SUGGEST_BUTTON, user=buyer(OWNER)))

    # The owner's text is library content, offered to the library as always.
    assert daily.bot.texts_to(OWNER) == [texts.TEXT_PROMPT]
    assert await _draft_keys(redis_client) == []


async def test_a_draft_is_only_ever_opened_in_a_private_chat(
    daily: DailyHarness,
    redis_client: Redis,
) -> None:
    await _buyer(daily)

    await daily.feed(
        message_update(texts.SUGGEST_BUTTON, user=buyer(BUYER), chat_type="group", chat_id=-100)
    )

    assert daily.bot.calls == []
    assert await _draft_keys(redis_client) == []


async def test_the_request_history_starts_with_the_buyer(daily: DailyHarness) -> None:
    await _buyer(daily)
    await _say(daily, texts.SUGGEST_BUTTON)
    await _say(daily, "idea")
    await daily.feed(_press(daily, DraftButton.SEND))
    ((request_id, _, _),) = await _requests(daily)

    async with daily.uow_factory() as uow:
        events = await uow.daily_requests.events_of(request_id)
        request = await uow.daily_requests.get(request_id)

    assert [(event.kind, event.actor) for event in events] == [
        (DailyRequestEventKind.SUBMITTED, f"user:{BUYER}"),
    ]
    assert request is not None
    assert request.purchases_at_submit == 20
