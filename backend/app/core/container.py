"""Composition root.

Wiring lives here and nowhere else: services receive their collaborators, never
construct them. Both entrypoints (admin API, bot) build one container per
process from the shared infrastructure resources.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING

from app.infrastructure.cache.locks import RedisLockManager
from app.infrastructure.cache.rate_limit import RedisRateLimiter
from app.infrastructure.cache.revocation import RedisTokenRevocationStore
from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory
from app.infrastructure.payments.cryptobot import CryptoBotClient
from app.services.auth import AuthService
from app.services.bonuses import BonusService
from app.services.checkout import CheckoutService
from app.services.delivery import DeliveryService
from app.services.products import ProductService
from app.services.purchases import PurchaseService
from app.services.referrals import ReferralService
from app.services.stats import StatsService

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.core.resources import Resources
    from app.domain.delivery import DeliveryGateway
    from app.domain.notifications import RewardNotifier
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
    referrals: ReferralService
    bonuses: BonusService
    stats: StatsService
    auth: AuthService

    @classmethod
    def create(cls, resources: Resources) -> Container:
        """Build the container from the process's infrastructure resources."""
        settings = resources.settings
        uow_factory = SqlAlchemyUnitOfWorkFactory(resources.database)
        locks = RedisLockManager(resources.cache.client, settings.redis)
        # Pricing is built before the purchase service because the purchase
        # service takes it as a collaborator: the discount and the bonus hold
        # have to commit in the same transaction as the purchase row.
        bonuses = BonusService(
            uow_factory=uow_factory,
            locks=locks,
            settings=settings.referral,
        )
        purchases = PurchaseService(
            uow_factory=uow_factory,
            locks=locks,
            invoice_ttl=timedelta(seconds=settings.cryptobot.invoice_ttl_seconds),
            pricing=bonuses,
        )
        return cls(
            settings=settings,
            uow_factory=uow_factory,
            locks=locks,
            rate_limiter=RedisRateLimiter(resources.cache.client),
            crypto_payments=CryptoBotClient(settings.cryptobot),
            products=ProductService(uow_factory=uow_factory, telegram=settings.telegram),
            purchases=purchases,
            referrals=ReferralService(
                uow_factory=uow_factory,
                telegram=settings.telegram,
                settings=settings.referral,
            ),
            bonuses=bonuses,
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
        notifier: RewardNotifier | None = None,
    ) -> CheckoutService:
        """Wire checkout to a concrete transport (bot process or admin API)."""
        return CheckoutService(
            purchases=self.purchases,
            delivery=self.build_delivery(delivery_gateway, notifier=notifier),
            stars=stars,
            crypto=self.crypto_payments,
        )

    def build_delivery(
        self,
        gateway: DeliveryGateway,
        *,
        notifier: RewardNotifier | None = None,
    ) -> DeliveryService:
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
            sale_completion=self.build_bonuses(notifier=notifier),
        )

    def build_bonuses(self, *, notifier: RewardNotifier | None = None) -> BonusService:
        """The bonus service, optionally able to tell an inviter they earned.

        The notifier lives in the bot process only, so the container keeps a
        transport-free instance and hands out a notifying one on request.
        """
        if notifier is None:
            return self.bonuses
        return replace(self.bonuses, notifier=notifier)
