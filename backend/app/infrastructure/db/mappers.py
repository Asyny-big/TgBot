"""Row-to-entity mapping.

The only place that knows about both SQLAlchemy models and domain entities.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain.entities import (
    BonusTransaction,
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
        rate_units_per_usdt=model.rate_units_per_usdt,
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
