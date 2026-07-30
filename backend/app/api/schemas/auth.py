"""Authentication schemas."""

from __future__ import annotations

from pydantic import Field

from app.api.schemas.common import ApiModel


class LoginRequest(ApiModel):
    """Administrator credentials."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(ApiModel):
    """Body fallback for clients that cannot use the refresh cookie."""

    refresh_token: str | None = Field(default=None, max_length=4096)


class TokenResponse(ApiModel):
    """Issued credentials. The refresh token is also set as an httpOnly cookie."""

    access_token: str
    refresh_token: str
    token_type: str
    access_expires_in: int
    refresh_expires_in: int


class AdminResponse(ApiModel):
    """Who the caller is."""

    username: str
