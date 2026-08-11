"""Phase 04-E L3 pipeline checkpoint tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import pixelle_video.management.models as _management_models  # noqa: F401
from api.dependencies import get_orchestration_service
from api.routers.orchestration import router as orchestration_router
from api.services.orchestration import OrchestrationService
from pixelle_video.media_jobs.models import Base
from pixelle_video.orchestration.agents.decision_agent import DecisionAgent
from pixelle_video.orchestration.agents.sub_agents import (
    ContentStrategist,
    Copywriter,
    StoryboardPlanner,
    Supervisor,
)
from pixelle_video.orchestration.pipeline import OrchestrationPipeline, PipelineError
from pixelle_video.orchestration.repository import ContentPlanRepository


async def _good_llm(text: str) -> str:
    if "storyboard_planner" in text:
        return '{"scenes": [{"index": 1}], "camera_notes": "推近"}'
    if "copywriter" in text:
        return '{"hooks": ["h"], "ctas": ["c"], "body_copy": "b"}'
    if "supervisor" in text:
        return '{"grade": "A", "severe_issues": 0, "medium_issues": 0, "suggestions": []}'
    return '{"target_audience": "a", "creative_directions": [{"hook": "h"}], "visual_style": {"mood": "m"}}'


def _agents(llm=None):
    caller = llm or _good_llm
    return {
        "run_content_strategist": ContentStrategist(caller),
        "run_copywriter": Copywriter(caller),
        "run_storyboard_planner": StoryboardPlanner(caller),
        "run_supervisor": Supervisor(caller),
    }


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'l3.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ContentPlanRepository(factory)
    service = OrchestrationService(repository)
    try:
        yield factory, repository, service
    finally:
        await engine.dispose()


def _pipeline(repository, llm=None):
    agents = _agents(llm)
    decision_agent = DecisionAgent(agents, llm_caller=None)
    return OrchestrationPipeline(repository, decision_agent, supervisor=agents["run_supervisor"])


async def test_fresh_checkpoint_shape(env) -> None:
    _factory, _repository, _service = env
    checkpoint = OrchestrationPipeline.fresh_checkpoint()
    assert checkpoint["completed_stages"] == []
    assert checkpoint["total_cost_so_far"] == 0.0
    assert checkpoint["current_stage"] is None


async def test_pipeline_serializes_all_stages(env) -> None:
    _factory, repository, service = env
    service.pipeline = _pipeline(repository)
    from api.schemas.orchestration import PlanCreateRequest

    plan = await service.create_plan(PlanCreateRequest(request_text="短视频"))
    payload = await service.generate(plan.id)
    checkpoint = payload["checkpoint"]
    assert checkpoint["current_stage"] is None
    assert set(checkpoint["completed_stages"]) == {
        "intent_classification",
        "audience_analysis",
        "copy_writing",
        "storyboard",
        "supervision",
    }
    assert len(checkpoint["stage_results"]) == 5
    assert checkpoint["total_cost_so_far"] > 0


async def test_resume_skips_completed_stages(env) -> None:
    _factory, repository, service = env
    service.pipeline = _pipeline(repository)
    from api.schemas.orchestration import PlanCreateRequest

    plan = await service.create_plan(PlanCreateRequest(request_text="动画"))
    await service.generate(plan.id)
    # Simulate a half checkpoint: drop supervision from completed.
    stored = await repository.get_plan(plan.id)
    checkpoint = dict(stored.checkpoint_json)
    checkpoint["completed_stages"].remove("supervision")
    checkpoint["stage_results"].pop("supervision", None)
    await repository.update_plan(plan.id, checkpoint_json=checkpoint, status="draft")
    payload = await service.resume(plan.id)
    completed = payload["checkpoint"]["completed_stages"]
    assert "supervision" in completed
    assert "copy_writing" in completed  # untouched by resume


async def test_retry_stage_resets_one_stage(env) -> None:
    _factory, repository, service = env
    service.pipeline = _pipeline(repository)
    from api.schemas.orchestration import PlanCreateRequest

    plan = await service.create_plan(PlanCreateRequest(request_text="短视频"))
    await service.generate(plan.id)
    payload = await service.retry_stage(plan.id, "copy_writing")
    checkpoint = payload["checkpoint"]
    assert "copy_writing" in checkpoint["completed_stages"]  # re-run after reset
    assert checkpoint["retry_count"]["copy_writing"] == 0


async def test_stage_failure_marks_plan(env) -> None:
    _factory, repository, service = env
    calls = {"count": 0}

    async def flaky_llm(text: str) -> str:
        calls["count"] += 1
        if "storyboard_planner" in text:
            return "invalid output forever"
        return await _good_llm(text)

    service.pipeline = _pipeline(repository, llm=flaky_llm)
    from api.schemas.orchestration import PlanCreateRequest

    plan = await service.create_plan(PlanCreateRequest(request_text="短视频"))
    with pytest.raises(PipelineError, match="storyboard"):
        await service.generate(plan.id)
    stored = await repository.get_plan(plan.id)
    assert stored.status == "stage_failed"
    checkpoint = stored.checkpoint_json
    assert checkpoint["retry_count"]["storyboard"] >= 1


async def test_status_reports_progress(env) -> None:
    _factory, repository, service = env
    service.pipeline = _pipeline(repository)
    from api.schemas.orchestration import PlanCreateRequest

    plan = await service.create_plan(PlanCreateRequest(request_text="短视频"))
    await service.generate(plan.id)
    status = await service.status(plan.id)
    assert status["status"] == "awaiting_approval"
    assert len(status["completed_stages"]) == 5


@pytest.fixture
async def api_client(env):
    _factory, _repository, service = env
    app = FastAPI()
    app.include_router(orchestration_router, prefix="/api")
    app.dependency_overrides[get_orchestration_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, service


async def test_api_resume_from_checkpoint(api_client) -> None:
    client, _factory, repository, service = api_client
    service.pipeline = _pipeline(repository)
    created = await client.post("/api/orchestration/plans", json={"request_text": "短视频"})
    plan_id = created.json()["id"]
    await client.post(f"/api/orchestration/plans/{plan_id}/generate")
    resumed = await client.post(f"/api/orchestration/plans/{plan_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "awaiting_approval"

    status = await client.get(f"/api/orchestration/plans/{plan_id}/status")
    assert status.status_code == 200
    assert status.json()["total_cost_so_far"] > 0


async def test_api_stage_retry(api_client) -> None:
    client, _factory, repository, service = api_client
    service.pipeline = _pipeline(repository)
    created = await client.post("/api/orchestration/plans", json={"request_text": "短视频"})
    plan_id = created.json()["id"]
    await client.post(f"/api/orchestration/plans/{plan_id}/generate")
    retried = await client.post(f"/api/orchestration/plans/{plan_id}/stage/copy_writing/retry")
    assert retried.status_code == 200
    assert "copy_writing" in retried.json()["checkpoint"]["completed_stages"]
