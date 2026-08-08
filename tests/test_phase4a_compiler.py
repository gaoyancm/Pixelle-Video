"""Phase 04-A P2 variable injection engine tests."""

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
from pixelle_video.prompts.compiler import (
    PromptCompileError,
    compile,
    extract_variables,
    validate,
)
from pixelle_video.prompts.models import PromptTemplate
from pixelle_video.prompts.repository import PromptRepository

VARIABLE_DEFS = [
    {
        "name": "product_name",
        "type": "string",
        "required": True,
        "default": None,
        "description": "",
    },
    {
        "name": "target_audience",
        "type": "string",
        "required": False,
        "default": "年轻人",
        "description": "",
    },
    {"name": "platform", "type": "string", "required": True, "default": None, "description": ""},
    {"name": "price", "type": "integer", "required": False, "default": None, "description": ""},
]

TEMPLATE = (
    "为 {{product_name}} 制作面向 {{target_audience}} 的 {{platform}} 广告，价格 {{price}} 元。"
)


def test_extract_variables_returns_referenced_names() -> None:
    assert extract_variables(TEMPLATE) == {
        "product_name",
        "target_audience",
        "platform",
        "price",
    }


def test_compile_replaces_placeholders() -> None:
    result = compile(
        TEMPLATE,
        {"product_name": "保温杯", "target_audience": "上班族", "platform": "抖音", "price": 99},
        variable_defs=VARIABLE_DEFS,
    )
    assert result == "为 保温杯 制作面向 上班族 的 抖音 广告，价格 99 元。"
    assert "{{" not in result


def test_compile_uses_default_for_missing_optional() -> None:
    result = compile(
        TEMPLATE,
        {"product_name": "保温杯", "platform": "抖音"},
        variable_defs=VARIABLE_DEFS,
    )
    assert "年轻人" in result


def test_compile_missing_required_raises() -> None:
    with pytest.raises(PromptCompileError) as exc:
        compile(TEMPLATE, {"product_name": "保温杯"}, variable_defs=VARIABLE_DEFS)
    assert "platform" in str(exc.value)


def test_compile_without_definitions_raises_on_missing() -> None:
    with pytest.raises(PromptCompileError):
        compile("hi {{name}}", {})


def test_validate_reports_missing_required_and_unknown() -> None:
    issues = validate(TEMPLATE, VARIABLE_DEFS, {"product_name": "x"})
    assert any("platform" in issue for issue in issues)
    issues_unknown = validate("{{undef}} ok", VARIABLE_DEFS, {})
    assert any("undef" in issue for issue in issues_unknown)


def test_validate_reports_type_mismatch() -> None:
    issues = validate(TEMPLATE, VARIABLE_DEFS, {"product_name": 123, "platform": "x"})
    assert any("product_name" in issue and "string" in issue for issue in issues)


def test_validate_passes_when_complete() -> None:
    issues = validate(
        TEMPLATE,
        VARIABLE_DEFS,
        {"product_name": "a", "platform": "b", "target_audience": "c", "price": 1},
    )
    assert issues == []


@pytest.fixture
async def api_env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'compiler.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = PromptRepository(factory)
    service = PromptApplicationService(repository)
    try:
        yield factory, repository, service
    finally:
        await engine.dispose()


async def _seed_template(repository: PromptRepository):
    return await repository.create_template(
        name="编译模板",
        category="广告",
        template_text=TEMPLATE,
        variables_json=VARIABLE_DEFS,
        description="编译测试",
    )


async def test_compile_api_returns_prompt_and_updates_usage(api_env) -> None:
    factory, repository, service = api_env
    template = await _seed_template(repository)
    app = FastAPI()
    app.include_router(prompts_router, prefix="/api")
    app.dependency_overrides[get_prompt_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/admin/prompts/{template.id}/compile",
            json={"variables": {"product_name": "杯子", "platform": "小红书"}},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["prompt"] == "为 杯子 制作面向 年轻人 的 小红书 广告，价格  元。"
    async with factory() as session:
        stored = await session.get(PromptTemplate, template.id)
    assert stored is not None
    assert stored.usage_count == 1
    assert stored.last_used_at is not None


async def test_validate_api_reports_issues(api_env) -> None:
    factory, repository, service = api_env
    template = await _seed_template(repository)
    app = FastAPI()
    app.include_router(prompts_router, prefix="/api")
    app.dependency_overrides[get_prompt_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/admin/prompts/{template.id}/validate",
            json={"variables": {}},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert any("product_name" in issue for issue in body["issues"])


async def test_compile_api_errors_on_missing_required(api_env) -> None:
    factory, repository, service = api_env
    template = await _seed_template(repository)
    app = FastAPI()
    app.include_router(prompts_router, prefix="/api")
    app.dependency_overrides[get_prompt_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/admin/prompts/{template.id}/compile",
            json={"variables": {"product_name": "杯子"}},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "compile_failed"
