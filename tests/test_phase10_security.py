"""Phase 10 task 8: local security boundary (host, CORS, wildcard rejection)."""

from __future__ import annotations

import httpx
import pytest

from api.config import APIConfig, api_config


def test_api_binds_loopback_by_default() -> None:
    assert api_config.host == "127.0.0.1"


def test_cors_origins_are_local_ui_only() -> None:
    assert "*" not in api_config.cors_origins
    assert "http://127.0.0.1:8501" in api_config.cors_origins


def test_wildcard_origin_with_credentials_is_rejected() -> None:
    with pytest.raises(ValueError):
        APIConfig(cors_origins=["*"])


async def test_cors_allows_local_origin_and_rejects_foreign() -> None:
    from api.app import app

    transport = httpx.ASGITransport(app=app)

    # Local UI origin is allowed.
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test",
        headers={"Origin": "http://127.0.0.1:8501"},
    ) as client:
        allowed = await client.get("/health")
        assert allowed.headers.get("access-control-allow-origin") == "http://127.0.0.1:8501"

    # Foreign origin is not echoed back (browser will block it).
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test",
        headers={"Origin": "https://evil.example.com"},
    ) as client:
        denied = await client.get("/health")
        assert denied.headers.get("access-control-allow-origin") is None
