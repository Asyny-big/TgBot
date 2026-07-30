"""Application level exception hierarchy.

Errors carry an HTTP status and a stable machine readable ``code`` so that both
the admin SPA and the bot can react to them without string matching.
"""

from __future__ import annotations

from http import HTTPStatus


class AppError(Exception):
    """Base class for every deliberate, expected application failure."""

    status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "Internal server error"

    def __init__(self, message: str | None = None, /, **details: object) -> None:
        self.message = message or self.message
        self.details: dict[str, object] = dict(details)
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message


class ServiceUnavailableError(AppError):
    """A required infrastructure dependency is not reachable."""

    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    code = "service_unavailable"
    message = "Service temporarily unavailable"


class NotFoundError(AppError):
    """A requested entity does not exist."""

    status_code = HTTPStatus.NOT_FOUND
    code = "not_found"
    message = "Entity not found"


class ConflictError(AppError):
    """The request contradicts the current state of the system."""

    status_code = HTTPStatus.CONFLICT
    code = "conflict"
    message = "Conflicting state"


class ValidationError(AppError):
    """Input is syntactically fine but violates a domain rule."""

    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    code = "validation_error"
    message = "Invalid input"


class ProductNotFoundError(NotFoundError):
    """No product matches the given identifier or slug."""

    code = "product_not_found"
    message = "Product not found"


class PurchaseNotFoundError(NotFoundError):
    """No purchase matches the given identifier or invoice."""

    code = "purchase_not_found"
    message = "Purchase not found"


class UserNotFoundError(NotFoundError):
    """No user matches the given Telegram id."""

    code = "user_not_found"
    message = "User not found"


class SlugAlreadyExistsError(ConflictError):
    """Another product already occupies this deep-link slug."""

    code = "slug_already_exists"
    message = "A product with this slug already exists"


class DuplicatePurchaseError(ConflictError):
    """The buyer already owns this product."""

    code = "duplicate_purchase"
    message = "This product has already been purchased by the user"


class InvalidSlugError(ValidationError):
    """The slug cannot be used as a Telegram deep-link payload."""

    code = "invalid_slug"
    message = "Invalid deep-link slug"


class InvalidPriceError(ValidationError):
    """A product must be purchasable through at least one provider."""

    code = "invalid_price"
    message = "At least one price (Stars or USDT) must be set"
