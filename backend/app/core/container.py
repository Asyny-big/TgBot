"""Composition root.

Wiring lives here and nowhere else: services receive their collaborators, never
construct them. Both entrypoints (admin API, bot) build one container per
process from the shared infrastructure resources.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from app.infrastructure.cache.locks import RedisLockManager
from app.infrastructure.cache.rate_limit import RedisRateLimiter
from app.infrastructure.cache.revocation import RedisTokenRevocationStore
from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory
from app.infrastructure.payments.cryptobot import CryptoBotClient
from app.services.auth import AuthService
from app.services.checkout import CheckoutService
from app.services.delivery import DeliveryService
from app.services.products import ProductService
from app.services.purchases import PurchaseService
from app.services.stats import StatsService

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.core.resources import Resources
    from app.domain.delivery import DeliveryGateway
    from app.domain.payments import StarsInvoiceSender


@dataclass(frozen=True, slots=True)
class Container:
    """Every service the process can use, already wired."""

    settings: Settings
    uow_factory: SqlAlchemyUnitOfWorkFactory
    locks: RedisLockManager
    rate_limiter: RedisRateLimiter
    crypto_payments: CryptoBotClient
    products: ProductService
    purchases: PurchaseService
    stats: StatsService
    auth: AuthService

    @classmethod
    def create(cls, resources: Resources) -> Container:
        """Build the container from the process's infrastructure resources."""
        settings = resources.settings
        uow_factory = SqlAlchemyUnitOfWorkFactory(resources.database)
        locks = RedisLockManager(resources.cache.client, settings.redis)
        purchases = PurchaseService(
            uow_factory=uow_factory,
            locks=locks,
            invoice_ttl=timedelta(seconds=settings.cryptobot.invoice_ttl_seconds),
        )
        return cls(
            settings=settings,
            uow_factory=uow_factory,
            locks=locks,
            rate_limiter=RedisRateLimiter(resources.cache.client),
            crypto_payments=CryptoBotClient(settings.cryptobot),
            products=ProductService(uow_factory=uow_factory, telegram=settings.telegram),
            purchases=purchases,
            stats=StatsService(uow_factory=uow_factory),
            auth=AuthService(
                settings.security,
                RedisTokenRevocationStore(resources.cache.client),
            ),
        )

    def build_checkout(
        self,
        *,
        delivery_gateway: DeliveryGateway,
        stars: StarsInvoiceSender,
    ) -> CheckoutService:
        """Wire checkout to a concrete transport (bot process or admin API)."""
        return CheckoutService(
            purchases=self.purchases,
            delivery=self.build_delivery(delivery_gateway),
            stars=stars,
            crypto=self.crypto_payments,
        )

    def build_delivery(self, gateway: DeliveryGateway) -> DeliveryService:
        """Create the delivery service for a concrete transport.

        The transport only exists in the bot process, so it is supplied here
        instead of being stored on the container.
        """
        return DeliveryService(
            uow_factory=self.uow_factory,
            purchases=self.purchases,
            gateway=gateway,
            locks=self.locks,
            settings=self.settings.delivery,
        )
