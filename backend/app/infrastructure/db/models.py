"""SQLAlchemy models.

Database-level invariants are enforced here rather than only in Python: money
moves through this schema, so "one paid copy per user" and "an invoice id is
recorded once" are constraints, not conventions.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.daily import MINUTES_PER_DAY
from app.domain.enums import (
    BonusTransactionType,
    Currency,
    DailyAlertState,
    DailyAttemptOutcome,
    DailyContentKind,
    DailyContentStatus,
    DailyDeliveryStatus,
    DailyDeliveryTrigger,
    DailyOutboxStatus,
    DailyRequestStatus,
    DailySubscriptionStatus,
    LoyaltyRewardKind,
    LoyaltyRewardStatus,
    PaymentProvider,
    PurchaseStatus,
)
from app.domain.loyalty import DISCOUNT_MILESTONE, FREE_PACK_MILESTONE
from app.domain.referrals import REFERRAL_CODE_MAX_LENGTH
from app.domain.slug import SLUG_MAX_LENGTH, SLUG_PATTERN
from app.infrastructure.db.base import Base, TimestampMixin

USDT_PRECISION: Final = 12
USDT_SCALE: Final = 2

STARS_RATE_PRECISION: Final = 18
STARS_RATE_SCALE: Final = 6
"""Width of the recorded Stars-per-USDT rate. Wide enough to never round it."""

_ACCESS_STATUS_SQL: Final = "status IN ('paid', 'delivered')"
_PENDING_CRYPTO_SQL: Final = "status = 'pending' AND provider = 'crypto'"

# ``provider::text`` rather than the enum literal: migration 0005 adds
# 'internal' to the enum and creates these checks in one transaction, and
# PostgreSQL refuses to use a freshly added enum value before it is committed.
_AMOUNT_MATCHES_PROVIDER_SQL: Final = (
    "(provider::text = 'internal' AND amount = 0) OR (provider::text <> 'internal' AND amount > 0)"
)
_INTERNAL_IS_LOYALTY_GRANT_SQL: Final = (
    "provider::text <> 'internal' OR ("
    "base_amount IS NOT NULL AND base_amount > 0 "
    "AND loyalty_amount = base_amount "
    "AND discount_amount = 0 AND bonus_amount = 0 "
    "AND telegram_charge_id IS NULL "
    "AND status::text IN ('paid', 'delivered', 'refunded'))"
)
_AMOUNT_BREAKDOWN_SQL: Final = (
    "base_amount IS NULL OR amount = base_amount - discount_amount - loyalty_amount - bonus_amount"
)
_REWARD_MILESTONE_KIND_SQL: Final = (
    f"(milestone = {DISCOUNT_MILESTONE} AND kind::text = 'discount_50_percent') "
    f"OR (milestone = {FREE_PACK_MILESTONE} AND kind::text = 'free_pack')"
)
_REWARD_STATE_SHAPE_SQL: Final = (
    "(status::text = 'available' AND applied_purchase_id IS NULL AND applied_at IS NULL "
    "AND consumed_purchase_id IS NULL AND consumed_at IS NULL) "
    "OR (status::text = 'applied' AND applied_purchase_id IS NOT NULL "
    "AND applied_at IS NOT NULL AND consumed_purchase_id IS NULL AND consumed_at IS NULL) "
    "OR (status::text = 'consumed' AND consumed_purchase_id IS NOT NULL "
    "AND consumed_at IS NOT NULL)"
)
_REWARD_FREE_PACK_NEVER_APPLIED_SQL: Final = (
    "kind::text <> 'free_pack' OR status::text <> 'applied'"
)

# Daily Content. These types are created together with the tables that use
# them, so their literals are safe to use in the checks and index predicates.
_DAILY_SUBSCRIPTION_SHAPE_SQL: Final = (
    "(status = 'inactive' AND activated_at IS NULL AND anchor_minute IS NULL "
    "AND paused_at IS NULL) "
    "OR (status = 'active' AND activated_at IS NOT NULL AND anchor_minute IS NOT NULL "
    "AND paused_at IS NULL) "
    "OR (status = 'paused' AND activated_at IS NOT NULL AND anchor_minute IS NOT NULL "
    "AND paused_at IS NOT NULL)"
)
_DAILY_ANCHOR_RANGE_SQL: Final = (
    f"anchor_minute IS NULL OR (anchor_minute >= 0 AND anchor_minute < {MINUTES_PER_DAY})"
)
_DAILY_ACTIVATION_ORDER_SQL: Final = "activated_at IS NULL OR activated_at >= first_interaction_at"
_DAILY_SUBSCRIPTION_DELIVERABLE_SQL: Final = "status = 'active' AND blocked_at IS NULL"

_DAILY_CONTENT_SHAPE_SQL: Final = (
    "(kind = 'text' AND file_id IS NULL AND file_unique_id IS NULL AND body IS NOT NULL) "
    "OR (kind <> 'text' AND file_id IS NOT NULL AND file_unique_id IS NOT NULL)"
)
_DAILY_CONTENT_BODY_LENGTH_SQL: Final = "body IS NULL OR char_length(body) BETWEEN 1 AND 4096"
_DAILY_CONTENT_SIZES_SQL: Final = (
    "(file_size IS NULL OR file_size >= 0) AND (duration IS NULL OR duration >= 0) "
    "AND (width IS NULL OR width >= 0) AND (height IS NULL OR height >= 0)"
)
_DAILY_CONTENT_FILE_LIVE_SQL: Final = "file_unique_id IS NOT NULL AND status <> 'archived'"

_DAILY_DELIVERY_SENT_SHAPE_SQL: Final = (
    "(status = 'sent' AND content_id IS NOT NULL AND sent_at IS NOT NULL "
    "AND telegram_message_id IS NOT NULL) "
    "OR (status <> 'sent' AND sent_at IS NULL AND telegram_message_id IS NULL)"
)
_DAILY_DELIVERY_LEASE_SHAPE_SQL: Final = (
    "(status = 'sending' AND content_id IS NOT NULL AND lease_token IS NOT NULL "
    "AND lease_until IS NOT NULL) "
    "OR (status <> 'sending' AND lease_token IS NULL AND lease_until IS NULL)"
)
_DAILY_DELIVERY_NO_CONTENT_SQL: Final = "status <> 'no_content' OR content_id IS NULL"
_DAILY_DELIVERY_WINDOW_ORDER_SQL: Final = (
    "window_opens_at <= scheduled_for AND scheduled_for <= window_closes_at "
    "AND window_closes_at <= deadline_at"
)
# A buyer never holds the same item twice: not in two open deliveries at once,
# and never again once it was sent. A delivery that failed, expired or was
# cancelled gives its item back.
_DAILY_DELIVERY_HOLDS_CONTENT_SQL: Final = (
    "content_id IS NOT NULL AND status IN ('pending', 'sending', 'sent')"
)
_DAILY_DELIVERY_DUE_SQL: Final = "status IN ('pending', 'no_content')"

_DAILY_ATTEMPT_SHAPE_SQL: Final = "(finished_at IS NULL) = (outcome IS NULL)"

_DAILY_OUTBOX_LEASE_SHAPE_SQL: Final = (
    "(status = 'sending' AND lease_token IS NOT NULL AND lease_until IS NOT NULL) "
    "OR (status <> 'sending' AND lease_token IS NULL AND lease_until IS NULL)"
)
_DAILY_OUTBOX_SENT_SHAPE_SQL: Final = (
    "(status = 'sent' AND sent_at IS NOT NULL) "
    "OR (status <> 'sent' AND sent_at IS NULL AND telegram_message_id IS NULL)"
)
_DAILY_OUTBOX_CHANNEL_SQL: Final = "channel IN ('daily', 'shop')"
_DAILY_OUTBOX_BODY_LENGTH_SQL: Final = "char_length(body) BETWEEN 1 AND 4096"
_DAILY_OUTBOX_DUE_SQL: Final = "status = 'pending'"
_DAILY_OUTBOX_OPEN_SQL: Final = "status IN ('pending', 'sending')"
_DAILY_ALERT_FIRING_SHAPE_SQL: Final = "state <> 'firing' OR fired_at IS NOT NULL"
_DAILY_OUTBOX_ONE_ATTACHMENT_SQL: Final = "num_nonnulls(content_id, request_item_id) <= 1"
_DAILY_SUBSCRIPTION_BAN_SHAPE_SQL: Final = (
    "(requests_banned_at IS NULL) = (requests_banned_by IS NULL)"
)
_DAILY_REQUEST_CLOSED_SHAPE_SQL: Final = (
    "(status IN ('completed', 'rejected')) = (closed_at IS NOT NULL)"
)
_DAILY_REQUEST_NOTE_LENGTH_SQL: Final = "admin_note IS NULL OR char_length(admin_note) <= 2000"
_DAILY_REQUEST_ITEM_KIND_SHAPE_SQL: Final = (
    "(kind = 'text' AND file_id IS NULL AND body IS NOT NULL) "
    "OR (kind <> 'text' AND file_id IS NOT NULL)"
)
_DAILY_REQUEST_ITEM_BODY_LENGTH_SQL: Final = "body IS NULL OR char_length(body) BETWEEN 1 AND 4096"

#: Operator class that makes ``ILIKE '%term%'`` index-assisted.
_TRGM_OPS: Final = "gin_trgm_ops"


def _trigram_index(name: str, column: str) -> Index:
    """A GIN trigram index for the admin panel's substring search.

    A B-tree cannot serve ``ILIKE '%term%'`` — the pattern has no anchored
    prefix — so without this the admin search degrades into a full table scan as
    soon as the shop has real traffic.
    """
    return Index(name, column, postgresql_using="gin", postgresql_ops={column: _TRGM_OPS})


def _enum_column[EnumT: enum.Enum](enum_class: type[EnumT], name: str) -> Enum:
    """Native PostgreSQL enum storing the enum *values* (not member names)."""
    return Enum(
        enum_class,
        name=name,
        native_enum=True,
        create_constraint=False,
        validate_strings=True,
        values_callable=lambda enum: [member.value for member in enum],
    )


class ProductModel(TimestampMixin, Base):
    """A digital good reachable through exactly one deep link."""

    __tablename__ = "products"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(SLUG_MAX_LENGTH), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    photo_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delivery_url: Mapped[str] = mapped_column(Text, nullable=False)
    price_stars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_usdt: Mapped[Decimal | None] = mapped_column(
        Numeric(USDT_PRECISION, USDT_SCALE),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )

    purchases: Mapped[list[PurchaseModel]] = relationship(
        back_populates="product",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("slug", name="uq_products_slug"),
        CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="slug_format"),
        CheckConstraint(
            "price_stars IS NOT NULL OR price_usdt IS NOT NULL",
            name="price_present",
        ),
        CheckConstraint("price_stars IS NULL OR price_stars > 0", name="price_stars_positive"),
        CheckConstraint("price_usdt IS NULL OR price_usdt > 0", name="price_usdt_positive"),
        Index("ix_products_is_active_created_at", "is_active", "created_at"),
        _trigram_index("ix_products_title_trgm", "title"),
        _trigram_index("ix_products_slug_trgm", "slug"),
        _trigram_index("ix_products_description_trgm", "description"),
    )


class UserModel(Base):
    """A Telegram user who opened at least one product card."""

    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(32), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)

    referral_code: Mapped[str | None] = mapped_column(
        String(REFERRAL_CODE_MAX_LENGTH),
        nullable=True,
    )
    # Cache of the bonus ledger's sum. Written in the same transaction as the
    # entry that moved it, so the two cannot drift apart.
    bonus_balance: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
    )

    purchases: Mapped[list[PurchaseModel]] = relationship(
        back_populates="user",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("telegram_id > 0", name="telegram_id_positive"),
        # A referral code identifies exactly one inviter.
        UniqueConstraint("referral_code", name="uq_users_referral_code"),
        # The last line of defence against spending bonuses twice: whatever
        # races upstream, a balance cannot go negative.
        CheckConstraint("bonus_balance >= 0", name="bonus_balance_non_negative"),
        Index("ix_users_username_lower", text("lower(username)")),
        _trigram_index("ix_users_username_trgm", "username"),
    )


class PurchaseModel(Base):
    """One attempt to buy one product through one provider."""

    __tablename__ = "purchases"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="RESTRICT"),
        nullable=False,
    )
    product_id: Mapped[UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider: Mapped[PaymentProvider] = mapped_column(
        _enum_column(PaymentProvider, "payment_provider"),
        nullable=False,
    )
    status: Mapped[PurchaseStatus] = mapped_column(
        _enum_column(PurchaseStatus, "purchase_status"),
        nullable=False,
        server_default=text("'pending'"),
    )
    amount: Mapped[Decimal] = mapped_column(nullable=False)
    """What the buyer is billed. Unchanged by the referral feature."""

    # How ``amount`` was reached. Nullable ``base_amount`` is what keeps the
    # migration additive: rows written before this feature have no breakdown,
    # and for those the billed amount *was* the list price.
    base_amount: Mapped[Decimal | None] = mapped_column(nullable=True)
    discount_amount: Mapped[Decimal] = mapped_column(
        nullable=False,
        server_default=text("0"),
    )
    bonus_amount: Mapped[Decimal] = mapped_column(
        nullable=False,
        server_default=text("0"),
    )
    loyalty_amount: Mapped[Decimal] = mapped_column(
        nullable=False,
        server_default=text("0"),
    )
    """Waived by a loyalty reward: half a price, or all of it on a free pack."""
    currency: Mapped[Currency] = mapped_column(
        _enum_column(Currency, "currency"),
        nullable=False,
    )
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    telegram_charge_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    delivered_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(nullable=True)

    user: Mapped[UserModel] = relationship(back_populates="purchases", lazy="raise")
    product: Mapped[ProductModel] = relationship(back_populates="purchases", lazy="raise")

    __table_args__ = (
        # One provider invoice maps to exactly one purchase: replayed webhooks
        # and duplicated Telegram updates cannot create a second row.
        UniqueConstraint("provider", "external_id", name="uq_purchases_provider_external_id"),
        # A user can hold at most one paid copy of a product. Enforced by the
        # database so a race between two payment callbacks cannot double-charge.
        Index(
            "uq_purchases_user_product_paid",
            "user_id",
            "product_id",
            unique=True,
            postgresql_where=text(_ACCESS_STATUS_SQL),
        ),
        # At most one live CryptoBot checkout per buyer and product. The checkout
        # lock already keeps a second invoice from being issued; this is what
        # still holds if that lock ever expired mid-checkout.
        Index(
            "uq_purchases_user_product_pending_crypto",
            "user_id",
            "product_id",
            unique=True,
            postgresql_where=text(_PENDING_CRYPTO_SQL),
        ),
        Index(
            "uq_purchases_telegram_charge_id",
            "telegram_charge_id",
            unique=True,
            postgresql_where=text("telegram_charge_id IS NOT NULL"),
        ),
        Index("ix_purchases_paid_at", "paid_at"),
        Index("ix_purchases_created_at", "created_at"),
        Index("ix_purchases_product_id_status", "product_id", "status"),
        Index("ix_purchases_user_id", "user_id"),
        Index("ix_purchases_status_created_at", "status", "created_at"),
        Index("ix_purchases_user_id_status", "user_id", "status"),
        _trigram_index("ix_purchases_external_id_trgm", "external_id"),
        _trigram_index("ix_purchases_telegram_charge_id_trgm", "telegram_charge_id"),
        # A payment rail always bills something; only an internal grant, which
        # bills nobody, may be zero — and it must be.
        CheckConstraint(_AMOUNT_MATCHES_PROVIDER_SQL, name="amount_matches_provider"),
        # An internal purchase exists for one reason: a loyalty free pack. It
        # waives the whole list price, carries nothing else, has no Telegram
        # charge, and is never pending or expired — so no provider is ever
        # asked about it.
        CheckConstraint(_INTERNAL_IS_LOYALTY_GRANT_SQL, name="internal_is_loyalty_grant"),
        CheckConstraint("discount_amount >= 0", name="discount_amount_non_negative"),
        CheckConstraint("bonus_amount >= 0", name="bonus_amount_non_negative"),
        CheckConstraint("loyalty_amount >= 0", name="loyalty_amount_non_negative"),
        # The breakdown must add up. Without this a bug in the pricing code
        # could store a sale whose history does not explain its own price.
        CheckConstraint(_AMOUNT_BREAKDOWN_SQL, name="amount_breakdown_consistent"),
        CheckConstraint(
            "status IN ('pending', 'expired') OR paid_at IS NOT NULL",
            name="paid_at_present",
        ),
        CheckConstraint(
            "status <> 'delivered' OR (delivered_at IS NOT NULL AND delivered_url IS NOT NULL)",
            name="delivery_complete",
        ),
    )


class ReferralModel(Base):
    """One buyer, permanently attributed to whoever invited them."""

    __tablename__ = "referrals"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    referrer_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="RESTRICT"),
        nullable=False,
    )
    referred_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)
    # The first-purchase discount exists once. These two columns are what burn
    # it, and they are set when the payment is confirmed rather than when the
    # invoice is issued — an abandoned invoice must not consume the discount.
    discount_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    discount_purchase_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("purchases.id", ondelete="RESTRICT"),
        nullable=True,
    )

    __table_args__ = (
        # One referrer per buyer, for life. A later link cannot take the buyer
        # over, and this is the constraint that guarantees it rather than a
        # check in Python that a race could slip past.
        UniqueConstraint("referred_user_id", name="uq_referrals_referred_user_id"),
        CheckConstraint("referrer_user_id <> referred_user_id", name="no_self_referral"),
        CheckConstraint(
            "(discount_used_at IS NULL) = (discount_purchase_id IS NULL)",
            name="discount_usage_complete",
        ),
        # A discount is consumed by at most one purchase.
        Index(
            "uq_referrals_discount_purchase_id",
            "discount_purchase_id",
            unique=True,
            postgresql_where=text("discount_purchase_id IS NOT NULL"),
        ),
        Index("ix_referrals_referrer_user_id", "referrer_user_id"),
        Index("ix_referrals_created_at", "created_at"),
    )


class BonusTransactionModel(Base):
    """One movement of the internal bonus balance.

    An append-only ledger, with one deliberate exception: a reservation is
    promoted from ``bonus_reserved`` to ``bonus_spent`` when the invoice behind
    it is paid. The amount never changes — only the reason the units are held.
    """

    __tablename__ = "bonus_transactions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="RESTRICT"),
        nullable=False,
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    """Signed bonus units: positive credits the balance, negative debits it."""

    type: Mapped[BonusTransactionType] = mapped_column(
        _enum_column(BonusTransactionType, "bonus_transaction_type"),
        nullable=False,
    )
    referral_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("referrals.id", ondelete="RESTRICT"),
        nullable=True,
    )
    purchase_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("purchases.id", ondelete="RESTRICT"),
        nullable=True,
    )
    stars_per_usdt: Mapped[Decimal | None] = mapped_column(
        Numeric(STARS_RATE_PRECISION, STARS_RATE_SCALE),
        nullable=True,
    )
    """Stars-per-USDT rate used for this entry, so history stays readable.

    Taken from the product's own two prices at the moment of the sale. Set only
    on entries produced by a USDT purchase; a Stars purchase needs no rate.
    """

    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)

    __table_args__ = (
        # The idempotency guarantee for the whole feature: one entry of each
        # kind per purchase. Five replayed payment notifications credit one
        # reward, not five, because the second insert cannot exist.
        Index(
            "uq_bonus_transactions_purchase_type",
            "purchase_id",
            "type",
            unique=True,
            postgresql_where=text("purchase_id IS NOT NULL"),
        ),
        CheckConstraint("amount <> 0", name="amount_not_zero"),
        CheckConstraint(
            "stars_per_usdt IS NULL OR stars_per_usdt > 0",
            name="rate_positive",
        ),
        Index("ix_bonus_transactions_user_id_created_at", "user_id", "created_at"),
        Index("ix_bonus_transactions_referral_id", "referral_id"),
    )


class LoyaltyRewardModel(Base):
    """One loyalty milestone reward of one buyer.

    Written lazily, the first time a reward is used — never by a backfill — so
    the table only ever holds rewards somebody actually touched. VIP has no row:
    it is read off the number of completed purchases.
    """

    __tablename__ = "loyalty_rewards"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="RESTRICT"),
        nullable=False,
    )
    milestone: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    kind: Mapped[LoyaltyRewardKind] = mapped_column(
        _enum_column(LoyaltyRewardKind, "loyalty_reward_kind"),
        nullable=False,
    )
    status: Mapped[LoyaltyRewardStatus] = mapped_column(
        _enum_column(LoyaltyRewardStatus, "loyalty_reward_status"),
        nullable=False,
        server_default=text("'available'"),
    )
    applied_purchase_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("purchases.id", ondelete="RESTRICT"),
        nullable=True,
    )
    consumed_purchase_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("purchases.id", ondelete="RESTRICT"),
        nullable=True,
    )
    earned_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        # Each milestone rewards a buyer once, however many times it is reached
        # concurrently: the second insert of the same reward cannot exist.
        UniqueConstraint("user_id", "milestone", name="uq_loyalty_rewards_user_milestone"),
        CheckConstraint(_REWARD_MILESTONE_KIND_SQL, name="milestone_kind"),
        # Every status carries exactly the facts that status implies.
        CheckConstraint(_REWARD_STATE_SHAPE_SQL, name="state_shape"),
        # A free pack is handed over in one step; it is never priced into an invoice.
        CheckConstraint(_REWARD_FREE_PACK_NEVER_APPLIED_SQL, name="free_pack_never_applied"),
        # One reward per purchase, whichever way it was used.
        Index(
            "uq_loyalty_rewards_applied_purchase_id",
            "applied_purchase_id",
            unique=True,
            postgresql_where=text("applied_purchase_id IS NOT NULL"),
        ),
        Index(
            "uq_loyalty_rewards_consumed_purchase_id",
            "consumed_purchase_id",
            unique=True,
            postgresql_where=text("consumed_purchase_id IS NOT NULL"),
        ),
    )


# --------------------------------------------------------------------------- #
# Daily Content                                                               #
# --------------------------------------------------------------------------- #


class DailySubscriptionModel(TimestampMixin, Base):
    """One buyer's relationship with the Daily Content bot.

    Written the first time the buyer writes to the bot, whatever their access.
    The activation — the first interaction while they have access — fixes the
    anchor of their personal daily window and never moves it.
    """

    __tablename__ = "daily_subscriptions"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="RESTRICT"),
        primary_key=True,
        autoincrement=False,
    )
    status: Mapped[DailySubscriptionStatus] = mapped_column(
        _enum_column(DailySubscriptionStatus, "daily_subscription_status"),
        nullable=False,
        server_default=text("'inactive'"),
    )
    first_interaction_at: Mapped[datetime] = mapped_column(nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    anchor_minute: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(nullable=True)
    blocked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    exhausted_notified_at: Mapped[datetime | None] = mapped_column(nullable=True)
    requests_banned_at: Mapped[datetime | None] = mapped_column(nullable=True)
    requests_banned_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        CheckConstraint(_DAILY_SUBSCRIPTION_SHAPE_SQL, name="state_shape"),
        CheckConstraint(_DAILY_SUBSCRIPTION_BAN_SHAPE_SQL, name="requests_ban_shape"),
        CheckConstraint(_DAILY_ANCHOR_RANGE_SQL, name="anchor_minute_range"),
        CheckConstraint(_DAILY_ACTIVATION_ORDER_SQL, name="activation_after_first_interaction"),
        # The planner's question — whose window opens around now? — over the
        # buyers it may actually send to.
        Index(
            "ix_daily_subscriptions_deliverable_anchor",
            "anchor_minute",
            postgresql_where=text(_DAILY_SUBSCRIPTION_DELIVERABLE_SQL),
        ),
    )


class DailyContentModel(TimestampMixin, Base):
    """One item of the Daily Content library.

    Never physically deleted: archiving is the delete. Every delivery that ever
    sent the item keeps pointing at it, and that history is what stops a buyer
    from receiving the same item twice.
    """

    __tablename__ = "daily_content"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    status: Mapped[DailyContentStatus] = mapped_column(
        _enum_column(DailyContentStatus, "daily_content_status"),
        nullable=False,
        server_default=text("'active'"),
    )
    kind: Mapped[DailyContentKind] = mapped_column(
        _enum_column(DailyContentKind, "daily_content_kind"),
        nullable=False,
    )
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The message text, or the media caption."""

    entities: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB, nullable=True)
    """Telegram formatting entities of ``body``, exactly as Telegram sent them."""

    file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_unique_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    file_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    added_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status_changed_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"),
        nullable=False,
    )
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # The same Telegram message is one item, however often its update is
        # delivered to the bot.
        UniqueConstraint(
            "source_chat_id",
            "source_message_id",
            name="uq_daily_content_source",
        ),
        CheckConstraint(_DAILY_CONTENT_SHAPE_SQL, name="kind_shape"),
        CheckConstraint(_DAILY_CONTENT_BODY_LENGTH_SQL, name="body_length"),
        CheckConstraint(_DAILY_CONTENT_SIZES_SQL, name="sizes_non_negative"),
        # The same file is in the library once while it is not archived.
        Index(
            "uq_daily_content_file_unique_id",
            "file_unique_id",
            unique=True,
            postgresql_where=text(_DAILY_CONTENT_FILE_LIVE_SQL),
        ),
        Index("ix_daily_content_status_created_at", "status", "created_at"),
    )


class DailyDeliveryModel(TimestampMixin, Base):
    """One buyer's delivery for one Moscow date — and, once sent, their history.

    The row is the job and its record at once: the database decides that a
    buyer has one delivery per date and never the same item twice, and the
    lease columns decide who may send it right now.
    """

    __tablename__ = "daily_deliveries"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("daily_subscriptions.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    delivery_date: Mapped[date] = mapped_column(Date, nullable=False)
    trigger: Mapped[DailyDeliveryTrigger] = mapped_column(
        _enum_column(DailyDeliveryTrigger, "daily_delivery_trigger"),
        nullable=False,
    )
    status: Mapped[DailyDeliveryStatus] = mapped_column(
        _enum_column(DailyDeliveryStatus, "daily_delivery_status"),
        nullable=False,
        server_default=text("'pending'"),
    )
    content_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("daily_content.id", ondelete="RESTRICT"),
        nullable=True,
    )
    scheduled_for: Mapped[datetime] = mapped_column(nullable=False)
    window_opens_at: Mapped[datetime] = mapped_column(nullable=False)
    window_closes_at: Mapped[datetime] = mapped_column(nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(nullable=False)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    lease_token: Mapped[UUID | None] = mapped_column(nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_outcome: Mapped[DailyAttemptOutcome | None] = mapped_column(
        _enum_column(DailyAttemptOutcome, "daily_attempt_outcome"),
        nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # At most one delivery per buyer and date, whatever runs twice.
        UniqueConstraint("user_id", "delivery_date", name="uq_daily_deliveries_user_date"),
        CheckConstraint(_DAILY_DELIVERY_SENT_SHAPE_SQL, name="sent_shape"),
        CheckConstraint(_DAILY_DELIVERY_LEASE_SHAPE_SQL, name="lease_shape"),
        CheckConstraint(_DAILY_DELIVERY_NO_CONTENT_SQL, name="no_content_shape"),
        CheckConstraint(_DAILY_DELIVERY_WINDOW_ORDER_SQL, name="window_order"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        Index(
            "uq_daily_deliveries_user_content",
            "user_id",
            "content_id",
            unique=True,
            postgresql_where=text(_DAILY_DELIVERY_HOLDS_CONTENT_SQL),
        ),
        Index(
            "ix_daily_deliveries_due",
            "next_attempt_at",
            postgresql_where=text(_DAILY_DELIVERY_DUE_SQL),
        ),
        Index(
            "ix_daily_deliveries_lease_until",
            "lease_until",
            postgresql_where=text("status = 'sending'"),
        ),
        Index("ix_daily_deliveries_user_id_status", "user_id", "status"),
        Index(
            "ix_daily_deliveries_content_id_sent",
            "content_id",
            postgresql_where=text("status = 'sent'"),
        ),
        Index("ix_daily_deliveries_sent_at", "sent_at"),
        Index("ix_daily_deliveries_delivery_date_status", "delivery_date", "status"),
    )


class DailyDeliveryAttemptModel(Base):
    """One attempt to send a delivery's item — the delivery's own history."""

    __tablename__ = "daily_delivery_attempts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    delivery_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("daily_deliveries.id", ondelete="RESTRICT"),
        nullable=False,
    )
    attempt_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    lease_token: Mapped[UUID] = mapped_column(nullable=False)
    content_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("daily_content.id", ondelete="RESTRICT"),
        nullable=True,
    )
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    outcome: Mapped[DailyAttemptOutcome | None] = mapped_column(
        _enum_column(DailyAttemptOutcome, "daily_attempt_outcome"),
        nullable=True,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_after_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "delivery_id",
            "attempt_no",
            name="uq_daily_delivery_attempts_delivery_attempt",
        ),
        UniqueConstraint("lease_token", name="uq_daily_delivery_attempts_lease_token"),
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        CheckConstraint(_DAILY_ATTEMPT_SHAPE_SQL, name="outcome_shape"),
        Index("ix_daily_delivery_attempts_finished_at", "finished_at"),
    )


class DailyOutboxModel(TimestampMixin, Base):
    """A message the Daily runtime owes somebody: an alert, a notice, an answer,
    or an item the admin panel asked for.

    Queued once per ``dedupe_key``, sent under a lease like a delivery, and
    dropped unsent once its deadline passes.
    """

    __tablename__ = "daily_outbox"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    content_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("daily_content.id", ondelete="RESTRICT"),
        nullable=True,
    )
    request_item_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("daily_request_items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    status: Mapped[DailyOutboxStatus] = mapped_column(
        _enum_column(DailyOutboxStatus, "daily_outbox_status"),
        nullable=False,
        server_default=text("'pending'"),
    )
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(nullable=False)
    lease_token: Mapped[UUID | None] = mapped_column(nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # The same alert or notice is queued once, whoever decides it.
        UniqueConstraint("dedupe_key", name="uq_daily_outbox_dedupe_key"),
        CheckConstraint(_DAILY_OUTBOX_LEASE_SHAPE_SQL, name="lease_shape"),
        CheckConstraint(_DAILY_OUTBOX_SENT_SHAPE_SQL, name="sent_shape"),
        CheckConstraint(_DAILY_OUTBOX_CHANNEL_SQL, name="channel_known"),
        CheckConstraint(_DAILY_OUTBOX_BODY_LENGTH_SQL, name="body_length"),
        CheckConstraint(_DAILY_OUTBOX_ONE_ATTACHMENT_SQL, name="one_attachment"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        Index(
            "ix_daily_outbox_due",
            "next_attempt_at",
            postgresql_where=text(_DAILY_OUTBOX_DUE_SQL),
        ),
        Index(
            "ix_daily_outbox_lease_until",
            "lease_until",
            postgresql_where=text("status = 'sending'"),
        ),
        Index(
            "ix_daily_outbox_open_deadline",
            "deadline_at",
            postgresql_where=text(_DAILY_OUTBOX_OPEN_SQL),
        ),
    )


class DailyAlertModel(TimestampMixin, Base):
    """One of the owner's alerts: its state, never its stream of messages."""

    __tablename__ = "daily_alerts"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[DailyAlertState] = mapped_column(
        _enum_column(DailyAlertState, "daily_alert_state"),
        nullable=False,
        server_default=text("'ok'"),
    )
    notifications: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    value: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    fired_at: Mapped[datetime | None] = mapped_column(nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint(_DAILY_ALERT_FIRING_SHAPE_SQL, name="firing_shape"),
        CheckConstraint("notifications >= 0", name="notifications_non_negative"),
    )


class DailyRequestModel(TimestampMixin, Base):
    """A buyer's content request: one per submitted draft, versioned for the panel."""

    __tablename__ = "daily_requests"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("daily_subscriptions.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[DailyRequestStatus] = mapped_column(
        _enum_column(DailyRequestStatus, "daily_request_status"),
        nullable=False,
        server_default=text("'new'"),
    )
    client_draft_id: Mapped[UUID] = mapped_column(nullable=False)
    purchases_at_submit: Mapped[int] = mapped_column(Integer, nullable=False)
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        # The same draft submitted twice is one request.
        UniqueConstraint("client_draft_id", name="uq_daily_requests_client_draft_id"),
        CheckConstraint(_DAILY_REQUEST_CLOSED_SHAPE_SQL, name="closed_shape"),
        CheckConstraint(_DAILY_REQUEST_NOTE_LENGTH_SQL, name="note_length"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("purchases_at_submit >= 0", name="purchases_non_negative"),
        Index("ix_daily_requests_status_created_at", "status", "created_at"),
        Index("ix_daily_requests_user_id_status", "user_id", "status"),
    )


class DailyRequestItemModel(Base):
    """One message of a content request."""

    __tablename__ = "daily_request_items"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    request_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("daily_requests.id", ondelete="RESTRICT"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    kind: Mapped[DailyContentKind] = mapped_column(
        _enum_column(DailyContentKind, "daily_content_kind"),
        nullable=False,
    )
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    entities: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB, nullable=True)
    file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_unique_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    file_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "request_id",
            "position",
            name="uq_daily_request_items_request_position",
        ),
        CheckConstraint(_DAILY_REQUEST_ITEM_KIND_SHAPE_SQL, name="kind_shape"),
        CheckConstraint(_DAILY_REQUEST_ITEM_BODY_LENGTH_SQL, name="body_length"),
        CheckConstraint("position BETWEEN 1 AND 10", name="position_range"),
    )


class DailyRequestEventModel(Base):
    """One line of a content request's audit trail."""

    __tablename__ = "daily_request_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    request_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("daily_requests.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    from_status: Mapped[DailyRequestStatus | None] = mapped_column(
        _enum_column(DailyRequestStatus, "daily_request_status"),
        nullable=True,
    )
    to_status: Mapped[DailyRequestStatus | None] = mapped_column(
        _enum_column(DailyRequestStatus, "daily_request_status"),
        nullable=True,
    )
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    outbox_message_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("daily_outbox.id", ondelete="RESTRICT"),
        nullable=True,
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"), nullable=False)

    __table_args__ = (
        # The owner's reply submitted twice is queued once.
        UniqueConstraint("idempotency_key", name="uq_daily_request_events_idempotency_key"),
        Index("ix_daily_request_events_request_id", "request_id"),
        Index("ix_daily_request_events_outbox_message_id", "outbox_message_id"),
    )
