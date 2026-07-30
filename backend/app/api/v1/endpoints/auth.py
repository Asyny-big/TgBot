"""Administrator authentication endpoints.

The access token is returned in the body for the SPA to keep in memory; the
refresh token is additionally set as an httpOnly cookie so it is not reachable
from JavaScript. Login attempts are rate limited per client address.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Request, Response, status

from app.api.deps import ContainerDep, CurrentAdmin, SettingsDep
from app.api.schemas.auth import AdminResponse, LoginRequest, RefreshRequest, TokenResponse
from app.core.exceptions import AppError, InvalidTokenError
from app.core.logging import get_logger
from app.domain.auth import TokenPair

router = APIRouter(prefix="/auth", tags=["auth"])

logger = get_logger(__name__)


class TooManyLoginAttemptsError(AppError):
    """The caller has tried too many passwords in a short window."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "too_many_attempts"
    message = "Too many login attempts, please wait and try again"


def _client_key(request: Request, username: str) -> str:
    host = request.client.host if request.client else "unknown"
    return f"login:{host}:{username.lower()}"


def _set_refresh_cookie(response: Response, tokens: TokenPair, settings: SettingsDep) -> None:
    response.set_cookie(
        key=settings.security.cookie_name,
        value=tokens.refresh_token,
        max_age=tokens.refresh_expires_in,
        httponly=True,
        secure=settings.security.cookie_secure,
        samesite="strict",
        path=settings.app.api_prefix,
    )


@router.post("/login", response_model=TokenResponse, summary="Exchange credentials for tokens")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    container: ContainerDep,
    settings: SettingsDep,
) -> TokenResponse:
    """Authenticate the administrator.

    Raises:
        TooManyLoginAttemptsError: the rate limit for this client was exceeded.
        AuthenticationError: wrong username or password.
    """
    limiter = container.rate_limiter
    key = _client_key(request, payload.username)
    if not await limiter.hit(
        key,
        limit=settings.security.login_rate_limit,
        window_seconds=settings.security.login_rate_window_seconds,
    ):
        raise TooManyLoginAttemptsError

    tokens = await container.auth.login(payload.username, payload.password)
    await limiter.reset(key)
    _set_refresh_cookie(response, tokens, settings)
    return TokenResponse.model_validate(tokens, from_attributes=True)


@router.post("/refresh", response_model=TokenResponse, summary="Rotate the refresh token")
async def refresh(
    request: Request,
    response: Response,
    container: ContainerDep,
    settings: SettingsDep,
    payload: Annotated[RefreshRequest | None, Body()] = None,
) -> TokenResponse:
    """Issue a new token pair and invalidate the presented refresh token.

    Raises:
        InvalidTokenError: no token supplied, or it is invalid, expired or revoked.
    """
    token = (payload.refresh_token if payload else None) or request.cookies.get(
        settings.security.cookie_name
    )
    if not token:
        message = "No refresh token was supplied"
        raise InvalidTokenError(message)

    tokens = await container.auth.refresh(token)
    _set_refresh_cookie(response, tokens, settings)
    return TokenResponse.model_validate(tokens, from_attributes=True)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the refresh token",
)
async def logout(
    request: Request,
    response: Response,
    container: ContainerDep,
    settings: SettingsDep,
    payload: Annotated[RefreshRequest | None, Body()] = None,
) -> None:
    """Revoke the refresh token and clear the cookie. Idempotent for a stale token."""
    token = (payload.refresh_token if payload else None) or request.cookies.get(
        settings.security.cookie_name
    )
    response.delete_cookie(
        key=settings.security.cookie_name,
        path=settings.app.api_prefix,
        httponly=True,
        secure=settings.security.cookie_secure,
        samesite="strict",
    )
    if not token:
        return
    try:
        await container.auth.logout(token)
    except InvalidTokenError:
        # Already expired or revoked: the caller is logged out either way.
        logger.info("logout_with_stale_token")


@router.get("/me", response_model=AdminResponse, summary="Who am I")
async def me(admin: CurrentAdmin) -> AdminResponse:
    """Return the authenticated administrator."""
    return AdminResponse(username=admin.username)
