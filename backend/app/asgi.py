"""ASGI entrypoint used by uvicorn: ``uvicorn app.asgi:app``."""

from __future__ import annotations

from app.main import create_app

app = create_app()

__all__ = ["app"]
