"""Purchase, search and verification schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from app.api.schemas.common import ApiModel
from app.domain.delivery import DeliveryStatus
from app.domain.enums import Currency, PaymentProvider, PurchaseStatus
from app.domain.payments import PaymentState
from app.domain.verification import VerificationOutcome

if TYPE_CHECKING:
    from app.domain.entities import Product, Purchase, PurchaseRecord, User
    from app.domain.verification import VerificationReport


class BuyerResponse(ApiModel):
    """The buyer, as shown in listings and search results."""

    telegram_id: int
    username: str | None
    first_name: str | None
    display_name: str

    @classmethod
    def from_domain(cls, user: User) -> BuyerResponse:
        return cls(
            telegram_id=user.telegram_id,
            username=user.username,
            first_name=user.first_name,
            display_name=user.display_name,
        )


class ProductSummaryResponse(ApiModel):
    """Just enough product context for a purchase row."""

    id: UUID
    slug: str
    title: str

    @classmethod
    def from_domain(cls, product: Product) -> ProductSummaryResponse:
        return cls(id=product.id, slug=product.slug, title=product.title)


class PurchaseResponse(ApiModel):
    """One purchase."""

    id: UUID
    user_id: int
    product_id: UUID
    provider: PaymentProvider
    status: PurchaseStatus
    amount: Decimal
    currency: Currency
    external_id: str
    telegram_charge_id: str | None
    delivered_url: str | None
    created_at: datetime
    paid_at: datetime | None
    delivered_at: datetime | None

    @classmethod
    def from_domain(cls, purchase: Purchase) -> PurchaseResponse:
        return cls(
            id=purchase.id,
            user_id=purchase.user_id,
            product_id=purchase.product_id,
            provider=purchase.provider,
            status=purchase.status,
            amount=purchase.amount,
            currency=purchase.currency,
            external_id=purchase.external_id,
            telegram_charge_id=purchase.telegram_charge_id,
            delivered_url=purchase.delivered_url,
            created_at=purchase.created_at,
            paid_at=purchase.paid_at,
            delivered_at=purchase.delivered_at,
        )


class PurchaseRecordResponse(ApiModel):
    """A purchase joined with its buyer and product, for the search table."""

    purchase: PurchaseResponse
    buyer: BuyerResponse
    product: ProductSummaryResponse

    @classmethod
    def from_domain(cls, record: PurchaseRecord) -> PurchaseRecordResponse:
        return cls(
            purchase=PurchaseResponse.from_domain(record.purchase),
            buyer=BuyerResponse.from_domain(record.user),
            product=ProductSummaryResponse.from_domain(record.product),
        )


class DeliveryAttemptResponse(ApiModel):
    """Outcome of the delivery attempt triggered by an admin action."""

    status: DeliveryStatus
    attempts: int
    error: str | None


class VerificationResponse(ApiModel):
    """Result of "check payment": what was found and what was done."""

    purchase_id: UUID
    provider: PaymentProvider
    outcome: VerificationOutcome
    resolved: bool
    status_before: PurchaseStatus
    status_after: PurchaseStatus
    provider_state: PaymentState | None
    delivery: DeliveryAttemptResponse | None
    detail: str | None

    @classmethod
    def from_domain(cls, report: VerificationReport) -> VerificationResponse:
        delivery = (
            DeliveryAttemptResponse(
                status=report.delivery.status,
                attempts=report.delivery.attempts,
                error=report.delivery.error,
            )
            if report.delivery is not None
            else None
        )
        return cls(
            purchase_id=report.purchase_id,
            provider=report.provider,
            outcome=report.outcome,
            resolved=report.outcome.is_resolved,
            status_before=report.status_before,
            status_after=report.status_after,
            provider_state=report.provider_state,
            delivery=delivery,
            detail=report.detail,
        )
