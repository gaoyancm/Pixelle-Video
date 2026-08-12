"""Phase 08 D2 DeepSeek caller tests."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from pixelle_video.config.schema import LLMConfig
from pixelle_video.orchestration.llm.deepseek_caller import deepseek_llm_caller


@pytest.mark.asyncio
async def test_deepseek_caller_returns_content(monkeypatch) -> None:
    async def fake_post(self, url, json=None, headers=None):
        assert url == "https://api.deepseek.com/chat/completions"
        assert json["model"] == "deepseek-chat"
        assert json["messages"][1]["content"] == "hello"
        assert headers["Authorization"] == "Bearer sk-test"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "你好，世界"}}]},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await deepseek_llm_caller(
        "hello", model="deepseek-chat", api_key="sk-test", base_url="https://api.deepseek.com"
    )
    assert result == "你好，世界"


@pytest.mark.asyncio
async def test_deepseek_caller_http_error(monkeypatch) -> None:
    async def fake_post(self, url, json=None, headers=None):
        return httpx.Response(401, text="unauthorized")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    with pytest.raises(RuntimeError, match="deepseek HTTP 401"):
        await deepseek_llm_caller(
            "x", model="m", api_key="bad", base_url="https://api.deepseek.com"
        )


@pytest.mark.asyncio
async def test_deepseek_caller_network_error(monkeypatch) -> None:
    async def fake_post(self, url, json=None, headers=None):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    with pytest.raises(RuntimeError, match="deepseek call failed"):
        await deepseek_llm_caller("x", model="m", api_key="k", base_url="https://api.deepseek.com")


@pytest.mark.asyncio
async def test_deepseek_caller_bad_payload(monkeypatch) -> None:
    async def fake_post(self, url, json=None, headers=None):
        return httpx.Response(200, json={"unexpected": True})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    with pytest.raises(RuntimeError, match="unexpected deepseek response"):
        await deepseek_llm_caller("x", model="m", api_key="k", base_url="https://api.deepseek.com")


@pytest.mark.asyncio
async def test_deepseek_caller_empty_content(monkeypatch) -> None:
    async def fake_post(self, url, json=None, headers=None):
        return httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    with pytest.raises(RuntimeError, match="empty content"):
        await deepseek_llm_caller("x", model="m", api_key="k", base_url="https://api.deepseek.com")


@pytest.mark.asyncio
async def test_deepseek_caller_caps_max_tokens(monkeypatch) -> None:
    captured: dict = {}

    async def fake_post(self, url, json=None, headers=None):
        captured.update(json)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    await deepseek_llm_caller("x", model="m", api_key="k", base_url="https://api.deepseek.com")
    assert captured["max_tokens"] == 800  # cost cap under the ¥0.50 budget


# --- DI fallback logic ------------------------------------------------------


def _fake_manager(llm: LLMConfig | None = None) -> SimpleNamespace:
    return SimpleNamespace(config=SimpleNamespace(llm=llm or LLMConfig()))


@pytest.mark.asyncio
async def test_resolve_falls_back_to_mock_without_key(monkeypatch) -> None:
    import api.dependencies as deps
    from api.dependencies import _resolve_llm_caller

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    caller = _resolve_llm_caller(_fake_manager())
    assert caller is deps._mock_llm_caller


@pytest.mark.asyncio
async def test_resolve_uses_env_key(monkeypatch) -> None:
    from api.dependencies import _resolve_llm_caller

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    manager = _fake_manager(LLMConfig(base_url="https://api.deepseek.com", model="deepseek-chat"))
    caller = _resolve_llm_caller(manager)
    assert caller.__name__ == "_real_caller"


@pytest.mark.asyncio
async def test_resolve_uses_config_key(monkeypatch) -> None:
    from api.dependencies import _resolve_llm_caller

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    manager = _fake_manager(
        LLMConfig(
            api_key="sk-config",
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
        )
    )
    caller = _resolve_llm_caller(manager)
    assert caller.__name__ == "_real_caller"


@pytest.mark.asyncio
async def test_real_caller_invokes_deepseek(monkeypatch) -> None:
    from api.dependencies import _resolve_llm_caller

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    manager = _fake_manager(
        LLMConfig(
            api_key="sk-config",
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
        )
    )
    caller = _resolve_llm_caller(manager)

    async def fake_post(self, url, json=None, headers=None):
        return httpx.Response(200, json={"choices": [{"message": {"content": "真实响应"}}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await caller("你好")
    assert result == "真实响应"
