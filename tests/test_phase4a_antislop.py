"""Phase 04-A P5 Anti-Slop quality diagnostics tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_prompt_service
from api.routers.prompts import router as prompts_router
from api.services.prompts import PromptApplicationService
from pixelle_video.media_jobs.models import Base
from pixelle_video.prompts.antislop import AntiSlopChecker
from pixelle_video.prompts.repository import PromptRepository


def test_checker_detects_slop_across_categories() -> None:
    checker = AntiSlopChecker()
    text = (
        "An amazing masterpiece with 8k resolution, beautiful cinematic lighting "
        "and a very very dramatic atmosphere."
    )
    report = checker.check(text)
    assert report["slop_count"] >= 4
    categories = {violation["category"] for violation in report["violations"]}
    assert {
        "superlative_boosters",
        "quality_assertions",
        "resolution_theater",
        "vague_aesthetic",
        "empty_atmosphere",
        "redundant_emphasis",
    } <= categories


def test_checker_density_is_per_hundred_words() -> None:
    checker = AntiSlopChecker()
    report = checker.check("an amazing masterpiece with 8k resolution")
    words = len("an amazing masterpiece with 8k resolution".split())
    expected = round(3 * 100.0 / words, 2)
    assert report["density"] == expected


def test_checker_clean_text_has_no_violations() -> None:
    checker = AntiSlopChecker()
    report = checker.check("A woman pours coffee into a white ceramic cup on a wooden table")
    assert report["slop_count"] == 0
    assert report["violations"] == []


def test_checker_reports_ai_self_praise() -> None:
    checker = AntiSlopChecker()
    report = checker.check("This image was created by ai, a masterpiece")
    categories = {violation["category"] for violation in report["violations"]}
    assert "ai_self_praise" in categories


@pytest.fixture
async def api_env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'slop.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PromptRepository(factory)
    service = PromptApplicationService(repository)
    try:
        yield factory, repository, service
    finally:
        await engine.dispose()


async def test_api_check_quality_returns_report(api_env) -> None:
    _factory, repository, service = api_env
    template = await repository.create_template(
        name="Slop模板",
        category="广告",
        template_text="An amazing masterpiece with 8k resolution",
        variables_json=[],
        description="slop 测试",
    )
    app = FastAPI()
    app.include_router(prompts_router, prefix="/api")
    app.dependency_overrides[get_prompt_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(f"/api/admin/prompts/{template.id}/check-quality")
    assert response.status_code == 200
    body = response.json()
    assert body["slop_count"] >= 2
    assert body["violations"][0]["suggestion"]
