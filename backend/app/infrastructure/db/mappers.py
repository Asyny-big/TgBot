"""Row-to-entity mapping.

The only place that knows about both SQLAlchemy models and domain entities.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain.entities import Product, Purchase, PurchaseRecord, User

if TYPE_CHECKING:
    from app.infrastructure.db.models import ProductModel, PurchaseModel, UserModel


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
