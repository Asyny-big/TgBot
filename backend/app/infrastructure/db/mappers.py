"""Row-to-entity mapping.

The only place that knows about both SQLAlchemy models and domain entities.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain.entities import (
    BonusTransaction,
    DailyContent,
    DailyDelivery,
    DailyDeliveryAttempt,
    DailySubscription,
    LoyaltyReward,
    Product,
    Purchase,
    PurchaseRecord,
    Referral,
    ReferralRecord,
    User,
)

if TYPE_CHECKING:
    from app.infrastructure.db.models import (
        BonusTransactionModel,
        DailyContentModel,
        DailyDeliveryAttemptModel,
        DailyDeliveryModel,
        DailySubscriptionModel,
        LoyaltyRewardModel,
        ProductModel,
        PurchaseModel,
        ReferralModel,
        UserModel,
    )


def to_product(model: ProductModel) -> Product:
    """Map a product row onto its domain entity."""
    return Product(
        id=model.id,
        slug=model.slug,
        title=model.title,
        description=model.description,
        photo_file_id=model.photo_file_id,
        delivery_url=model.delivery_url,
        price_stars=model.price_stars,
        price_usdt=model.price_usdt,
        is_active=model.is_active,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def to_user(model: UserModel) -> User:
    """Map a user row onto its domain entity."""
    return User(
        telegram_id=model.telegram_id,
        username=model.username,
        first_name=model.first_name,
        language_code=model.language_code,
        created_at=model.created_at,
        last_seen_at=model.last_seen_at,
        referral_code=model.referral_code,
        bonus_balance=model.bonus_balance,
    )


def to_referral(model: ReferralModel) -> Referral:
    """Map a referral row onto its domain entity."""
    return Referral(
        id=model.id,
        referrer_user_id=model.referrer_user_id,
        referred_user_id=model.referred_user_id,
        created_at=model.created_at,
        discount_used_at=model.discount_used_at,
        discount_purchase_id=model.discount_purchase_id,
    )


def to_bonus_transaction(model: BonusTransactionModel) -> BonusTransaction:
    """Map a bonus ledger row onto its domain entity."""
    return BonusTransaction(
        id=model.id,
        user_id=model.user_id,
        amount=model.amount,
        type=model.type,
        referral_id=model.referral_id,
        purchase_id=model.purchase_id,
        stars_per_usdt=model.stars_per_usdt,
        created_at=model.created_at,
    )


def to_referral_record(
    referral: ReferralModel,
    referrer: UserModel,
    referred: UserModel,
    *,
    referred_purchase_count: int,
    reward_total: int,
) -> ReferralRecord:
    """Combine a relationship with both parties for the admin panel."""
    return ReferralRecord(
        referral=to_referral(referral),
        referrer=to_user(referrer),
        referred=to_user(referred),
        referred_purchase_count=referred_purchase_count,
        reward_total=reward_total,
    )


def to_purchase(model: PurchaseModel) -> Purchase:
    """Map a purchase row onto its domain entity."""
    return Purchase(
        id=model.id,
        user_id=model.user_id,
        product_id=model.product_id,
        provider=model.provider,
        status=model.status,
        amount=model.amount,
        base_amount=model.base_amount,
        discount_amount=model.discount_amount,
        bonus_amount=model.bonus_amount,
        loyalty_amount=model.loyalty_amount,
        currency=model.currency,
        external_id=model.external_id,
        telegram_charge_id=model.telegram_charge_id,
        delivered_url=model.delivered_url,
        created_at=model.created_at,
        paid_at=model.paid_at,
        delivered_at=model.delivered_at,
    )


def to_record(
    purchase: PurchaseModel,
    user: UserModel,
    product: ProductModel,
) -> PurchaseRecord:
    """Combine a purchase with its buyer and product for admin listings."""
    return PurchaseRecord(
        purchase=to_purchase(purchase),
        user=to_user(user),
        product=to_product(product),
    )


def to_loyalty_reward(model: LoyaltyRewardModel) -> LoyaltyReward:
    """Map a loyalty reward row onto its domain entity."""
    return LoyaltyReward(
        id=model.id,
        user_id=model.user_id,
        milestone=model.milestone,
        kind=model.kind,
        status=model.status,
        earned_at=model.earned_at,
        applied_purchase_id=model.applied_purchase_id,
        consumed_purchase_id=model.consumed_purchase_id,
        applied_at=model.applied_at,
        consumed_at=model.consumed_at,
    )


def to_daily_subscription(model: DailySubscriptionModel) -> DailySubscription:
    """Map a Daily Content subscription row onto its domain entity."""
    return DailySubscription(
        user_id=model.user_id,
        status=model.status,
        first_interaction_at=model.first_interaction_at,
        activated_at=model.activated_at,
        anchor_minute=model.anchor_minute,
        paused_at=model.paused_at,
        blocked_at=model.blocked_at,
        exhausted_notified_at=model.exhausted_notified_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def to_daily_content(model: DailyContentModel) -> DailyContent:
    """Map a Daily Content library row onto its domain entity."""
    return DailyContent(
        id=model.id,
        status=model.status,
        kind=model.kind,
        source_chat_id=model.source_chat_id,
        source_message_id=model.source_message_id,
        added_by=model.added_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
        status_changed_at=model.status_changed_at,
        body=model.body,
        entities=tuple(model.entities or ()),
        file_id=model.file_id,
        file_unique_id=model.file_unique_id,
        file_size=model.file_size,
        file_name=model.file_name,
        mime_type=model.mime_type,
        duration=model.duration,
        width=model.width,
        height=model.height,
        status_reason=model.status_reason,
    )


def to_daily_delivery(model: DailyDeliveryModel) -> DailyDelivery:
    """Map a Daily Content delivery row onto its domain entity."""
    return DailyDelivery(
        id=model.id,
        user_id=model.user_id,
        delivery_date=model.delivery_date,
        trigger=model.trigger,
        status=model.status,
        scheduled_for=model.scheduled_for,
        window_opens_at=model.window_opens_at,
        window_closes_at=model.window_closes_at,
        deadline_at=model.deadline_at,
        next_attempt_at=model.next_attempt_at,
        attempts=model.attempts,
        created_at=model.created_at,
        updated_at=model.updated_at,
        content_id=model.content_id,
        lease_token=model.lease_token,
        lease_until=model.lease_until,
        sent_at=model.sent_at,
        telegram_message_id=model.telegram_message_id,
        last_outcome=model.last_outcome,
        last_error=model.last_error,
    )


def to_daily_attempt(model: DailyDeliveryAttemptModel) -> DailyDeliveryAttempt:
    """Map a Daily Content send attempt row onto its domain entity."""
    return DailyDeliveryAttempt(
        id=model.id,
        delivery_id=model.delivery_id,
        attempt_no=model.attempt_no,
        lease_token=model.lease_token,
        started_at=model.started_at,
        content_id=model.content_id,
        finished_at=model.finished_at,
        outcome=model.outcome,
        error=model.error,
        retry_after_seconds=model.retry_after_seconds,
        telegram_message_id=model.telegram_message_id,
    )
