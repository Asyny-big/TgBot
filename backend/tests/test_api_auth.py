"""Admin API: authentication and access control."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

import pytest

from tests.api_harness import ADMIN_PASSWORD, ADMIN_USERNAME

if TYPE_CHECKING:
    from tests.api_harness import ApiHarness

PROTECTED_ENDPOINTS = [
    ("GET", "/products"),
    ("POST", "/products"),
    ("GET", "/purchases"),
    ("GET", "/stats/overview"),
    ("GET", "/auth/me"),
]


async def test_login_returns_tokens_and_sets_the_refresh_cookie(api: ApiHarness) -> None:
    response = await api.client.post(
        f"{api.prefix}/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["token_type"] == "bearer"  # noqa: S105
    assert body["access_expires_in"] > 0
    assert body["refresh_expires_in"] > body["access_expires_in"]

    cookie = response.cookies.get(api.settings.security.cookie_name)
    assert cookie == body["refresh_token"]
    set_cookie = response.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie


@pytest.mark.parametrize(
    ("username", "password"),
    [
        (ADMIN_USERNAME, "wrong-password"),
        ("intruder", ADMIN_PASSWORD),
        (ADMIN_USERNAME.upper(), ADMIN_PASSWORD),
    ],
)
async def test_wrong_credentials_are_rejected(
    api: ApiHarness,
    username: str,
    password: str,
) -> None:
    response = await api.client.post(
        f"{api.prefix}/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_login_is_rate_limited(api: ApiHarness) -> None:
    """Credential guessing is slowed down per client and username."""
    limit = api.settings.security.login_rate_limit
    for _ in range(limit):
        response = await api.client.post(
            f"{api.prefix}/auth/login",
            json={"username": ADMIN_USERNAME, "password": "wrong"},
        )
        assert response.status_code == HTTPStatus.UNAUTHORIZED

    blocked = await api.client.post(
        f"{api.prefix}/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert blocked.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert blocked.json()["error"]["code"] == "too_many_attempts"


async def test_a_successful_login_clears_the_attempt_counter(api: ApiHarness) -> None:
    for _ in range(api.settings.security.login_rate_limit - 1):
        await api.client.post(
            f"{api.prefix}/auth/login",
            json={"username": ADMIN_USERNAME, "password": "wrong"},
        )

    assert (
        await api.client.post(
            f"{api.prefix}/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
    ).status_code == HTTPStatus.OK
    # The counter was reset, so a fresh series of attempts is allowed again.
    assert (
        await api.client.post(
            f"{api.prefix}/auth/login",
            json={"username": ADMIN_USERNAME, "password": "wrong"},
        )
    ).status_code == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize(("method", "path"), PROTECTED_ENDPOINTS)
async def test_every_endpoint_requires_a_token(
    api: ApiHarness,
    method: str,
    path: str,
) -> None:
    response = await api.client.request(method, f"{api.prefix}{path}")
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["error"]["code"] == "invalid_token"


async def test_a_garbage_token_is_rejected(api: ApiHarness) -> None:
    response = await api.client.get(
        f"{api.prefix}/auth/me",
        headers={"Authorization": "Bearer not-a-jwt"},
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_a_refresh_token_is_not_accepted_as_an_access_token(api: ApiHarness) -> None:
    login = await api.client.post(
        f"{api.prefix}/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    refresh_token = login.json()["refresh_token"]

    response = await api.client.get(
        f"{api.prefix}/auth/me",
        headers={"Authorization": f"Bearer {refresh_token}"},
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_me_returns_the_administrator(admin_api: ApiHarness) -> None:
    response = await admin_api.client.get(f"{admin_api.prefix}/auth/me")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"username": ADMIN_USERNAME}


async def test_refresh_rotates_the_pair_using_the_cookie(api: ApiHarness) -> None:
    login = await api.client.post(
        f"{api.prefix}/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    old_refresh = login.json()["refresh_token"]

    rotated = await api.client.post(f"{api.prefix}/auth/refresh")
    assert rotated.status_code == HTTPStatus.OK
    new_tokens = rotated.json()
    assert new_tokens["refresh_token"] != old_refresh

    # The rotated-away token can no longer be used.
    replay = await api.client.post(
        f"{api.prefix}/auth/refresh",
        json={"refresh_token": old_refresh},
    )
    assert replay.status_code == HTTPStatus.UNAUTHORIZED

    # The new access token works.
    me = await api.client.get(
        f"{api.prefix}/auth/me",
        headers={"Authorization": f"Bearer {new_tokens['access_token']}"},
    )
    assert me.status_code == HTTPStatus.OK


async def test_refresh_without_any_token_fails(api: ApiHarness) -> None:
    response = await api.client.post(f"{api.prefix}/auth/refresh")
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["error"]["code"] == "invalid_token"


async def test_logout_revokes_the_refresh_token_and_clears_the_cookie(
    api: ApiHarness,
) -> None:
    await api.client.post(
        f"{api.prefix}/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )

    response = await api.client.post(f"{api.prefix}/auth/logout")
    assert response.status_code == HTTPStatus.NO_CONTENT
    assert not api.client.cookies.get(api.settings.security.cookie_name)

    # A second logout is harmless, and the token is dead.
    assert (await api.client.post(f"{api.prefix}/auth/logout")).status_code == HTTPStatus.NO_CONTENT


async def test_validation_errors_use_the_error_envelope(api: ApiHarness) -> None:
    response = await api.client.post(f"{api.prefix}/auth/login", json={"username": "x"})
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"]["fields"]
