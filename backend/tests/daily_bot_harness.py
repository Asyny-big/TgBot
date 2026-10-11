"""Test doubles for the Daily Content bot: a recording bot, update builders,
and the whole bot assembled over whatever stores a test hands it.

Kept apart from ``bot_harness`` (the shop bot's) so neither grows the other's
concerns. Nothing here talks to Telegram.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final
from uuid import uuid4

from aiogram import Bot
from aiogram.methods import (
    AnswerCallbackQuery,
    CopyMessage,
    EditMessageText,
    GetMe,
    SendAnimation,
    SendAudio,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendVideo,
    SendVideoNote,
    SendVoice,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    MessageId,
    PhotoSize,
    Update,
    User,
    Video,
)

from app.core.config import DailySettings
from app.daily_bot.albums import AlbumCollector
from app.daily_bot.factory import create_daily_dispatcher
from app.daily_bot.middlewares import DailyServices
from app.domain.commands import ProductDraft, PurchaseDraft, UserDraft
from app.domain.enums import Currency, PaymentProvider, PurchaseStatus
from app.infrastructure.telegram.daily import TelegramDailyContentSender
from app.services.daily_delivery import DailyDeliveryService
from app.services.daily_library import DailyLibraryService
from app.services.daily_stock import DailyStockService
from app.services.daily_subscriptions import DailySubscriptionService

if TYPE_CHECKING:
    from aiogram import Dispatcher
    from aiogram.methods import TelegramMethod

    from app.domain.locks import LockManager
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory

DAILY_BOT_TOKEN: Final = "987654321:AAHfake-Daily-Token_for_unit_tests_01"  # noqa: S105
DAILY_BOT_USER: Final = User(id=987654321, is_bot=True, first_name="Daily", username="VipDailyBot")

ITEM_METHODS: Final = (
    SendPhoto,
    SendVideo,
    SendAnimation,
    SendDocument,
    SendAudio,
    SendVoice,
    SendVideoNote,
    CopyMessage,
)
"""The calls that put a library item in front of a buyer."""

_update_ids = itertools.count(1)
_message_ids = itertools.count(1)


def next_update_id() -> int:
    return next(_update_ids)


class DailyRecordingBot(Bot):
    """A Bot whose API calls are recorded instead of sent.

    ``failures`` maps a method type to the errors its next calls raise, in order.
    """

    def __init__(self) -> None:
        super().__init__(token=DAILY_BOT_TOKEN)
        self.calls: list[TelegramMethod[Any]] = []
        self.attempted: list[TelegramMethod[Any]] = []
        """Every call that reached Telegram, failed ones included."""

        self.failures: dict[type[TelegramMethod[Any]], list[Exception]] = {}

    async def __call__(
        self,
        method: TelegramMethod[Any],
        request_timeout: int | None = None,
    ) -> Any:
        del request_timeout
        self.attempted.append(method)
        queued = self.failures.get(type(method))
        if queued:
            raise queued.pop(0)
        self.calls.append(method)
        if isinstance(method, GetMe):
            return DAILY_BOT_USER
        if isinstance(method, CopyMessage):
            return MessageId(message_id=next(_message_ids))
        if isinstance(method, (SendMessage, *ITEM_METHODS)):
            chat_id = int(getattr(method, "chat_id", 0))
            return Message(
                message_id=next(_message_ids),
                date=datetime.now(UTC),
                chat=Chat(id=chat_id, type="private"),
            )
        return True

    # -- assertion helpers --------------------------------------------------- #
    def texts_to(self, chat_id: int) -> list[str]:
        """Every text message sent to one chat, in order."""
        return [
            str(call.text)
            for call in self.calls
            if isinstance(call, SendMessage) and call.chat_id == chat_id
        ]

    def items_to(self, chat_id: int) -> list[TelegramMethod[Any]]:
        """Every library item sent to one chat, in order."""
        return [
            call
            for call in self.calls
            if isinstance(call, ITEM_METHODS) and getattr(call, "chat_id", None) == chat_id
        ]

    def items(self) -> list[TelegramMethod[Any]]:
        """Every library item sent, to anybody, in order."""
        return [call for call in self.calls if isinstance(call, ITEM_METHODS)]

    def sent_to(self, chat_id: int) -> list[TelegramMethod[Any]]:
        """Every text or item sent to one chat, in order."""
        return [
            call
            for call in self.calls
            if isinstance(call, (SendMessage, *ITEM_METHODS))
            and getattr(call, "chat_id", None) == chat_id
        ]

    def answers(self) -> list[AnswerCallbackQuery]:
        """Every answer to a button press, in order."""
        return [call for call in self.calls if isinstance(call, AnswerCallbackQuery)]

    def edits(self) -> list[EditMessageText]:
        """Every message text the bot replaced, in order."""
        return [call for call in self.calls if isinstance(call, EditMessageText)]


def buyer(telegram_id: int, username: str | None = "buyer") -> User:
    return User(id=telegram_id, is_bot=False, first_name="Buyer", username=username)


def next_message_id() -> int:
    return next(_message_ids)


def owner_message(
    *,
    user: User,
    chat_type: str = "private",
    chat_id: int | None = None,
    message_id: int | None = None,
    **fields: Any,
) -> Message:
    """A message from ``user`` carrying ``fields`` — ``photo=``, ``video=``, ``text=``..."""
    chat = Chat(
        id=chat_id if chat_id is not None else user.id,
        type=chat_type,
        title=None if chat_type == "private" else "Some group",
    )
    return Message(
        message_id=message_id if message_id is not None else next_message_id(),
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        **fields,
    )


def update_of(message: Message) -> Update:
    """A fresh update delivering ``message`` (the same message twice = a replay)."""
    return Update(update_id=next_update_id(), message=message)


def photo_sizes(tag: str) -> list[PhotoSize]:
    """Telegram's sizes of one photo, smallest first: what ``message.photo`` holds."""
    return [
        PhotoSize(
            file_id=f"{tag}-{width}",
            file_unique_id=f"{tag}-unique-{width}",
            width=width,
            height=height,
            file_size=size,
        )
        for width, height, size in ((90, 60, 1_200), (320, 213, 9_800), (1280, 853, 98_000))
    ]


def video(tag: str, *, duration: int = 45) -> Video:
    return Video(
        file_id=f"{tag}-file",
        file_unique_id=f"{tag}-unique",
        width=1280,
        height=720,
        duration=duration,
        file_size=4_200_000,
        mime_type="video/mp4",
        file_name=f"{tag}.mp4",
    )


def bot_message(*, chat_id: int, text: str, reply_to: Message | None = None) -> Message:
    """A message the Daily bot sent — what an owner's button sits under."""
    return Message(
        message_id=next_message_id(),
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=DAILY_BOT_USER,
        text=text,
        reply_to_message=reply_to,
    )


def callback_update(*, user: User, data: str, message: Message | None) -> Update:
    """``user`` pressing a button whose payload is ``data`` under ``message``."""
    return Update(
        update_id=next_update_id(),
        callback_query=CallbackQuery(
            id=str(next_update_id()),
            from_user=user,
            chat_instance="daily-chat",
            data=data,
            message=message,
        ),
    )


# --------------------------------------------------------------------------- #
# Seeding a buyer                                                             #
# --------------------------------------------------------------------------- #


async def completed_purchase(uow: Any, user_id: int) -> None:
    """One delivered pack for ``user_id``: one more completed purchase."""
    product = await uow.products.create(
        ProductDraft(
            slug=f"d{uuid4().hex[:10]}",
            title="Pack",
            description="",
            delivery_url="https://t.me/+pack",
            price_stars=100,
        )
    )
    paid = await uow.purchases.create(
        PurchaseDraft(
            user_id=user_id,
            product_id=product.id,
            provider=PaymentProvider.STARS,
            amount=Decimal(100),
            currency=Currency.XTR,
            external_id=uuid4().hex,
            status=PurchaseStatus.PAID,
        )
    )
    await uow.purchases.mark_delivered(paid.id, delivered_url="https://t.me/+pack")


async def seed_buyer(uow_factory: Any, user_id: int, *, completed: int = 15) -> None:
    """A shop user with ``completed`` completed purchases."""
    async with uow_factory() as uow:
        await uow.users.upsert(UserDraft(telegram_id=user_id))
        for _ in range(completed):
            await completed_purchase(uow, user_id)


# --------------------------------------------------------------------------- #
# The whole bot                                                               #
# --------------------------------------------------------------------------- #


@dataclass
class Clock:
    """A clock a test moves by hand; shared by every service of one harness."""

    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@dataclass
class DailyHarness:
    """The Daily bot over real services; only Telegram is a recording double."""

    bot: DailyRecordingBot
    dispatcher: Dispatcher
    services: DailyServices
    uow_factory: SqlAlchemyUnitOfWorkFactory
    clock: Clock
    settings: DailySettings = field(repr=False)
    default_user: int = 0

    async def feed(self, update: Update) -> None:
        await self.dispatcher.feed_update(self.bot, update)

    async def send(self, text: str, user_id: int | None = None) -> None:
        user = buyer(user_id if user_id is not None else self.default_user)
        await self.feed(message_update(text, user=user))

    async def settle(self) -> None:
        """Wait until every album summary still due has gone out."""
        await self.services.albums.drain()

    async def close(self) -> None:
        await self.settle()
        await self.bot.session.close()


def build_daily_harness(  # noqa: PLR0913 — every knob a Daily bot test turns
    uow_factory: SqlAlchemyUnitOfWorkFactory,
    locks: LockManager,
    *,
    clock: Clock,
    owner: int,
    default_user: int,
    throttle_seconds: float = 0.0,
    subscriptions_uow: Any | None = None,
    library_uow: Any | None = None,
    **settings: Any,
) -> DailyHarness:
    """The Daily bot over ``uow_factory``; ``settings`` override ``DAILY_*``.

    ``subscriptions_uow`` and ``library_uow`` replace the store of one service
    only — how a test holds that service at a gate.
    """
    values: dict[str, Any] = {
        "enabled": True,
        "admin_telegram_ids": (owner,),
        "lease_seconds": 300,
        "recovery_hours": 12,
        "max_attempts": 5,
        "use_webhook": False,
    }
    values.update(settings)
    daily_settings = DailySettings(**values)
    bot = DailyRecordingBot()
    sender = TelegramDailyContentSender(bot)
    services = DailyServices(
        subscriptions=DailySubscriptionService(
            uow_factory=subscriptions_uow or uow_factory,
            settings=daily_settings,
            clock=clock,
        ),
        delivery=DailyDeliveryService(
            uow_factory=uow_factory,
            sender=sender,
            settings=daily_settings,
            clock=clock,
            jitter=lambda: 0.0,
        ),
        library=DailyLibraryService(
            uow_factory=library_uow or uow_factory,
            settings=daily_settings,
            sender=sender,
            clock=clock,
        ),
        albums=AlbumCollector(settle_seconds=0.05),
        stock=DailyStockService(uow_factory=uow_factory, settings=daily_settings, clock=clock),
    )
    return DailyHarness(
        bot=bot,
        dispatcher=create_daily_dispatcher(
            services, locks=locks, throttle_seconds=throttle_seconds
        ),
        services=services,
        uow_factory=uow_factory,
        clock=clock,
        settings=daily_settings,
        default_user=default_user,
    )


def message_update(
    text: str,
    *,
    user: User,
    chat_type: str = "private",
    chat_id: int | None = None,
) -> Update:
    """A text message from ``user``; a private chat unless said otherwise."""
    chat = Chat(id=chat_id if chat_id is not None else user.id, type=chat_type, title=None)
    return Update(
        update_id=next_update_id(),
        message=Message(
            message_id=next(_message_ids),
            date=datetime.now(UTC),
            chat=chat,
            from_user=user,
            text=text,
        ),
    )


def membership_update(
    *,
    user: User,
    status: str,
    chat_type: str = "private",
    chat_id: int | None = None,
) -> Update:
    """Telegram telling the bot its own membership changed (blocked, unblocked, added)."""
    new_member: ChatMemberBanned | ChatMemberMember | ChatMemberLeft
    if status == "kicked":
        new_member = ChatMemberBanned(user=DAILY_BOT_USER, until_date=datetime.now(UTC))
    elif status == "left":
        new_member = ChatMemberLeft(user=DAILY_BOT_USER)
    else:
        new_member = ChatMemberMember(user=DAILY_BOT_USER)
    old_member = ChatMemberLeft(user=DAILY_BOT_USER)
    chat = Chat(
        id=chat_id if chat_id is not None else user.id,
        type=chat_type,
        title=None if chat_type == "private" else "Some group",
    )
    return Update(
        update_id=next_update_id(),
        my_chat_member=ChatMemberUpdated(
            chat=chat,
            from_user=user,
            date=datetime.now(UTC),
            old_chat_member=old_member,
            new_chat_member=new_member,
        ),
    )


__all__ = [
    "DAILY_BOT_TOKEN",
    "DAILY_BOT_USER",
    "ITEM_METHODS",
    "Clock",
    "DailyHarness",
    "DailyRecordingBot",
    "bot_message",
    "build_daily_harness",
    "buyer",
    "callback_update",
    "completed_purchase",
    "membership_update",
    "message_update",
    "next_message_id",
    "next_update_id",
    "owner_message",
    "photo_sizes",
    "seed_buyer",
    "update_of",
    "video",
]
