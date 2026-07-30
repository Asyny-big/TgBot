"""FastAPI dependency providers."""

from __future__ import annotations

from typing import Annotated, Final, cast

from fastapi import Depends, Request

from app.core.config import Settings
from app.core.resources import Resources

RESOURCES_STATE_KEY: Final = "resources"


def get_resources(request: Request) -> Resources:
    """Return the infrastructure bundle created by the application lifespan."""
    resources = getattr(request.app.state, RESOURCES_STATE_KEY, None)
    if resources is None:
        msg = "Application resources are not initialised"
        raise RuntimeError(msg)
    return cast("Resources", resources)


def get_settings_dep(resources: Annotated[Resources, Depends(get_resources)]) -> Settings:
    """Return the validated settings of the running process."""
    return resources.settings


ResourcesDep = Annotated[Resources, Depends(get_resources)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
