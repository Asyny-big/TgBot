"""Pagination primitives shared by repositories and the admin API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

DEFAULT_PAGE_SIZE: Final = 20
MAX_PAGE_SIZE: Final = 100


@dataclass(frozen=True, slots=True)
class PageRequest:
    """Offset based slice of a result set."""

    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_PAGE_SIZE:
            msg = f"limit must be between 1 and {MAX_PAGE_SIZE}, got {self.limit}"
            raise ValueError(msg)
        if self.offset < 0:
            msg = f"offset must not be negative, got {self.offset}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class Page[ItemT]:
    """A slice of results plus the total number of matching rows."""

    items: tuple[ItemT, ...] = ()
    total: int = 0
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0

    @property
    def has_more(self) -> bool:
        """Whether further rows exist after this slice."""
        return self.offset + len(self.items) < self.total


@dataclass(frozen=True, slots=True)
class ProductFilters:
    """Filters accepted by the product listing query."""

    search: str | None = None
    is_active: bool | None = None


@dataclass(frozen=True, slots=True)
class PurchaseFilters:
    """Filters accepted by the purchase search query.

    ``search`` matches Telegram id, username, product title, product slug,
    invoice id (``external_id``) and Telegram transaction id.
    """

    search: str | None = None
    statuses: tuple[str, ...] = field(default_factory=tuple)
