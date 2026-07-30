"""Distributed lock contract.

Locks are always bounded in time: a crashed process must never leave a buyer
permanently unable to purchase. The implementation stores the lock with an
expiry, and the holder releases it only if the stored token is still its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager
    from uuid import UUID

    from app.domain.enums import PaymentProvider

DEFAULT_LOCK_TTL_SECONDS: Final = 45.0
"""Long enough for an invoice round trip, short enough to self-heal quickly."""


class LockManager(Protocol):
    """Acquires short lived, automatically expiring distributed locks."""

    def lock(
        self,
        key: str,
        *,
        ttl_seconds: float | None = None,
        wait_seconds: float = 0.0,
    ) -> AbstractAsyncContextManager[None]:
        """Hold ``key`` for at most ``ttl_seconds``.

        Args:
            key: Logical resource name.
            ttl_seconds: Expiry of the lock; the configured default when omitted.
                The lock disappears on its own once this elapses, even if the
                holder died.
            wait_seconds: How long to keep retrying before giving up.

        Raises:
            LockBusyError: The lock is held elsewhere and did not free up in time.
        """
        ...


def purchase_lock_key(user_id: int, product_id: UUID) -> str:
    """Serialise invoice creation per buyer and product."""
    return f"purchase:{user_id}:{product_id}"


def delivery_lock_key(purchase_id: UUID) -> str:
    """Serialise delivery attempts for one purchase."""
    return f"delivery:{purchase_id}"


def payment_lock_key(provider: PaymentProvider, external_id: str) -> str:
    """Serialise payment confirmations for one provider invoice."""
    return f"payment:{provider.value}:{external_id}"
