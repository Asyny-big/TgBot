"""Repository contracts.

Structural (``Protocol``) interfaces: the domain declares what it needs, the
infrastructure layer satisfies it without importing anything from here at
runtime. Services depend on these types only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime, timedelta
    from uuid import UUID

    from app.domain.commands import (
        BonusTransactionDraft,
        DailyContentDraft,
        DailyDeliveryDraft,
        DailyOutboxDraft,
        DailyRequestDraft,
        DailyRequestEventDraft,
        ProductDraft,
        ProductUpdate,
        PurchaseDraft,
        ReferralDraft,
        UserDraft,
    )
    from app.domain.daily import DeliveryCounts, LibraryStats
    from app.domain.daily_reports import (
        DailyDeliveryEntry,
        DailyDeliveryFilters,
        DailyLibraryEntry,
        DailyLibraryFilters,
        DailyOverviewCounts,
        DailyRequestEntry,
        DailyRequestFilters,
        DailySubscriberEntry,
        DailySubscriberFilters,
    )
    from app.domain.entities import (
        BonusTransaction,
        DailyAlert,
        DailyContent,
        DailyContentRequest,
        DailyDelivery,
        DailyDeliveryAttempt,
        DailyOutboxMessage,
        DailyRequestEvent,
        DailyRequestItem,
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
        DailyAlertKey,
        DailyAttemptOutcome,
        DailyContentStatus,
        DailyRequestStatus,
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
    from app.domain.patch import Maybe
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

    async def has_later_checkout(self, purchase: Purchase) -> bool:
        """Whether this buyer started a later purchase of the same product on the same rail.

        Later in the order ``pending_checkouts`` uses, and whatever its status:
        a later invoice replaced this one, paid or not.
        """
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

    async def received_active_counts(
        self,
        *,
        min_completed: int,
        only: tuple[int, ...] = (),
    ) -> dict[int, int]:
        """For every buyer ``deliverable`` would return: how many *active* items
        they hold or received — what their stock is counted down from."""
        ...

    async def mark_exhausted_notified(
        self,
        user_id: int,
        *,
        at: datetime,
    ) -> DailySubscription | None:
        """Record that the buyer was told they have seen everything, for now."""
        ...

    async def set_requests_ban(
        self,
        user_id: int,
        *,
        banned_by: str | None,
        at: datetime,
    ) -> DailySubscription | None:
        """Ban the buyer from sending content requests (``banned_by``), or lift it (``None``).

        Idempotent: banning a banned buyer keeps who banned them first and when.
        ``None`` when the buyer has no subscription.
        """
        ...


class DailyContentRepository(Protocol):
    """The Daily Content library. Items are archived, never deleted."""

    async def add(self, draft: DailyContentDraft) -> tuple[DailyContent, bool]:
        """Add an item; return it and whether this call created it.

        Idempotent per source message: the same message added again returns the
        item it already became.

        Raises:
            DailyContentDuplicateError: the same file, or the very same text,
                is already in the library and not archived.
        """
        ...

    async def get(self, content_id: int) -> DailyContent | None:
        """One item, or ``None``."""
        ...

    async def lock(self, content_id: int) -> DailyContent | None:
        """One item, locked until the end of the transaction; ``None`` if missing."""
        ...

    async def find_by_file(self, file_unique_id: str) -> DailyContent | None:
        """The item holding this file: the live one, else the newest archived one."""
        ...

    async def find_by_text(self, body: str) -> DailyContent | None:
        """The text item with exactly this body: the live one, else the newest archived."""
        ...

    async def stats(self) -> LibraryStats:
        """How many items there are, by status; the active ones by kind."""
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
                file, or the same text, into the library twice.
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

    async def added_since(self, since: datetime) -> int:
        """How many items were added from ``since`` on, whatever became of them."""
        ...

    async def count_available_for(self, user_id: int) -> int:
        """How many active items this buyer has neither received nor holds."""
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

    async def open_many(self, drafts: Sequence[DailyDeliveryDraft]) -> int:
        """Open many deliveries in one statement; return how many were new.

        Each is opened exactly as ``open`` would; a buyer who already has a
        delivery for that date is skipped, however many planners run at once.
        """
        ...

    async def lock_due(
        self,
        *,
        now: datetime,
        limit: int,
        activation_grace: timedelta | None = None,
    ) -> tuple[DailyDelivery, ...]:
        """Lock the deliveries that may be attempted now, skipping locked ones.

        Pending and no-content deliveries whose next attempt is due, and sending
        ones whose lease ran out — the attempt died — all before their deadline.

        ``activation_grace`` leaves alone an activation delivery nobody has
        attempted yet for that long after its moment: the bot is sending the
        welcome and then the item itself.
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

    async def finish_rejected(
        self,
        lease_token: UUID,
        *,
        at: datetime,
        outcome: DailyAttemptOutcome,
        error: str,
        next_attempt_at: datetime,
    ) -> DailyDelivery | None:
        """Telegram definitely refused the item: give it back, keep the day open.

        Only for a refusal that leaves no doubt the buyer got nothing: the item
        is released — offered again later — and the next attempt picks another
        one. ``None`` when the lease was taken over meanwhile.
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
        """Close an open delivery nobody is sending right now; ``None`` otherwise.

        "Nobody" includes an attempt whose lease ran out: it is closed as
        ``CRASHED`` together with the delivery.
        """
        ...

    async def expire_overdue(self, *, now: datetime, limit: int = 100) -> int:
        """Close open deliveries whose deadline passed; return how many.

        An attempt still unfinished there is closed as ``CRASHED``.
        """
        ...

    async def attempts_of(self, delivery_id: int) -> tuple[DailyDeliveryAttempt, ...]:
        """Every attempt of a delivery, in order."""
        ...

    async def count_sent(self, user_id: int) -> int:
        """How many items this buyer has received."""
        ...

    async def last_sent_at(self, user_id: int) -> datetime | None:
        """When this buyer last received an item, if ever."""
        ...

    async def outcome_counts(self, *, since: datetime) -> DeliveryCounts:
        """Deliveries closed from ``since`` on: sent, and failed for a reason
        other than the buyer blocking the bot or the library running dry."""
        ...


class DailyOutboxRepository(Protocol):
    """Messages the Daily runtime owes somebody: alerts, notices, answers to
    content requests, and items the admin panel sends to the owner.

    Sent like deliveries: a lease while an attempt is under way, every outcome
    written against that lease's token, a dead attempt taken over (so a
    message may arrive twice, never zero times before its deadline).
    """

    async def enqueue(self, draft: DailyOutboxDraft) -> DailyOutboxMessage | None:
        """Queue a message; ``None`` when its ``dedupe_key`` was queued before.

        A message may carry one item to send instead of its text: a library
        item (``content_id``) or a request's attachment (``request_item_id``).
        """
        ...

    async def get(self, message_id: int) -> DailyOutboxMessage | None:
        """One message, or ``None``."""
        ...

    async def find_by_key(self, dedupe_key: str) -> DailyOutboxMessage | None:
        """The message queued under this key, if any."""
        ...

    async def claim_due(
        self,
        *,
        now: datetime,
        limit: int,
        lease_until: datetime,
    ) -> tuple[DailyOutboxMessage, ...]:
        """Lease the messages due now, skipping rows another worker holds.

        Pending ones whose next attempt is due and sending ones whose lease ran
        out, before their deadline. Each comes back ``SENDING`` with a new lease
        token and one more attempt counted.
        """
        ...

    async def finish_sent(
        self,
        lease_token: UUID,
        *,
        at: datetime,
        telegram_message_id: int,
    ) -> DailyOutboxMessage | None:
        """Telegram took it. ``None`` when the lease is no longer this one's."""
        ...

    async def finish_retry(
        self,
        lease_token: UUID,
        *,
        at: datetime,
        next_attempt_at: datetime,
        error: str,
    ) -> DailyOutboxMessage | None:
        """An uncertain failure: back to pending, due again at ``next_attempt_at``."""
        ...

    async def finish_failed(
        self,
        lease_token: UUID,
        *,
        at: datetime,
        error: str,
    ) -> DailyOutboxMessage | None:
        """Refused for good, or out of attempts. Final."""
        ...

    async def expire_overdue(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> tuple[DailyOutboxMessage, ...]:
        """Drop the messages whose deadline passed unsent; return them, now expired."""
        ...


class DailyAlertRepository(Protocol):
    """The owner's alerts: one row of state each."""

    async def lock(self, key: DailyAlertKey) -> DailyAlert:
        """The alert, created ``OK`` if new, locked until the end of the transaction."""
        ...

    async def save(self, alert: DailyAlert) -> DailyAlert:
        """Write the locked alert's new state."""
        ...

    async def get(self, key: DailyAlertKey) -> DailyAlert | None:
        """The alert, or ``None`` before its first evaluation."""
        ...

    async def list_all(self) -> tuple[DailyAlert, ...]:
        """Every alert evaluated so far."""
        ...


class DailyRequestRepository(Protocol):
    """Buyers' content requests, their messages and their history.

    A request is created once per draft (``client_draft_id`` is unique), never
    deleted, and changed from the panel only against the version it was read
    at. A history line written under an idempotency key is written once.
    """

    async def create(
        self,
        draft: DailyRequestDraft,
        *,
        at: datetime,
    ) -> tuple[DailyContentRequest, bool]:
        """Create the request, its messages and its ``submitted`` line; return it
        and whether this call created it.

        Idempotent per ``client_draft_id``: the same draft again returns the
        request it already became.

        Raises:
            ConflictError: the draft became another buyer's request.
        """
        ...

    async def get(self, request_id: int) -> DailyContentRequest | None:
        """One request, or ``None``."""
        ...

    async def lock(self, request_id: int) -> DailyContentRequest | None:
        """One request, locked until the end of the transaction; ``None`` if missing."""
        ...

    async def find_by_draft(self, client_draft_id: UUID) -> DailyContentRequest | None:
        """The request this draft became, if it was sent."""
        ...

    async def items_of(self, request_id: int) -> tuple[DailyRequestItem, ...]:
        """The request's messages, in the buyer's order."""
        ...

    async def get_item(self, item_id: int) -> DailyRequestItem | None:
        """One message of a request, or ``None``."""
        ...

    async def events_of(self, request_id: int) -> tuple[DailyRequestEvent, ...]:
        """The request's history, oldest first."""
        ...

    async def count_open(self, user_id: int) -> int:
        """The buyer's requests still waiting for the owner: new or in review."""
        ...

    async def count_since(self, user_id: int, since: datetime) -> int:
        """The buyer's requests sent from ``since`` on, whatever became of them."""
        ...

    async def update(
        self,
        request_id: int,
        *,
        expected_version: int,
        at: datetime,
        status: Maybe[DailyRequestStatus],
        admin_note: Maybe[str | None],
    ) -> DailyContentRequest | None:
        """Apply the owner's change and move to the next version.

        Only when the request is still at ``expected_version``; ``None`` when it
        is not — somebody changed it since it was read — or does not exist.
        A move into completed or rejected stamps ``closed_at``; out of them,
        clears it.
        """
        ...

    async def add_event(
        self, draft: DailyRequestEventDraft, *, at: datetime
    ) -> DailyRequestEvent | None:
        """Add a line to the history; ``None`` when its idempotency key was used before."""
        ...

    async def find_event(self, idempotency_key: str) -> DailyRequestEvent | None:
        """The line written under this key, if any."""
        ...

    async def reply_of(self, outbox_message_id: int) -> DailyRequestEvent | None:
        """The ``reply_queued`` line of the answer this outbox message carries."""
        ...


class DailyReportRepository(Protocol):
    """What the admin panel reads about Daily Content. Reads only."""

    async def overview_counts(self, *, today: date, since: datetime) -> DailyOverviewCounts:
        """Subscriptions, today's deliveries, requests and the outbox, counted.

        ``since`` bounds the outbox failures counted as recent.
        """
        ...

    async def library(
        self,
        filters: DailyLibraryFilters,
        page: PageRequest,
    ) -> Page[DailyLibraryEntry]:
        """Library items, newest first."""
        ...

    async def library_entry(self, content_id: int) -> DailyLibraryEntry | None:
        """One library item with its numbers, or ``None``."""
        ...

    async def subscribers(
        self,
        filters: DailySubscriberFilters,
        page: PageRequest,
    ) -> Page[DailySubscriberEntry]:
        """Subscriptions, most recent first contact first."""
        ...

    async def subscriber(self, user_id: int) -> DailySubscriberEntry | None:
        """One subscription with its numbers, or ``None``."""
        ...

    async def deliveries(
        self,
        filters: DailyDeliveryFilters,
        page: PageRequest,
    ) -> Page[DailyDeliveryEntry]:
        """Deliveries, latest date first."""
        ...

    async def delivery(self, delivery_id: int) -> DailyDeliveryEntry | None:
        """One delivery with the buyer's name, or ``None``."""
        ...

    async def requests(
        self,
        filters: DailyRequestFilters,
        page: PageRequest,
    ) -> Page[DailyRequestEntry]:
        """Content requests, newest first."""
        ...

    async def request_entry(self, request_id: int) -> DailyRequestEntry | None:
        """One request as a row of the listing, or ``None``."""
        ...

    async def recent_deliveries(self, user_id: int, *, limit: int) -> tuple[DailyDelivery, ...]:
        """The buyer's latest deliveries, latest date first."""
        ...

    async def requests_of(self, user_id: int, *, limit: int) -> tuple[DailyContentRequest, ...]:
        """The buyer's latest requests, newest first."""
        ...
