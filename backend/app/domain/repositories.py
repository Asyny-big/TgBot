"""Repository contracts.

Structural (``Protocol``) interfaces: the domain declares what it needs, the
infrastructure layer satisfies it without importing anything from here at
runtime. Services depend on these types only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime
    from uuid import UUID

    from app.domain.commands import (
        BonusTransactionDraft,
        DailyContentDraft,
        DailyDeliveryDraft,
        ProductDraft,
        ProductUpdate,
        PurchaseDraft,
        ReferralDraft,
        UserDraft,
    )
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
    from app.domain.enums import (
        BonusTransactionType,
        DailyAttemptOutcome,
        DailyContentStatus,
        PaymentProvider,
        PurchaseStatus,
    )
    from app.domain.pagination import (
        Page,
        PageRequest,
        ProductFilters,
        PurchaseFilters,
        ReferralFilters,
    )
    from app.domain.stats import RevenueSummary, StatsPeriod, TopProduct


class ProductRepository(Protocol):
    """Persistence contract for products."""

    async def get(self, product_id: UUID) -> Product | None:
        """Return the product with this id, or ``None``."""
        ...

    async def get_by_slug(self, slug: str, *, only_active: bool = False) -> Product | None:
        """Return the product behind this deep-link slug, or ``None``."""
        ...

    async def list_products(self, filters: ProductFilters, page: PageRequest) -> Page[Product]:
        """Return a filtered, paginated slice of products, newest first."""
        ...

    async def create(self, draft: ProductDraft) -> Product:
        """Insert a product.

        Raises:
            SlugAlreadyExistsError: the slug is taken.
        """
        ...

    async def update(self, product_id: UUID, changes: ProductUpdate) -> Product:
        """Apply a partial update and return the stored product.

        Raises:
            ProductNotFoundError: no product with this id.
            SlugAlreadyExistsError: the new slug is taken.
        """
        ...

    async def delete(self, product_id: UUID) -> None:
        """Delete a product.

        Raises:
            ProductNotFoundError: no product with this id.
            ConflictError: the product has purchases and must be kept.
        """
        ...

    async def count(self, *, only_active: bool | None = None) -> int:
        """Number of products, optionally restricted by activity."""
        ...


class UserRepository(Protocol):
    """Persistence contract for Telegram users."""

    async def get(self, telegram_id: int) -> User | None:
        """Return the user with this Telegram id, or ``None``."""
        ...

    async def upsert(self, draft: UserDraft, *, seen_at: datetime | None = None) -> User:
        """Insert the user or refresh their profile snapshot and last-seen time."""
        ...

    async def count(self) -> int:
        """Total number of known users."""
        ...

    async def get_by_referral_code(self, code: str) -> User | None:
        """Return the owner of this referral code, or ``None``."""
        ...

    async def ensure_referral_code(self, telegram_id: int, *, candidate: str) -> str:
        """Return the user's permanent referral code, assigning one if needed.

        A user has exactly one code for life. ``candidate`` is used only when
        the user has none yet; a collision with an existing code is reported so
        the caller can offer another candidate.

        Raises:
            UserNotFoundError: the user was never recorded.
            ConflictError: ``candidate`` is already taken by somebody else.
        """
        ...


class PurchaseRepository(Protocol):
    """Persistence contract for purchases."""

    async def create(self, draft: PurchaseDraft) -> Purchase:
        """Insert a pending purchase.

        Raises:
            DuplicatePurchaseError: the buyer already owns this product, or the
                provider invoice id is already recorded.
        """
        ...

    async def get(self, purchase_id: UUID) -> Purchase | None:
        """Return the purchase with this id, or ``None``."""
        ...

    async def get_for_update(self, purchase_id: UUID) -> Purchase | None:
        """Return the purchase with its row locked until the transaction ends.

        Always read from the database, never from an earlier read in the same
        transaction: what it returns is what every later change will start from.
        """
        ...

    async def get_by_external_id(
        self,
        provider: PaymentProvider,
        external_id: str,
    ) -> Purchase | None:
        """Return the purchase behind a provider invoice id, or ``None``."""
        ...

    async def get_by_charge_id(self, telegram_charge_id: str) -> Purchase | None:
        """Return the purchase carrying this Telegram charge id, or ``None``."""
        ...

    async def find_access_granting(self, user_id: int, product_id: UUID) -> Purchase | None:
        """Return the paid or delivered purchase of this product by this user."""
        ...

    async def has_history(
        self,
        user_id: int,
        *,
        statuses: Sequence[PurchaseStatus],
    ) -> bool:
        """Whether this buyer has ever reached one of these statuses.

        Used to decide referral eligibility: a buyer with purchase history is
        not a new buyer, however new their Telegram account looks.
        """
        ...

    async def has_pending_discount(self, user_id: int) -> bool:
        """Whether this buyer has a pending purchase that already carries a discount.

        Closes the race window where two concurrent checkouts for different
        products both see the one-time referral discount as available: the
        second checkout refuses the discount when the first one already claimed
        it, even though the first has not been paid yet.

        Only ``PENDING`` is checked: paid/delivered/expired purchases are handled
        by ``has_history`` and ``discount_available`` on the referral row.
        """
        ...

    async def mark_paid(
        self,
        purchase_id: UUID,
        *,
        paid_at: datetime | None = None,
        telegram_charge_id: str | None = None,
    ) -> Purchase:
        """Move a pending purchase to ``paid``.

        Idempotent: an already paid or delivered purchase is returned unchanged.

        Raises:
            PurchaseNotFoundError: no purchase with this id.
            ConflictError: the purchase was refunded or expired.
        """
        ...

    async def mark_delivered(
        self,
        purchase_id: UUID,
        *,
        delivered_url: str,
        delivered_at: datetime | None = None,
    ) -> Purchase:
        """Record that the delivery link was sent. Idempotent.

        Raises:
            PurchaseNotFoundError: no purchase with this id.
            ConflictError: the purchase is not paid.
        """
        ...

    async def mark_refunded(self, purchase_id: UUID) -> Purchase:
        """Revoke access after a Telegram Stars refund.

        Raises:
            PurchaseNotFoundError: no purchase with this id.
        """
        ...

    async def expire_pending(self, older_than: datetime) -> int:
        """Mark stale pending purchases as expired; return how many changed."""
        ...

    async def expire(self, purchase_id: UUID) -> Purchase:
        """Move one pending purchase to ``expired``, under a row lock.

        Any other status is returned unchanged: an invoice that was paid in the
        meantime must never be expired by this call.

        Raises:
            PurchaseNotFoundError: no purchase with this id.
        """
        ...

    async def pending_checkouts(
        self,
        user_id: int,
        product_id: UUID,
        provider: PaymentProvider,
    ) -> tuple[Purchase, ...]:
        """This buyer's pending purchases of one product on one rail, newest first."""
        ...

    async def list_pending(
        self,
        provider: PaymentProvider,
        *,
        limit: int = 100,
    ) -> tuple[Purchase, ...]:
        """Pending purchases of one provider, oldest first (reconciliation)."""
        ...

    async def search(self, filters: PurchaseFilters, page: PageRequest) -> Page[PurchaseRecord]:
        """Search purchases by user, product, invoice or transaction id."""
        ...


class ReferralRepository(Protocol):
    """Persistence contract for referral relationships."""

    async def create(self, draft: ReferralDraft) -> Referral:
        """Attribute a buyer to their inviter, permanently.

        Raises:
            SelfReferralError: both sides are the same account.
            ReferralAlreadySetError: this buyer already has a referrer.
        """
        ...

    async def get(self, referral_id: UUID) -> Referral | None:
        """Return the relationship with this id, or ``None``."""
        ...

    async def get_by_referred(self, referred_user_id: int) -> Referral | None:
        """Return the relationship that owns this buyer, or ``None``."""
        ...

    async def lock_for_discount_check(self, referred_user_id: int) -> Referral | None:
        """Re-read the referral with ``SELECT ... FOR UPDATE``.

        Serialises concurrent discount-eligible checkouts at the database level:
        two transactions that both want to offer the one-time discount are forced
        to wait for each other on this row lock, so the second one sees the first
        one's pending purchase and refuses the discount.

        Called only when a preliminary (unlocked) read showed the discount might
        apply, so non-referred buyers and buyers who already used it never touch
        this lock.
        """
        ...

    async def mark_discount_used(
        self,
        referral_id: UUID,
        *,
        purchase_id: UUID,
        used_at: datetime | None = None,
    ) -> Referral:
        """Burn the one-time first-purchase discount. Idempotent.

        A second call with the *same* purchase is a replayed notification and
        changes nothing. A call with a different purchase is rejected: the
        discount exists once.

        Raises:
            ConflictError: the discount was already used by another purchase.
        """
        ...

    async def count_invited(self, referrer_user_id: int) -> int:
        """How many buyers this user has invited."""
        ...

    async def count_referral_purchases(self, referrer_user_id: int) -> int:
        """How many settled purchases this user's invitees have made."""
        ...

    async def search(self, filters: ReferralFilters, page: PageRequest) -> Page[ReferralRecord]:
        """Referral relationships with both parties, for the admin panel."""
        ...


class BonusRepository(Protocol):
    """Persistence contract for the bonus ledger and the cached balance.

    The ledger is the truth and the ``users.bonus_balance`` column is a cache of
    its sum. Both are written in the same transaction, so they cannot drift, and
    ``recompute_balance`` can always prove it.
    """

    async def balance(self, user_id: int) -> int:
        """Cached balance of this user, or ``0`` for an unknown user."""
        ...

    async def lock_balance(self, user_id: int) -> int:
        """Read the balance with ``SELECT ... FOR UPDATE``.

        Serialises concurrent spending inside the database, so two checkouts
        cannot both be told the same units are available.

        Raises:
            UserNotFoundError: the user was never recorded.
        """
        ...

    async def add(self, draft: BonusTransactionDraft) -> BonusTransaction | None:
        """Append an entry and move the cached balance in the same statement.

        Returns ``None`` when an entry of this type already exists for this
        purchase: that is a replayed notification, not an error.

        Raises:
            InsufficientBonusBalanceError: the entry would drive the balance
                below zero.
        """
        ...

    async def find_for_purchase(
        self,
        purchase_id: UUID,
        *types: BonusTransactionType,
    ) -> BonusTransaction | None:
        """Return the first matching entry recorded against this purchase."""
        ...

    async def settle_hold(self, purchase_id: UUID) -> BonusTransaction | None:
        """Turn a reservation into a spend. Idempotent, and never re-debits."""
        ...

    async def release_hold(self, purchase_id: UUID) -> BonusTransaction | None:
        """Give back a reservation for an invoice that was never paid.

        Idempotent: a reservation is released at most once, which the unique
        constraint on ``(purchase_id, type)`` guarantees.
        """
        ...

    async def recompute_balance(self, user_id: int) -> int:
        """Sum the ledger from scratch, ignoring the cache."""
        ...

    async def reward_total(self, user_id: int) -> int:
        """Total bonus units this user has ever earned from referrals."""
        ...

    async def purchases_awaiting_reward(
        self,
        *,
        reward_percent: int,
        limit: int = 100,
    ) -> tuple[UUID, ...]:
        """Delivered purchases by invited buyers that carry no reward yet.

        The safety net behind reward accrual: a process that died between
        marking a sale delivered and crediting the inviter is repaired here,
        the same way a lost payment webhook is repaired by reconciliation.

        Only purchases that earn at least one bonus at ``reward_percent`` are
        returned: one that earns nothing never gets an entry, and must not be
        offered again on every pass.
        """
        ...

    async def stale_holds(self, *, limit: int = 100) -> tuple[UUID, ...]:
        """Purchases whose reservation is dead and not yet released."""
        ...


class LoyaltyRepository(Protocol):
    """A buyer's loyalty progress, and the rewards recorded as they get used.

    Rewards are written lazily — ``ensure_reward`` the first time one is used —
    and every change of state is a method that checks the current state under
    the row lock, so no transition can happen twice or out of order.
    """

    async def count_completed(self, user_id: int) -> int:
        """Purchases this buyer received: delivered, or refunded after delivery.

        Pending, paid-but-undelivered, expired and refunded-before-delivery
        purchases are not counted. Every provider counts, including an
        ``internal`` free pack once it has been delivered.
        """
        ...

    async def rewards_of(self, user_id: int) -> tuple[LoyaltyReward, ...]:
        """Loyalty rewards recorded for this buyer so far, by milestone."""
        ...

    async def ensure_reward(self, user_id: int, milestone: int) -> None:
        """Record this milestone's reward as available, unless it already exists.

        Idempotent and race free: ``INSERT … ON CONFLICT DO NOTHING`` on the
        unique (user, milestone) pair. The caller checks the milestone is
        reached; this only makes sure the row is there to lock.
        """
        ...

    async def lock_reward(self, user_id: int, milestone: int) -> LoyaltyReward | None:
        """The reward row, locked ``FOR UPDATE`` until the transaction ends."""
        ...

    async def mark_applied(self, reward_id: UUID, purchase_id: UUID) -> LoyaltyReward:
        """``available → applied``: priced into this purchase.

        Raises:
            ConflictError: the reward is not available any more.
        """
        ...

    async def mark_consumed(self, reward_id: UUID, purchase_id: UUID) -> LoyaltyReward:
        """``available | applied → consumed`` by this purchase. Idempotent per purchase.

        Raises:
            ConflictError: another purchase already consumed it.
        """
        ...

    async def mark_released(self, reward_id: UUID, purchase_id: UUID) -> LoyaltyReward | None:
        """``applied → available`` if — and only if — it is applied to this purchase.

        ``None`` when it was not: already released, consumed, or used elsewhere.
        """
        ...

    async def release_expired(self, *, limit: int = 100) -> tuple[UUID, ...]:
        """Release every reward applied to a purchase that has since expired.

        One conditional ``UPDATE``: a released reward changes state, so it is
        never selected again, and rows a payment is settling right now are
        skipped rather than waited for.
        """
        ...

    async def lock_progress(self, user_id: int) -> None:
        """Serialise this buyer's deliveries until the transaction ends.

        Taken by every delivery while the programme is on, so each one counts
        the buyer's completed purchases with every earlier delivery committed —
        which makes that count the delivery's exact rank.
        """
        ...


class StatsRepository(Protocol):
    """Read-only aggregates for the admin dashboard."""

    async def revenue(self, period: StatsPeriod, *, now: datetime | None = None) -> RevenueSummary:
        """Revenue and volume inside a reporting window."""
        ...

    async def top_products(
        self,
        period: StatsPeriod,
        *,
        limit: int = 10,
        now: datetime | None = None,
    ) -> tuple[TopProduct, ...]:
        """Best selling products inside a reporting window."""
        ...

    async def recent_purchases(self, *, limit: int = 10) -> tuple[PurchaseRecord, ...]:
        """Latest paid or delivered purchases, newest first."""
        ...


# ------------------------------------------------------------- Daily Content


class DailySubscriptionRepository(Protocol):
    """Buyers' relationship with the Daily Content bot.

    Every change starts from the row locked ``FOR UPDATE`` and checks the
    status it starts from, so two updates of one buyer cannot interleave.
    """

    async def get(self, user_id: int) -> DailySubscription | None:
        """The buyer's subscription, or ``None`` if they never wrote to the bot."""
        ...

    async def lock(self, user_id: int) -> DailySubscription | None:
        """The subscription, locked until the transaction ends."""
        ...

    async def record_first_interaction(self, user_id: int, *, at: datetime) -> DailySubscription:
        """Create an inactive subscription unless one exists, and return it locked.

        Idempotent: a second call keeps the first interaction's time.
        """
        ...

    async def activate(
        self,
        user_id: int,
        *,
        at: datetime,
        anchor_minute: int,
    ) -> DailySubscription:
        """``inactive → active``, fixing the window's anchor for good.

        Raises:
            ConflictError: no subscription, or it is not inactive.
        """
        ...

    async def pause(self, user_id: int, *, at: datetime) -> DailySubscription:
        """``active → paused``. Idempotent for a paused one.

        Raises:
            ConflictError: no subscription, or it was never activated.
        """
        ...

    async def resume(self, user_id: int, *, at: datetime) -> DailySubscription:
        """``paused → active``. Idempotent for an active one.

        Raises:
            ConflictError: no subscription, or it was never activated.
        """
        ...

    async def mark_blocked(self, user_id: int, *, at: datetime) -> DailySubscription | None:
        """Record that the buyer blocked the bot; keeps the first time. ``None``: no row."""
        ...

    async def mark_unblocked(self, user_id: int) -> DailySubscription | None:
        """Clear the block. ``None``: no row."""
        ...

    async def deliverable(
        self,
        *,
        min_completed: int,
        after_user_id: int | None = None,
        limit: int = 100,
        only: tuple[int, ...] = (),
    ) -> tuple[DailySubscription, ...]:
        """Active, unblocked subscriptions of buyers who have the access.

        "Have the access" is the loyalty programme's own count of completed
        purchases — the same predicate, never a counter of its own. Ordered by
        buyer for keyset paging; ``only`` restricts it to those buyers.
        """
        ...


class DailyContentRepository(Protocol):
    """The Daily Content library. Items are archived, never deleted."""

    async def add(self, draft: DailyContentDraft) -> tuple[DailyContent, bool]:
        """Add an item; return it and whether this call created it.

        Idempotent per source message: the same message added again returns the
        item it already became.

        Raises:
            DailyContentDuplicateError: the same file is already in the library.
        """
        ...

    async def get(self, content_id: int) -> DailyContent | None:
        """One item, or ``None``."""
        ...

    async def set_status(
        self,
        content_id: int,
        status: DailyContentStatus,
        *,
        at: datetime,
        reason: str | None = None,
    ) -> DailyContent:
        """Move an item between active, disabled and archived. Idempotent.

        Raises:
            ConflictError: no such item.
            DailyContentDuplicateError: bringing it back would put the same
                file into the library twice.
        """
        ...

    async def pick_for(self, user_id: int) -> DailyContent | None:
        """A random active item this buyer has neither received nor holds.

        ``None`` when the buyer has received everything there is.
        """
        ...

    async def count_active(self) -> int:
        """How many items take part in deliveries."""
        ...


class DailyDeliveryRepository(Protocol):
    """Daily deliveries: the job and its record in one row.

    Who may send a delivery right now is decided by a lease: a random token
    stored on the row together with its expiry. Every outcome is written
    against that token, so an attempt whose lease was taken over can never
    overwrite what its successor did.
    """

    async def open(self, draft: DailyDeliveryDraft) -> DailyDelivery | None:
        """Create the buyer's delivery for the window's date.

        ``None`` when they already have one for that date: the database keeps
        it to one per buyer and date, however many planners run.
        """
        ...

    async def get(self, delivery_id: int) -> DailyDelivery | None:
        """One delivery, or ``None``."""
        ...

    async def for_date(self, user_id: int, delivery_date: date) -> DailyDelivery | None:
        """The buyer's delivery for a date, or ``None``."""
        ...

    async def lock(self, delivery_id: int) -> DailyDelivery | None:
        """One delivery, locked until the transaction ends."""
        ...

    async def lock_due(self, *, now: datetime, limit: int) -> tuple[DailyDelivery, ...]:
        """Lock the deliveries that may be attempted now, skipping locked ones.

        Pending and no-content deliveries whose next attempt is due, and sending
        ones whose lease ran out — the attempt died — all before their deadline.
        """
        ...

    async def start_attempt(
        self,
        delivery_id: int,
        *,
        content_id: int,
        now: datetime,
        lease_until: datetime,
    ) -> DailyDeliveryAttempt:
        """Take the lease and record a new attempt for this item.

        The item is fixed by the first attempt: later ones must name the same
        item. An attempt left unfinished by a dead process is closed as
        ``CRASHED`` first. The caller holds the row's lock.

        Raises:
            ConflictError: the delivery cannot be attempted (closed, or its
                lease is still live), or the item is held for this buyer
                elsewhere, or a retry names a different item.
        """
        ...

    async def finish_sent(
        self,
        lease_token: UUID,
        *,
        at: datetime,
        telegram_message_id: int,
    ) -> DailyDelivery | None:
        """The attempt holding ``lease_token`` was accepted by Telegram.

        ``None`` when the lease was taken over meanwhile: the attempt is
        recorded as ``LEASE_LOST`` and the delivery is left to its new owner.
        """
        ...

    async def finish_retry(  # noqa: PLR0913 — one outcome, this many facts
        self,
        lease_token: UUID,
        *,
        at: datetime,
        outcome: DailyAttemptOutcome,
        error: str,
        next_attempt_at: datetime,
        retry_after_seconds: int | None = None,
    ) -> DailyDelivery | None:
        """The attempt failed for now: release the lease, retry the same item later.

        ``None`` when the lease was taken over meanwhile (see ``finish_sent``).
        """
        ...

    async def finish_failed(
        self,
        lease_token: UUID,
        *,
        at: datetime,
        outcome: DailyAttemptOutcome,
        error: str,
    ) -> DailyDelivery | None:
        """The attempt failed for good: the delivery is closed as failed.

        The item is given back: it was not delivered. ``None`` when the lease
        was taken over meanwhile.
        """
        ...

    async def mark_no_content(
        self,
        delivery_id: int,
        *,
        at: datetime,
        recheck_at: datetime,
    ) -> DailyDelivery:
        """Nothing left to send this buyer: look again at ``recheck_at``.

        Raises:
            ConflictError: the delivery holds an item or is not open.
        """
        ...

    async def cancel(self, delivery_id: int, *, at: datetime) -> DailyDelivery | None:
        """Close an open delivery nobody is sending right now; ``None`` otherwise."""
        ...

    async def expire_overdue(self, *, now: datetime, limit: int = 100) -> int:
        """Close open deliveries whose deadline passed; return how many.

        An attempt still unfinished there is closed as ``CRASHED``.
        """
        ...

    async def attempts_of(self, delivery_id: int) -> tuple[DailyDeliveryAttempt, ...]:
        """Every attempt of a delivery, in order."""
        ...
