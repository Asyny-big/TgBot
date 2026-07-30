"""Administrator authentication value objects and ports."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class TokenType(StrEnum):
    """Kind of JWT; a refresh token must never be accepted as an access token."""

    ACCESS = "access"
    REFRESH = "refresh"


@dataclass(frozen=True, slots=True, kw_only=True)
class AdminIdentity:
    """The authenticated administrator behind a request."""

    username: str
    token_id: str
    token_type: TokenType


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenPair:
    """Freshly issued credentials for the admin panel."""

    access_token: str
    refresh_token: str
    access_expires_in: int
    refresh_expires_in: int
    token_type: str = "bearer"  # noqa: S105 — a scheme name, not a secret


class TokenRevocationStore(Protocol):
    """Remembers refresh tokens that must no longer be accepted."""

    async def revoke(self, token_id: str, *, ttl_seconds: float) -> None:
        """Block a token id until its natural expiry has passed."""
        ...

    async def is_revoked(self, token_id: str) -> bool:
        """Whether this token id was revoked."""
        ...
