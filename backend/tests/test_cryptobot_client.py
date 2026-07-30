"""Crypto Pay client: request shape, error handling and webhook signatures."""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import CryptoBotSettings
from app.core.exceptions import PaymentGatewayError
from app.domain.enums import Currency
from app.domain.payments import InvoiceRequest, PaymentState
from app.infrastructure.payments.cryptobot import CryptoBotClient

if TYPE_CHECKING:
    from collections.abc import Callable

TOKEN = "12345:crypto-token"  # noqa: S105


def _settings(**overrides: Any) -> CryptoBotSettings:
    values: dict[str, Any] = {
        "api_token": SecretStr(TOKEN),
        "network": "testnet",
        "invoice_ttl_seconds": 900,
    }
    values.update(overrides)
    return CryptoBotSettings(**values)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> CryptoBotClient:
    settings = _settings()
    return CryptoBotClient(
        settings,
        client=httpx.AsyncClient(
            base_url=settings.api_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )


def _request(amount: Decimal | int = Decimal("5.00")) -> InvoiceRequest:
    return InvoiceRequest(
        user_id=1,
        product_id=uuid4(),
        title="VIP access",
        description="Lifetime access",
        amount=amount,
        currency=Currency.USDT,
        payload="payload-1",
    )


async def test_create_invoice_sends_the_expected_request() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("Crypto-Pay-API-Token")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "invoice_id": 4242,
                    "status": "active",
                    "bot_invoice_url": "https://t.me/CryptoBot?start=IV1",
                },
            },
        )

    invoice = await _client(handler).create_invoice(_request())

    assert invoice.external_id == "4242"
    assert invoice.pay_url == "https://t.me/CryptoBot?start=IV1"
    assert seen["url"].endswith("/createInvoice")
    assert seen["token"] == TOKEN
    assert seen["body"] == {
        "currency_type": "crypto",
        "asset": "USDT",
        "amount": "5.00",
        "description": "VIP access",
        "payload": "payload-1",
        "expires_in": 900,
        "allow_comments": False,
        "allow_anonymous": False,
    }


async def test_the_testnet_and_mainnet_endpoints_differ() -> None:
    assert "testnet" in _settings(network="testnet").api_base_url
    assert "testnet" not in _settings(network="mainnet").api_base_url


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"ok": False}),
        httpx.Response(200, json={"ok": False, "error": {"code": 400}}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"ok": True, "result": "unexpected"}),
        httpx.Response(200, json={"ok": True, "result": {"status": "active"}}),
    ],
)
async def test_a_bad_provider_response_becomes_a_gateway_error(
    response: httpx.Response,
) -> None:
    client = _client(lambda _: response)
    with pytest.raises(PaymentGatewayError):
        await client.create_invoice(_request())


async def test_an_unreachable_provider_becomes_a_gateway_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        error = httpx.ConnectError("connection refused", request=request)
        raise error

    with pytest.raises(PaymentGatewayError, match="unreachable"):
        await _client(handler).create_invoice(_request())


async def test_fetch_states_maps_provider_statuses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        requested = json.loads(request.content)["invoice_ids"].split(",")
        assert requested == ["1", "2", "3", "4"]
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "items": [
                        {"invoice_id": 1, "status": "active"},
                        {"invoice_id": 2, "status": "paid"},
                        {"invoice_id": 3, "status": "expired"},
                        {"invoice_id": 4, "status": "something-new"},
                    ]
                },
            },
        )

    states = await _client(handler).fetch_states(["1", "2", "3", "4"])

    assert states == {
        "1": PaymentState.PENDING,
        "2": PaymentState.PAID,
        "3": PaymentState.EXPIRED,
    }


async def test_fetch_states_of_nothing_calls_nobody() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        message = "the provider must not be called for an empty batch"
        raise AssertionError(message)

    assert await _client(handler).fetch_states([]) == {}


async def test_signature_verification() -> None:
    client = _client(lambda _: httpx.Response(200, json={"ok": True, "result": {}}))
    body = b'{"update_type":"invoice_paid"}'
    secret = hashlib.sha256(TOKEN.encode()).digest()
    signature = hmac.new(secret, body, hashlib.sha256).hexdigest()

    assert client.verify_signature(body, signature) is True
    # A tampered body invalidates the signature.
    assert client.verify_signature(body + b" ", signature) is False
    assert client.verify_signature(body, "deadbeef") is False
    assert client.verify_signature(body, None) is False
    assert client.verify_signature(body, "") is False


async def test_close_only_closes_a_client_it_owns() -> None:
    external = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    client = CryptoBotClient(_settings(), client=external)
    await client.close()
    assert external.is_closed is False
    await external.aclose()

    owned = CryptoBotClient(_settings())
    await owned.close()
