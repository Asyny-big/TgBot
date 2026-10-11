"""``openapi.json`` is the contract the admin panel's types are generated from.

It must be exactly the schema the code serves: a route or a schema changed
without re-exporting it would let the frontend drift from the API silently.
"""

from __future__ import annotations

import importlib.util
import json
from typing import TYPE_CHECKING

from app.core.config import BACKEND_DIR

if TYPE_CHECKING:
    from types import ModuleType

FIX = "re-export it: `make openapi`, then `npm run api:types` in frontend/"


def _exporter() -> ModuleType:
    path = BACKEND_DIR / "scripts" / "export_openapi.py"
    spec = importlib.util.spec_from_file_location("export_openapi", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openapi_json_is_the_schema_the_code_serves() -> None:
    exporter = _exporter()
    committed = (BACKEND_DIR / "openapi.json").read_text(encoding="utf-8")
    served = exporter.build_schema()

    assert json.loads(committed) == served, f"openapi.json is stale — {FIX}"
    # Byte for byte too (line endings aside): the file is the exporter's output.
    assert committed.replace("\r\n", "\n") == exporter.render(served), FIX


def test_the_daily_endpoints_are_in_the_contract() -> None:
    paths = json.loads((BACKEND_DIR / "openapi.json").read_text(encoding="utf-8"))["paths"]

    daily = sorted(path for path in paths if path.startswith("/api/v1/daily/"))

    assert daily == [
        "/api/v1/daily/alerts",
        "/api/v1/daily/deliveries",
        "/api/v1/daily/deliveries/{delivery_id}",
        "/api/v1/daily/library",
        "/api/v1/daily/library/{content_id}",
        "/api/v1/daily/library/{content_id}/send-to-me",
        "/api/v1/daily/library/{content_id}/status",
        "/api/v1/daily/overview",
        "/api/v1/daily/requests",
        "/api/v1/daily/requests/{request_id}",
        "/api/v1/daily/requests/{request_id}/items/{item_id}/send-to-me",
        "/api/v1/daily/requests/{request_id}/replies",
        "/api/v1/daily/subscribers",
        "/api/v1/daily/subscribers/{user_id}",
        "/api/v1/daily/subscribers/{user_id}/requests-ban",
    ]
    # Nothing the panel reads can carry a file id: previews go through the bot.
    schemas = json.loads((BACKEND_DIR / "openapi.json").read_text(encoding="utf-8"))["components"]
    for name, schema in schemas["schemas"].items():
        if name.startswith("Daily"):
            assert "file_id" not in schema.get("properties", {}), name
            assert "file_unique_id" not in schema.get("properties", {}), name
