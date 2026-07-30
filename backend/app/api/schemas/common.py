"""Shared API schema building blocks."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domain.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE


class ApiModel(BaseModel):
    """Base of every API schema: strict, forbidding unknown fields."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)


class PageMeta(ApiModel):
    """Pagination envelope shared by every listing endpoint."""

    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=MAX_PAGE_SIZE)
    offset: int = Field(ge=0)
    has_more: bool


class PageResponse[ItemT](ApiModel):
    """A page of results plus its metadata."""

    items: list[ItemT]
    meta: PageMeta


class PaginationParams(ApiModel):
    """Query parameters accepted by every listing endpoint."""

    limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    offset: int = Field(default=0, ge=0)
