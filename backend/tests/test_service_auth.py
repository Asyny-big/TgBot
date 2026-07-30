"""Unit tests for AuthService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from pydantic import SecretStr

from app.core.config import SecuritySettings
from app.core.exceptions import AuthenticationError, InvalidTokenError
from app.domain.auth import TokenType
from app.services.auth import AuthService
from tests.fakes import FakeRevocationStore

PASSWORD = "correct-horse-battery"  # noqa: S105
USERNAME = "administrator"


def _settings(**overrides: object) -> SecuritySettings:
    values: dict[str, object] = {
        "jwt_secret": SecretStr("s" * 48),
        "admin_username": USERNAME,
        "admin_password": SecretStr(PASSWORD),
        "access_token_ttl_minutes": 15,
        "refresh_token_ttl_days": 7,
    }
    values.update(overrides)
    return SecuritySettings(**values)  # type: ignore[arg-type]


@pytest.fixture
def revocations() -> FakeRevocationStore:
    return FakeRevocationStore()


@pytest.fixture
def service(revocations: FakeRevocationStore) -> AuthService:
    return AuthService(_settings(), revocations)


async def test_login_issues_a_usable_token_pair(service: AuthService) -> None:
    tokens = await service.login(USERNAME, PASSWORD)

    assert tokens.token_type == "bearer"  # noqa: S105
    assert tokens.access_expires_in == 15 * 60
    assert tokens.refresh_expires_in == 7 * 24 * 3600

    identity = await service.authenticate(tokens.access_token)
    assert identity.username == USERNAME
    assert identity.token_type is TokenType.ACCESS


async def test_the_plain_password_is_never_stored(service: AuthService) -> None:
    assert PASSWORD not in repr(vars(service))


@pytest.mark.parametrize(
    ("username", "password"),
    [
        (USERNAME, "wrong-password"),
        ("intruder", PASSWORD),
        ("", ""),
        (USERNAME.upper(), PASSWORD),
    ],
)
async def test_wrong_credentials_are_rejected(
    service: AuthService,
    username: str,
    password: str,
) -> None:
    with pytest.raises(AuthenticationError):
        await service.login(username, password)


async def test_a_unicode_username_is_rejected_not_crashed(service: AuthService) -> None:
    """Non-ASCII input must fail the credential check, not raise a TypeError."""
    with pytest.raises(AuthenticationError):
        await service.login("админ", PASSWORD)


async def test_a_refresh_token_cannot_be_used_as_an_access_token(service: AuthService) -> None:
    tokens = await service.login(USERNAME, PASSWORD)

    with pytest.raises(InvalidTokenError):
        await service.authenticate(tokens.refresh_token)

    identity = await service.authenticate(tokens.refresh_token, expected=TokenType.REFRESH)
    assert identity.username == USERNAME


async def test_refresh_rotates_and_invalidates_the_old_token(
    service: AuthService,
    revocations: FakeRevocationStore,
) -> None:
    tokens = await service.login(USERNAME, PASSWORD)

    rotated = await service.refresh(tokens.refresh_token)
    assert rotated.refresh_token != tokens.refresh_token
    assert len(revocations.revoked) == 1

    with pytest.raises(InvalidTokenError):
        await service.refresh(tokens.refresh_token)
    # The new token still works.
    await service.authenticate(rotated.access_token)


async def test_logout_revokes_the_refresh_token(service: AuthService) -> None:
    tokens = await service.login(USERNAME, PASSWORD)
    await service.logout(tokens.refresh_token)

    with pytest.raises(InvalidTokenError):
        await service.refresh(tokens.refresh_token)


async def test_a_tampered_signature_is_rejected(service: AuthService) -> None:
    forged = jwt.encode(
        {
            "sub": USERNAME,
            "type": TokenType.ACCESS.value,
            "jti": uuid4().hex,
            "iss": "tgshop-admin",
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        "x" * 48,  # a valid length key, but the wrong one
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        await service.authenticate(forged)


async def test_an_expired_token_is_rejected(revocations: FakeRevocationStore) -> None:
    settings = _settings()
    service = AuthService(settings, revocations)
    expired = jwt.encode(
        {
            "sub": USERNAME,
            "type": TokenType.ACCESS.value,
            "jti": uuid4().hex,
            "iss": "tgshop-admin",
            "iat": datetime.now(UTC) - timedelta(hours=2),
            "exp": datetime.now(UTC) - timedelta(hours=1),
        },
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        await service.authenticate(expired)


async def test_a_token_issued_for_another_admin_is_rejected(
    revocations: FakeRevocationStore,
) -> None:
    old_service = AuthService(_settings(admin_username="olduser"), revocations)
    tokens = await old_service.login("olduser", PASSWORD)

    renamed = AuthService(_settings(admin_username="newuser"), revocations)
    with pytest.raises(InvalidTokenError):
        await renamed.authenticate(tokens.access_token)


async def test_garbage_is_rejected(service: AuthService) -> None:
    for candidate in ("", "not-a-jwt", "a.b.c"):
        with pytest.raises(InvalidTokenError):
            await service.authenticate(candidate)
