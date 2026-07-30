"""Webhook endpoints served by the bot process.

Both providers post here, and both are authenticated before anything is read:

* Telegram sends its secret token in ``X-Telegram-Bot-Api-Secret-Token``
  (checked by aiogram's request handler);
* Crypto Pay signs the raw body, which is verified against
  ``HMAC-SHA256(SHA256(api_token), body)`` before the JSON is parsed.

Every handler answers ``200`` as soon as the payment is recorded. A provider that
retries a notification must find an idempotent endpoint, not an error.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import Any

from aiohttp import web

from app.core.exceptions import AppError, LockBusyError, PurchaseNotFoundError
from app.core.logging import get_logger
from app.domain.enums import PaymentProvider
from app.infrastructure.payments.cryptobot import (
    INVOICE_PAID_UPDATE,
    SIGNATURE_HEADER,
    CryptoBotClient,
)
from app.services.checkout import CheckoutService

logger = get_logger(__name__)

CRYPTO_CLIENT_KEY: web.AppKey[CryptoBotClient] = web.AppKey("crypto_client")
CHECKOUT_KEY: web.AppKey[CheckoutService] = web.AppKey("checkout")


def _extract_invoice_id(payload: dict[str, Any]) -> str | None:
    invoice = payload.get("payload")
    if not isinstance(invoice, dict):
        return None
    invoice_id = invoice.get("invoice_id")
    return None if invoice_id is None else str(invoice_id)


async def handle_cryptobot_webhook(request: web.Request) -> web.Response:
    """Settle a paid CryptoBot invoice. Idempotent and signature checked."""
    client = request.app[CRYPTO_CLIENT_KEY]
    checkout = request.app[CHECKOUT_KEY]

    body = await request.read()
    signature = request.headers.get(SIGNATURE_HEADER)
    if not client.verify_signature(body, signature):
        logger.warning("cryptobot_webhook_bad_signature", body_size=len(body))
        return web.json_response({"ok": False}, status=HTTPStatus.UNAUTHORIZED)

    try:
        payload = json.loads(body)
    except ValueError:
        logger.warning("cryptobot_webhook_malformed_body")
        return web.json_response({"ok": False}, status=HTTPStatus.BAD_REQUEST)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False}, status=HTTPStatus.BAD_REQUEST)

    update_type = payload.get("update_type")
    if update_type != INVOICE_PAID_UPDATE:
        # Other update types are acknowledged and ignored on purpose.
        logger.info("cryptobot_webhook_ignored", update_type=str(update_type))
        return web.json_response({"ok": True})

    invoice_id = _extract_invoice_id(payload)
    if invoice_id is None:
        logger.warning("cryptobot_webhook_without_invoice_id")
        return web.json_response({"ok": False}, status=HTTPStatus.BAD_REQUEST)

    try:
        result = await checkout.settle_payment(
            provider=PaymentProvider.CRYPTO,
            external_id=invoice_id,
        )
    except LockBusyError:
        # A concurrent notification or the reconciliation loop already has it.
        logger.info("cryptobot_webhook_already_processing", invoice_id=invoice_id)
        return web.json_response({"ok": True})
    except PurchaseNotFoundError:
        logger.error(  # noqa: TRY400 — unknown invoice paid: alert, but acknowledge
            "cryptobot_webhook_unknown_invoice",
            invoice_id=invoice_id,
        )
        return web.json_response({"ok": True})
    except AppError as error:
        logger.error(  # noqa: TRY400 — never make the provider retry a business conflict
            "cryptobot_webhook_settlement_failed",
            invoice_id=invoice_id,
            error_code=error.code,
        )
        return web.json_response({"ok": True})

    logger.info(
        "cryptobot_webhook_processed",
        invoice_id=invoice_id,
        delivery_status=result.status.value,
    )
    return web.json_response({"ok": True})


def register_cryptobot_webhook(
    app: web.Application,
    *,
    path: str,
    client: CryptoBotClient,
    checkout: CheckoutService,
) -> None:
    """Mount the CryptoBot webhook route with its dependencies."""
    app[CRYPTO_CLIENT_KEY] = client
    app[CHECKOUT_KEY] = checkout
    app.router.add_post(path, handle_cryptobot_webhook)
