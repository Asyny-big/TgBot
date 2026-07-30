"""Admin API: product CRUD."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

if TYPE_CHECKING:
    from tests.api_harness import ApiHarness


def _payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "slug": "vip1",
        "title": "VIP access",
        "description": "Lifetime access",
        "delivery_url": "https://t.me/+private-invite",
        "price_stars": 150,
        "price_usdt": "5.00",
    }
    body.update(overrides)
    return body


async def _create(api: ApiHarness, **overrides: Any) -> dict[str, Any]:
    response = await api.client.post(f"{api.prefix}/products", json=_payload(**overrides))
    assert response.status_code == HTTPStatus.CREATED, response.text
    return dict(response.json())


async def test_create_returns_the_product_with_its_deep_link(admin_api: ApiHarness) -> None:
    product = await _create(admin_api, slug="vip1")

    assert product["slug"] == "vip1"
    assert product["price_stars"] == 150
    assert product["price_usdt"] == "5.00"
    assert product["is_active"] is True
    assert product["deep_link"] == "https://t.me/MyShopBot?start=vip1"
    assert product["created_at"].endswith("Z") or "+" in product["created_at"]


@pytest.mark.parametrize(
    "override",
    [
        {"slug": "not a slug"},
        {"slug": ""},
        {"title": ""},
        {"delivery_url": ""},
        {"price_stars": 0},
        {"price_usdt": "-1"},
        {"price_stars": None, "price_usdt": None},
        {"price_usdt": "1.234"},
        {"unknown_field": "x"},
    ],
)
async def test_invalid_payloads_are_rejected(
    admin_api: ApiHarness,
    override: dict[str, Any],
) -> None:
    response = await admin_api.client.post(
        f"{admin_api.prefix}/products",
        json=_payload(**override),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["error"]["code"] == "validation_error"


async def test_a_relative_delivery_url_is_refused_by_the_domain(admin_api: ApiHarness) -> None:
    response = await admin_api.client.post(
        f"{admin_api.prefix}/products",
        json=_payload(delivery_url="t.me/+invite"),
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["error"]["code"] == "invalid_delivery_url"


async def test_a_duplicate_slug_is_a_conflict(admin_api: ApiHarness) -> None:
    await _create(admin_api, slug="taken")
    response = await admin_api.client.post(
        f"{admin_api.prefix}/products",
        json=_payload(slug="taken"),
    )
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["error"]["code"] == "slug_already_exists"


async def test_listing_paginates_filters_and_searches(admin_api: ApiHarness) -> None:
    await _create(admin_api, slug="course1", title="Python course")
    await _create(admin_api, slug="course2", title="Rust course")
    await _create(admin_api, slug="pack18", title="Sticker pack", is_active=False)

    page = await admin_api.client.get(f"{admin_api.prefix}/products", params={"limit": 2})
    body = page.json()
    assert page.status_code == HTTPStatus.OK
    assert body["meta"] == {"total": 3, "limit": 2, "offset": 0, "has_more": True}
    assert len(body["items"]) == 2

    found = await admin_api.client.get(
        f"{admin_api.prefix}/products",
        params={"search": "python"},
    )
    assert [item["slug"] for item in found.json()["items"]] == ["course1"]

    active = await admin_api.client.get(
        f"{admin_api.prefix}/products",
        params={"is_active": "false"},
    )
    assert [item["slug"] for item in active.json()["items"]] == ["pack18"]


async def test_read_one_and_unknown_id(admin_api: ApiHarness) -> None:
    product = await _create(admin_api)

    response = await admin_api.client.get(f"{admin_api.prefix}/products/{product['id']}")
    assert response.status_code == HTTPStatus.OK
    assert response.json()["id"] == product["id"]

    missing = await admin_api.client.get(f"{admin_api.prefix}/products/{uuid4()}")
    assert missing.status_code == HTTPStatus.NOT_FOUND
    assert missing.json()["error"]["code"] == "product_not_found"

    malformed = await admin_api.client.get(f"{admin_api.prefix}/products/not-a-uuid")
    assert malformed.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_patch_touches_only_the_supplied_fields(admin_api: ApiHarness) -> None:
    product = await _create(admin_api, photo_file_id="photo-1")

    response = await admin_api.client.patch(
        f"{admin_api.prefix}/products/{product['id']}",
        json={"title": "Renamed"},
    )
    assert response.status_code == HTTPStatus.OK
    updated = response.json()
    assert updated["title"] == "Renamed"
    assert updated["slug"] == product["slug"]
    assert updated["photo_file_id"] == "photo-1"
    assert updated["price_stars"] == 150


async def test_patch_can_clear_a_nullable_field(admin_api: ApiHarness) -> None:
    product = await _create(admin_api, photo_file_id="photo-1")

    response = await admin_api.client.patch(
        f"{admin_api.prefix}/products/{product['id']}",
        json={"photo_file_id": None, "price_usdt": None},
    )
    assert response.status_code == HTTPStatus.OK
    updated = response.json()
    assert updated["photo_file_id"] is None
    assert updated["price_usdt"] is None
    assert updated["price_stars"] == 150


async def test_patch_cannot_null_a_required_field(admin_api: ApiHarness) -> None:
    product = await _create(admin_api)

    response = await admin_api.client.patch(
        f"{admin_api.prefix}/products/{product['id']}",
        json={"title": None},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_patch_cannot_remove_every_price(admin_api: ApiHarness) -> None:
    product = await _create(admin_api)

    response = await admin_api.client.patch(
        f"{admin_api.prefix}/products/{product['id']}",
        json={"price_stars": None, "price_usdt": None},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["error"]["code"] == "invalid_price"


async def test_an_empty_patch_is_a_no_op(admin_api: ApiHarness) -> None:
    product = await _create(admin_api)

    response = await admin_api.client.patch(
        f"{admin_api.prefix}/products/{product['id']}",
        json={},
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["updated_at"] == product["updated_at"]


async def test_patching_a_slug_updates_the_deep_link(admin_api: ApiHarness) -> None:
    product = await _create(admin_api, slug="before")

    response = await admin_api.client.patch(
        f"{admin_api.prefix}/products/{product['id']}",
        json={"slug": "after"},
    )
    assert response.json()["deep_link"] == "https://t.me/MyShopBot?start=after"


async def test_delete_removes_an_unsold_product(admin_api: ApiHarness) -> None:
    product = await _create(admin_api)

    response = await admin_api.client.delete(f"{admin_api.prefix}/products/{product['id']}")
    assert response.status_code == HTTPStatus.NO_CONTENT

    gone = await admin_api.client.get(f"{admin_api.prefix}/products/{product['id']}")
    assert gone.status_code == HTTPStatus.NOT_FOUND


async def test_delete_is_refused_for_a_sold_product(admin_api: ApiHarness) -> None:
    """History must survive: a sold product is deactivated, never deleted."""
    from app.domain.commands import UserDraft  # noqa: PLC0415
    from app.domain.enums import PaymentProvider  # noqa: PLC0415

    product = await _create(admin_api)
    product_id = product["id"]
    await admin_api.container.purchases.remember_user(
        UserDraft(telegram_id=7001, username="buyer", first_name="Buyer", language_code="ru")
    )
    from uuid import UUID  # noqa: PLC0415

    await admin_api.container.purchases.start_purchase(
        user_id=7001,
        product_id=UUID(product_id),
        provider=PaymentProvider.STARS,
        external_id="inv-sold",
    )

    response = await admin_api.client.delete(f"{admin_api.prefix}/products/{product_id}")
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["error"]["code"] == "conflict"

    # Deactivating works instead.
    deactivated = await admin_api.client.patch(
        f"{admin_api.prefix}/products/{product_id}",
        json={"is_active": False},
    )
    assert deactivated.json()["is_active"] is False
