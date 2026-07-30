"""Product schemas.

The patch schema deliberately distinguishes *absent* from *null*: omitting
``photo_file_id`` leaves the photo alone, sending ``null`` removes it. Pydantic's
``model_fields_set`` carries that distinction into the domain's ``UNSET``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.api.schemas.common import ApiModel
from app.domain.commands import ProductDraft, ProductUpdate
from app.domain.slug import SLUG_MAX_LENGTH, SLUG_PATTERN

if TYPE_CHECKING:
    from app.domain.entities import Product

MAX_TITLE = 255
MAX_DESCRIPTION = 4096
MAX_URL = 2048
MAX_PHOTO_ID = 255
USDT_MAX = Decimal("1000000.00")
STARS_MAX = 1_000_000


class ProductCreateRequest(ApiModel):
    """Everything needed to put a product on sale."""

    slug: str = Field(pattern=SLUG_PATTERN, max_length=SLUG_MAX_LENGTH)
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    description: str = Field(default="", max_length=MAX_DESCRIPTION)
    delivery_url: str = Field(min_length=1, max_length=MAX_URL)
    photo_file_id: str | None = Field(default=None, max_length=MAX_PHOTO_ID)
    price_stars: int | None = Field(default=None, gt=0, le=STARS_MAX)
    price_usdt: Decimal | None = Field(default=None, gt=0, le=USDT_MAX, decimal_places=2)
    is_active: bool = True

    @model_validator(mode="after")
    def _at_least_one_price(self) -> Self:
        if self.price_stars is None and self.price_usdt is None:
            msg = "at least one of price_stars or price_usdt must be set"
            raise ValueError(msg)
        return self

    def to_draft(self) -> ProductDraft:
        """Map onto the domain command."""
        return ProductDraft(
            slug=self.slug,
            title=self.title,
            description=self.description,
            delivery_url=self.delivery_url,
            photo_file_id=self.photo_file_id,
            price_stars=self.price_stars,
            price_usdt=self.price_usdt,
            is_active=self.is_active,
        )


class ProductPatchRequest(ApiModel):
    """Partial update: only the fields present in the request are applied."""

    slug: str | None = Field(default=None, pattern=SLUG_PATTERN, max_length=SLUG_MAX_LENGTH)
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION)
    delivery_url: str | None = Field(default=None, min_length=1, max_length=MAX_URL)
    photo_file_id: str | None = Field(default=None, max_length=MAX_PHOTO_ID)
    price_stars: int | None = Field(default=None, gt=0, le=STARS_MAX)
    price_usdt: Decimal | None = Field(default=None, gt=0, le=USDT_MAX, decimal_places=2)
    is_active: bool | None = None

    _NON_NULLABLE = ("slug", "title", "description", "delivery_url", "is_active")

    @model_validator(mode="after")
    def _reject_nulls_where_meaningless(self) -> Self:
        """``null`` clears a nullable field; for the rest it is a mistake."""
        for name in self._NON_NULLABLE:
            if name in self.model_fields_set and getattr(self, name) is None:
                msg = f"{name} cannot be null"
                raise ValueError(msg)
        return self

    def to_update(self) -> ProductUpdate:
        """Map onto the domain command, preserving absent-versus-null."""
        supplied: dict[str, Any] = {name: getattr(self, name) for name in self.model_fields_set}
        return ProductUpdate(**supplied)

    @property
    def is_empty(self) -> bool:
        return not self.model_fields_set


class ProductResponse(ApiModel):
    """A product as the admin panel shows it, deep link included."""

    id: UUID
    slug: str
    title: str
    description: str
    photo_file_id: str | None
    delivery_url: str
    price_stars: int | None
    price_usdt: Decimal | None
    is_active: bool
    deep_link: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, product: Product, deep_link: str) -> ProductResponse:
        """Build the response from a domain entity."""
        return cls(
            id=product.id,
            slug=product.slug,
            title=product.title,
            description=product.description,
            photo_file_id=product.photo_file_id,
            delivery_url=product.delivery_url,
            price_stars=product.price_stars,
            price_usdt=product.price_usdt,
            is_active=product.is_active,
            deep_link=deep_link,
            created_at=product.created_at,
            updated_at=product.updated_at,
        )
