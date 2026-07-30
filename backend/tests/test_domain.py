"""Pure domain unit tests — no database, no framework."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.core.exceptions import InvalidPriceError, InvalidSlugError
from app.domain.commands import ProductDraft, ProductUpdate
from app.domain.entities import Product, Purchase, User
from app.domain.enums import Currency, PaymentProvider, PurchaseStatus
from app.domain.pagination import MAX_PAGE_SIZE, Page, PageRequest
from app.domain.patch import UNSET, is_set
from app.domain.slug import is_valid_slug, normalise_slug
from app.domain.stats import StatsPeriod

NOW = datetime(2026, 7, 30, 12, 30, tzinfo=UTC)


def _product(**overrides: object) -> Product:
    values: dict[str, object] = {
        "id": uuid4(),
        "slug": "vip1",
        "title": "VIP",
        "description": "",
        "photo_file_id": None,
        "delivery_url": "https://example.com/file",
        "price_stars": 100,
        "price_usdt": Decimal("4.99"),
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return Product(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Slugs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", ["vip1", "pack18", "course", "a", "A-b_c", "x" * 64])
def test_valid_slugs_are_accepted(value: str) -> None:
    assert is_valid_slug(value)
    assert normalise_slug(f" {value} ") == value


@pytest.mark.parametrize("value", ["", "x" * 65, "vip 1", "vip/1", "приват", "vip!"])
def test_invalid_slugs_are_rejected(value: str) -> None:
    assert not is_valid_slug(value)
    with pytest.raises(InvalidSlugError):
        normalise_slug(value)


# --------------------------------------------------------------------------- #
# Products and providers
# --------------------------------------------------------------------------- #
def test_product_reports_supported_providers() -> None:
    both = _product()
    assert both.available_providers == (PaymentProvider.STARS, PaymentProvider.CRYPTO)

    stars_only = _product(price_usdt=None)
    assert stars_only.available_providers == (PaymentProvider.STARS,)
    assert stars_only.supports(PaymentProvider.STARS)
    assert not stars_only.supports(PaymentProvider.CRYPTO)
    assert stars_only.price_for(PaymentProvider.CRYPTO) is None

    crypto_only = _product(price_stars=None)
    assert crypto_only.available_providers == (PaymentProvider.CRYPTO,)
    assert crypto_only.price_for(PaymentProvider.CRYPTO) == Decimal("4.99")


def test_provider_currency_mapping() -> None:
    assert PaymentProvider.STARS.currency is Currency.XTR
    assert PaymentProvider.CRYPTO.currency is Currency.USDT


def test_product_draft_requires_at_least_one_price() -> None:
    with pytest.raises(InvalidPriceError):
        ProductDraft(
            slug="vip1",
            title="VIP",
            description="",
            delivery_url="https://example.com",
        )


def test_user_display_name_prefers_username() -> None:
    base: dict[str, Any] = {"created_at": NOW, "last_seen_at": NOW, "language_code": "ru"}
    assert User(telegram_id=1, username="nick", first_name="Nick", **base).display_name == "@nick"
    assert User(telegram_id=2, username=None, first_name="Nick", **base).display_name == "Nick"
    assert User(telegram_id=3, username=None, first_name=None, **base).display_name == "3"


@pytest.mark.parametrize(
    ("status", "grants"),
    [
        (PurchaseStatus.PENDING, False),
        (PurchaseStatus.PAID, True),
        (PurchaseStatus.DELIVERED, True),
        (PurchaseStatus.REFUNDED, False),
        (PurchaseStatus.EXPIRED, False),
    ],
)
def test_purchase_access_depends_on_status(status: PurchaseStatus, grants: bool) -> None:
    purchase = Purchase(
        id=uuid4(),
        user_id=1,
        product_id=uuid4(),
        provider=PaymentProvider.STARS,
        status=status,
        amount=Decimal("100"),
        currency=Currency.XTR,
        external_id="inv-1",
        telegram_charge_id=None,
        delivered_url=None,
        created_at=NOW,
        paid_at=None,
        delivered_at=None,
    )
    assert purchase.grants_access is grants
    assert status.grants_access is grants


# --------------------------------------------------------------------------- #
# Partial updates
# --------------------------------------------------------------------------- #
def test_unset_fields_are_excluded_from_updates() -> None:
    update = ProductUpdate(title="New title")
    assert update.changes() == {"title": "New title"}
    assert not update.is_empty


def test_explicit_none_clears_a_field() -> None:
    update = ProductUpdate(photo_file_id=None, price_stars=None)
    assert update.changes() == {"photo_file_id": None, "price_stars": None}


def test_empty_update_is_detected() -> None:
    assert ProductUpdate().is_empty
    assert ProductUpdate().changes() == {}


def test_is_set_narrows_maybe_values() -> None:
    assert is_set("value")
    assert is_set(None)
    assert not is_set(UNSET)
    assert not UNSET


# --------------------------------------------------------------------------- #
# Pagination
# --------------------------------------------------------------------------- #
def test_page_request_validates_bounds() -> None:
    assert PageRequest().limit > 0
    with pytest.raises(ValueError, match="limit must be between"):
        PageRequest(limit=0)
    with pytest.raises(ValueError, match="limit must be between"):
        PageRequest(limit=MAX_PAGE_SIZE + 1)
    with pytest.raises(ValueError, match="offset must not be negative"):
        PageRequest(offset=-1)


def test_page_reports_whether_more_rows_exist() -> None:
    assert Page(items=(1, 2), total=5, limit=2, offset=0).has_more
    assert not Page(items=(4, 5), total=5, limit=2, offset=3).has_more


# --------------------------------------------------------------------------- #
# Statistics windows
# --------------------------------------------------------------------------- #
def test_period_windows_are_day_aligned_in_utc() -> None:
    midnight = datetime(2026, 7, 30, tzinfo=UTC)
    assert StatsPeriod.TODAY.start(NOW) == midnight
    assert StatsPeriod.WEEK.start(NOW) == midnight - timedelta(days=6)
    assert StatsPeriod.MONTH.start(NOW) == midnight - timedelta(days=29)
    assert StatsPeriod.ALL.start(NOW) is None


def test_period_window_normalises_other_timezones() -> None:
    """02:30 in Moscow is still the previous day in UTC — the window follows UTC."""
    moscow_night = datetime(2026, 7, 30, 2, 30, tzinfo=ZoneInfo("Europe/Moscow"))
    assert StatsPeriod.TODAY.start(moscow_night) == datetime(2026, 7, 29, tzinfo=UTC)
