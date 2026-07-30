"""Crypto Pay API client (CryptoBot).

Two responsibilities, both narrow:

* create an invoice when the buyer chose USDT, and read invoice states back for
  reconciliation;
* verify webhook signatures. Crypto Pay signs the raw body with
  ``HMAC-SHA256`` keyed by ``SHA256(api_token)``, so the signature is checked
  against the bytes as received — never against a re-serialised payload.
"""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import httpx

from app.core.exceptions import PaymentGatewayError
from app.core.logging import get_logger
from app.domain.payments import Invoice, PaymentState

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from app.core.config import CryptoBotSettings
    from app.domain.payments import InvoiceRequest

logger = get_logger(__name__)

SIGNATURE_HEADER: Final = "crypto-pay-api-signature"
TOKEN_HEADER: Final = "Crypto-Pay-API-Token"  # noqa: S105 — a header name
INVOICE_PAID_UPDATE: Final = "invoice_paid"

_STATE_BY_STATUS: Final[dict[str, PaymentState]] = {
    "active": PaymentState.PENDING,
    "paid": PaymentState.PAID,
    "expired": PaymentState.EXPIRED,
}
_STATES_PER_REQUEST: Final = 100


class CryptoBotClient:
    """Thin async client for the endpoints this shop actually uses."""

    def __init__(
        self,
        settings: CryptoBotSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._token = settings.api_token.get_secret_value()
        self._client = client or httpx.AsyncClient(
            base_url=settings.api_base_url,
            timeout=settings.request_timeout,
        )
        self._owns_client = client is None

    async def create_invoice(self, request: InvoiceRequest) -> Invoice:
        """Create a USDT invoice and return its id and payment URL."""
        result = await self._call(
            "createInvoice",
            {
                "currency_type": "crypto",
                "asset": self._settings.asset,
                "amount": str(Decimal(request.amount)),
                "description": request.title[:1024],
                "payload": request.payload,
                "expires_in": self._settings.invoice_ttl_seconds,
                "allow_comments": False,
                "allow_anonymous": False,
            },
        )
        invoice_id = result.get("invoice_id")
        pay_url = result.get("bot_invoice_url") or result.get("pay_url")
        if invoice_id is None or not isinstance(pay_url, str):
            message = "Crypto Pay returned an invoice without an id or a payment URL"
            raise PaymentGatewayError(message, response=str(result)[:200])

        logger.info(
            "crypto_invoice_created",
            invoice_id=str(invoice_id),
            amount=str(request.amount),
            asset=self._settings.asset,
            payload=request.payload,
        )
        return Invoice(external_id=str(invoice_id), pay_url=pay_url)

    async def fetch_states(self, external_ids: Sequence[str]) -> Mapping[str, PaymentState]:
        """Return the provider state of each invoice id that still exists."""
        states: dict[str, PaymentState] = {}
        for offset in range(0, len(external_ids), _STATES_PER_REQUEST):
            batch = external_ids[offset : offset + _STATES_PER_REQUEST]
            if not batch:
                continue
            result = await self._call("getInvoices", {"invoice_ids": ",".join(batch)})
            items = result.get("items")
            if not isinstance(items, list):  # pragma: no cover — defensive
                continue
            for item in items:
                if not isinstance(item, dict):  # pragma: no cover — defensive
                    continue
                invoice_id = item.get("invoice_id")
                status = item.get("status")
                if invoice_id is None or not isinstance(status, str):  # pragma: no cover
                    continue
                state = _STATE_BY_STATUS.get(status)
                if state is not None:
                    states[str(invoice_id)] = state
        return states

    def verify_signature(self, body: bytes, signature: str | None) -> bool:
        """Whether the raw webhook body carries a valid Crypto Pay signature."""
        if not signature:
            return False
        secret = hashlib.sha256(self._token.encode()).digest()
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def close(self) -> None:
        """Close the underlying HTTP client when this instance owns it."""
        if self._owns_client:
            await self._client.aclose()

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                f"/{method}",
                json=payload,
                headers={TOKEN_HEADER: self._token},
            )
        except httpx.HTTPError as error:
            logger.warning("crypto_pay_unreachable", method=method, error=str(error))
            message = "Crypto Pay is unreachable"
            raise PaymentGatewayError(message, method=method) from error

        if response.status_code >= httpx.codes.BAD_REQUEST:
            logger.warning(
                "crypto_pay_http_error",
                method=method,
                status_code=response.status_code,
            )
            message = f"Crypto Pay returned HTTP {response.status_code}"
            raise PaymentGatewayError(message, method=method)

        try:
            body = response.json()
        except ValueError as error:
            message = "Crypto Pay returned a malformed response"
            raise PaymentGatewayError(message, method=method) from error

        if not isinstance(body, dict) or not body.get("ok"):
            error_detail = body.get("error") if isinstance(body, dict) else None
            logger.warning("crypto_pay_api_error", method=method, error=str(error_detail))
            message = "Crypto Pay rejected the request"
            raise PaymentGatewayError(message, method=method, detail=str(error_detail)[:200])

        result = body.get("result")
        if not isinstance(result, dict):
            message = "Crypto Pay returned an unexpected result"
            raise PaymentGatewayError(message, method=method)
        return result
