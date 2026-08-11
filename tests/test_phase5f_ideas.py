"""Phase 05-F A2 LLM copy generation tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import pixelle_video.management.models as _management_models  # noqa: F401
from api.dependencies import get_product_service
from api.routers.products import router as products_router
from api.services.products import ProductApplicationService
from pixelle_video.media_jobs.models import Base
from pixelle_video.orchestration.agents.sub_agents import Copywriter
from pixelle_video.products.repository import ProductBriefRepository
from tests.test_phase5_briefs import _brief_body


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'a2.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ProductBriefRepository(factory)
    service = ProductApplicationService(repository)
    try:
        yield factory, repository, service
    finally:
        await engine.dispose()


async def _agent_with(caller):
    return Copywriter(caller)


async def test_generate_ideas_with_copywriter_agent(env) -> None:
    _factory, repository, service = env
    agent = await _agent_with(_mock_copywriter_llm)
    service.copywriter_agent = agent
    brief = await repository.create_brief(**_brief_body())
    payload = await service.generate_ideas(brief.id)
    assert payload["source"] == "04-e-copywriter"
    assert len(payload["ideas"]) == 3
    for idea in payload["ideas"]:
        assert idea["hook"] and idea["headline"] and idea["cta"] and idea["style"]
    assert payload["ideas"][0]["hook"] == "AI Hook 1"
    assert payload["ideas"][0]["cta"] == "AI CTA 1"


async def test_generate_ideas_agent_uses_creative_directions(env) -> None:
    _factory, repository, service = env
    captured: list[str] = []

    async def spy_llm(text: str) -> str:
        captured.append(text)
        return await _mock_copywriter_llm(text)

    service.copywriter_agent = await _agent_with(spy_llm)
    brief = await repository.create_brief(
        **_brief_body(
            reference_images=[{"plan_id": "plan-1", "creative_directions": [{"angle": "奢华风"}]}]
        )
    )
    await service.generate_ideas(brief.id)
    assert "奢华风" in captured[0]


async def test_generate_ideas_falls_back_to_template(env) -> None:
    """Backwards compatibility: without a copywriter agent, the legacy
    template path still returns the same ideas shape."""
    _factory, repository, service = env
    brief = await repository.create_brief(**_brief_body())
    payload = await service.generate_ideas(brief.id)
    assert payload["brief_id"] == brief.id
    assert len(payload["ideas"]) == 3
    assert "source" not in payload  # legacy path has no source marker
    for idea in payload["ideas"]:
        assert idea["hook"] and idea["cta"] and idea["style"]


async def test_generate_ideas_agent_still_passes_04a_compiler(env) -> None:
    """The agent path compiles through 04-A (double-brace contract)."""
    from pixelle_video.prompts.compiler import compile as prompt_compile

    _factory, repository, service = env
    service.copywriter_agent = Copywriter(_mock_copywriter_llm, prompt_compiler=prompt_compile)
    brief = await repository.create_brief(**_brief_body())
    payload = await service.generate_ideas(brief.id)
    assert payload["source"] == "04-e-copywriter"
    assert payload["ideas"][0]["hook"] == "AI Hook 1"


async def test_generate_ideas_agent_uses_plan_meta_when_present(env) -> None:
    _factory, repository, service = env
    service.copywriter_agent = await _agent_with(_mock_copywriter_llm)
    brief = await repository.create_brief(
        **_brief_body(reference_images=[{"plan_id": "plan-x", "creative_directions": []}])
    )
    payload = await service.generate_ideas(brief.id)
    assert payload["source"] == "04-e-copywriter"


async def test_existing_05_tests_stay_green(env) -> None:
    """05 regression: service-level legacy shape unchanged."""
    _factory, repository, service = env
    brief = await repository.create_brief(**_brief_body())
    payload = await service.generate_ideas(brief.id)
    assert payload["brief_id"] == brief.id
    assert len(payload["ideas"]) == 3
    for idea in payload["ideas"]:
        assert idea["hook"] and idea["headline"] and idea["cta"] and idea["style"]


@pytest.fixture
async def api_client(env):
    _factory, _repository, service = env
    app = FastAPI()
    app.include_router(products_router, prefix="/api")
    app.dependency_overrides[get_product_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, service


async def test_api_generate_ideas_with_agent(api_client) -> None:
    client, _factory, repository, service = api_client
    service.copywriter_agent = await _agent_with(_mock_copywriter_llm)
    brief = await repository.create_brief(**_brief_body())
    response = await client.post(f"/api/products/briefs/{brief.id}/generate-ideas")
    assert response.status_code == 200
    body = response.json()
    assert len(body["ideas"]) == 3
    assert body["ideas"][0]["hook"] == "AI Hook 1"


async def test_api_generate_ideas_legacy_still_works(api_client) -> None:
    client, _factory, repository, _service = api_client
    brief = await repository.create_brief(**_brief_body())
    response = await client.post(f"/api/products/briefs/{brief.id}/generate-ideas")
    assert response.status_code == 200
    assert len(response.json()["ideas"]) == 3


async def _mock_copywriter_llm(text: str) -> str:
    return (
        '{"hooks": ["AI Hook 1", "AI Hook 2", "AI Hook 3"],'
        ' "ctas": ["AI CTA 1", "AI CTA 2", "AI CTA 3"],'
        ' "body_copy": "AI 生成的正文"}'
    )
