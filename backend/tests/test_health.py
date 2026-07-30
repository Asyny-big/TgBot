"""HTTP surface tests: probes, error envelope and request correlation."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient

from app import __version__
from app.api.deps import RESOURCES_STATE_KEY, get_resources
from app.api.middleware import REQUEST_ID_HEADER
from app.main import create_app
from tests.conftest import build_resources, build_settings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient as HTTPXClient


async def test_liveness_probe(client: HTTPXClient) -> None:
    response = await client.get("/api/v1/health/live")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "alive", "version": __version__}


async def test_readiness_probe_reports_healthy_dependencies(client: HTTPXClient) -> None:
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["status"] == "ready"
    assert body["environment"] == "testing"
    assert body["components"] == {"database": "ok", "redis": "ok"}


async def test_readiness_probe_reports_degraded_dependencies() -> None:
    settings = build_settings()
    app = create_app(settings)
    resources = build_resources(settings, cache_healthy=False)
    setattr(app.state, RESOURCES_STATE_KEY, resources)
    app.dependency_overrides[get_resources] = lambda: resources

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    body = response.json()
    assert body["status"] == "degraded"
    assert body["components"] == {"database": "ok", "redis": "fail"}


async def test_request_id_is_echoed_back(client: HTTPXClient) -> None:
    response = await client.get(
        "/api/v1/health/live",
        headers={REQUEST_ID_HEADER: "fixed-request-id"},
    )
    assert response.headers[REQUEST_ID_HEADER] == "fixed-request-id"


async def test_request_id_is_generated_when_absent(client: HTTPXClient) -> None:
    response = await client.get("/api/v1/health/live")
    assert len(response.headers[REQUEST_ID_HEADER]) == 32


async def test_unknown_route_uses_error_envelope(client: HTTPXClient) -> None:
    response = await client.get("/api/v1/does-not-exist")
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json() == {
        "error": {"code": "not_found", "message": "Not Found", "details": {}}
    }


async def test_docs_are_disabled_in_production() -> None:
    from app.core.config import AppSettings, Environment, LogFormat  # noqa: PLC0415

    settings = build_settings(
        app=AppSettings(
            environment=Environment.PRODUCTION,
            log_format=LogFormat.JSON,
            docs_enabled=True,
        )
    )
    app: FastAPI = create_app(settings)
    assert app.docs_url is None
    assert app.openapi_url is None
