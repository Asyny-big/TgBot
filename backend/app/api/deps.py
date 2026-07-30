"""FastAPI dependency providers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Final, cast

from fastapi import Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings
from app.core.container import Container
from app.core.exceptions import InvalidTokenError
from app.core.resources import Resources
from app.domain.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, PageRequest
from app.services.checkout import CheckoutService

if TYPE_CHECKING:
    from app.domain.auth import AdminIdentity

RESOURCES_STATE_KEY: Final = "resources"
CONTAINER_STATE_KEY: Final = "container"
CHECKOUT_STATE_KEY: Final = "checkout"

# auto_error=False so a missing header produces our own error envelope.
_bearer = HTTPBearer(auto_error=False, description="Admin access token")


def _from_state(request: Request, key: str) -> object:
    """Read a value the application lifespan is expected to have installed."""
    value = getattr(request.app.state, key, None)
    if value is None:
        msg = f"Application state '{key}' is not initialised"
        raise RuntimeError(msg)
    return value


def get_resources(request: Request) -> Resources:
    """Return the infrastructure bundle created by the application lifespan."""
    return cast("Resources", _from_state(request, RESOURCES_STATE_KEY))


def get_container(request: Request) -> Container:
    """Return the wired service container created by the application lifespan."""
    return cast("Container", _from_state(request, CONTAINER_STATE_KEY))


def get_checkout(request: Request) -> CheckoutService:
    """Return the checkout service (delivery and manual payment verification)."""
    return cast("CheckoutService", _from_state(request, CHECKOUT_STATE_KEY))


def get_settings_dep(resources: Annotated[Resources, Depends(get_resources)]) -> Settings:
    """Return the validated settings of the running process."""
    return resources.settings


async def get_current_admin(
    container: Annotated[Container, Depends(get_container)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AdminIdentity:
    """Authenticate the bearer token.

    Raises:
        InvalidTokenError: header missing, or the token is invalid or revoked.
    """
    if credentials is None or not credentials.credentials:
        message = "Authorization header is missing"
        raise InvalidTokenError(message)
    return await container.auth.authenticate(credentials.credentials)


def get_page_request(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageRequest:
    """Pagination shared by every listing endpoint."""
    return PageRequest(limit=limit, offset=offset)


ResourcesDep = Annotated[Resources, Depends(get_resources)]
ContainerDep = Annotated[Container, Depends(get_container)]
CheckoutDep = Annotated[CheckoutService, Depends(get_checkout)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
CurrentAdmin = Annotated["AdminIdentity", Depends(get_current_admin)]
PageDep = Annotated[PageRequest, Depends(get_page_request)]
