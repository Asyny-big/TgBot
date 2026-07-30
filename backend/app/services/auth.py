"""Administrator authentication.

The single administrator's credentials come from the environment. The password is
hashed with argon2 once at start-up and compared against that hash, so the plain
value is never used in a comparison and never reaches a log line.

Access tokens are short lived; refresh tokens can be revoked (logout) through a
store that only has to remember them until they would expire anyway.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from uuid import uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

from app.core.exceptions import AuthenticationError, InvalidTokenError
from app.core.logging import get_logger
from app.domain.auth import AdminIdentity, TokenPair, TokenType

if TYPE_CHECKING:
    from app.core.config import SecuritySettings
    from app.domain.auth import TokenRevocationStore

logger = get_logger(__name__)

_ISSUER: Final = "tgshop-admin"


def _constant_time_equals(left: str, right: str) -> bool:
    """Compare two strings without leaking their difference through timing.

    ``secrets.compare_digest`` rejects non-ASCII ``str`` inputs with a TypeError,
    so both sides are encoded first: a Unicode username must fail the check, not
    crash the request.
    """
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


class AuthService:
    """Issues, refreshes, revokes and validates administrator tokens."""

    def __init__(
        self,
        settings: SecuritySettings,
        revocations: TokenRevocationStore,
        *,
        hasher: PasswordHasher | None = None,
    ) -> None:
        self._settings = settings
        self._revocations = revocations
        self._hasher = hasher or PasswordHasher()
        # Hash once: every later login compares against this digest.
        self._password_hash = self._hasher.hash(settings.admin_password.get_secret_value())

    async def login(self, username: str, password: str) -> TokenPair:
        """Exchange credentials for a token pair.

        Raises:
            AuthenticationError: username or password does not match.
        """
        username_ok = _constant_time_equals(username, self._settings.admin_username)
        password_ok = self._verify_password(password)
        # Both checks always run, so a wrong username and a wrong password take
        # the same time and cannot be told apart by an attacker.
        if not (username_ok and password_ok):
            logger.warning("admin_login_failed", username=username)
            raise AuthenticationError
        logger.info("admin_login_succeeded", username=username)
        return self._issue_pair(self._settings.admin_username)

    async def refresh(self, refresh_token: str) -> TokenPair:
        """Rotate a refresh token into a new pair, invalidating the old one.

        Raises:
            InvalidTokenError: the token is invalid, expired, revoked or not a
                refresh token.
        """
        identity = await self.authenticate(refresh_token, expected=TokenType.REFRESH)
        await self._revoke(identity.token_id, TokenType.REFRESH)
        logger.info("admin_token_refreshed", username=identity.username)
        return self._issue_pair(identity.username)

    async def logout(self, refresh_token: str) -> None:
        """Revoke a refresh token so it can no longer be rotated."""
        identity = await self.authenticate(refresh_token, expected=TokenType.REFRESH)
        await self._revoke(identity.token_id, TokenType.REFRESH)
        logger.info("admin_logged_out", username=identity.username)

    async def authenticate(
        self,
        token: str,
        *,
        expected: TokenType = TokenType.ACCESS,
    ) -> AdminIdentity:
        """Validate a token and return the administrator behind it.

        Raises:
            InvalidTokenError: signature, expiry, type or revocation check failed.
        """
        payload = self._decode(token)
        token_type = payload.get("type")
        if token_type != expected.value:
            raise InvalidTokenError(expected=expected.value, actual=str(token_type))

        token_id = payload.get("jti")
        subject = payload.get("sub")
        if not isinstance(token_id, str) or not isinstance(subject, str):
            raise InvalidTokenError

        if await self._revocations.is_revoked(token_id):
            logger.warning("revoked_token_used", token_id=token_id)
            raise InvalidTokenError

        if not _constant_time_equals(subject, self._settings.admin_username):
            # The administrator username changed since the token was issued.
            raise InvalidTokenError

        return AdminIdentity(username=subject, token_id=token_id, token_type=expected)

    def _verify_password(self, password: str) -> bool:
        try:
            return self._hasher.verify(self._password_hash, password)
        except (VerifyMismatchError, VerificationError):
            return False

    def _decode(self, token: str) -> dict[str, Any]:
        try:
            payload = jwt.decode(
                token,
                self._settings.jwt_secret.get_secret_value(),
                algorithms=[self._settings.jwt_algorithm],
                issuer=_ISSUER,
                options={"require": ["exp", "iat", "sub", "jti"]},
            )
        except jwt.PyJWTError as error:
            raise InvalidTokenError(reason=type(error).__name__) from error
        if not isinstance(payload, dict):  # pragma: no cover — PyJWT always returns a dict
            raise InvalidTokenError
        return payload

    def _issue_pair(self, username: str) -> TokenPair:
        access_ttl = timedelta(minutes=self._settings.access_token_ttl_minutes)
        refresh_ttl = timedelta(days=self._settings.refresh_token_ttl_days)
        issued_at = datetime.now(UTC)
        return TokenPair(
            access_token=self._encode(username, TokenType.ACCESS, access_ttl, issued_at),
            refresh_token=self._encode(username, TokenType.REFRESH, refresh_ttl, issued_at),
            access_expires_in=int(access_ttl.total_seconds()),
            refresh_expires_in=int(refresh_ttl.total_seconds()),
        )

    def _encode(
        self,
        username: str,
        token_type: TokenType,
        ttl: timedelta,
        issued_at: datetime,
    ) -> str:
        payload = {
            "sub": username,
            "type": token_type.value,
            "jti": uuid4().hex,
            "iss": _ISSUER,
            "iat": issued_at,
            "exp": issued_at + ttl,
        }
        return jwt.encode(
            payload,
            self._settings.jwt_secret.get_secret_value(),
            algorithm=self._settings.jwt_algorithm,
        )

    async def _revoke(self, token_id: str, token_type: TokenType) -> None:
        ttl = (
            timedelta(days=self._settings.refresh_token_ttl_days)
            if token_type is TokenType.REFRESH
            else timedelta(minutes=self._settings.access_token_ttl_minutes)
        )
        await self._revocations.revoke(token_id, ttl_seconds=ttl.total_seconds())
