"""Phase 04-E L4 approval gate and budget control tests."""

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
from pixelle_video.audit.repository import AuditRepository
from pixelle_video.budget.models import BudgetConfig
from pixelle_video.budget.repository import BudgetRepository
from pixelle_video.media_jobs.models import Base
from pixelle_video.orchestration.agents.decision_agent import DecisionAgent
from pixelle_video.orchestration.agents.sub_agents import (
    ContentStrategist,
    Copywriter,
    StoryboardPlanner,
    Supervisor,
)
from pixelle_video.orchestration.budget_guard import BudgetExceededError, BudgetGuard
from pixelle_video.orchestration.pipeline import OrchestrationPipeline
from pixelle_video.orchestration.repository import ContentPlanRepository


async def _good_llm(text: str) -> str:
    if "storyboard_planner" in text:
        return '{"scenes": [{"index": 1}], "camera_notes": "推近"}'
    if "copywriter" in text:
        return '{"hooks": ["h"], "ctas": ["c"], "body_copy": "b"}'
    if "supervisor" in text:
        return '{"grade": "B", "severe_issues": 0, "medium_issues": 1, "suggestions": ["x"]}'
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
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'l4.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ContentPlanRepository(factory)
    audit_repository = AuditRepository(factory)
    budget_repository = BudgetRepository(factory)
    service = OrchestrationService(repository)
    try:
        yield factory, repository, audit_repository, budget_repository, service
    finally:
        await engine.dispose()


def _pipeline(repository, audit_repository, budget_repository, mode="cap", limit=0.02):
    agents = _agents()
    decision_agent = DecisionAgent(agents, llm_caller=None)

    async def spent_resolver(plan_id: str) -> float:
        if audit_repository is None:
            return 0.0
        events, _ = await audit_repository.list(
            scope_type="content_plan",
            scope_id=plan_id,
            event_type="llm_call",
            limit=500,
        )
        return round(
            sum(float(e.cost_snapshot.get("cost", 0.0)) for e in events if e.cost_snapshot),
            6,
        )

    async def config_reader():
        return BudgetConfig(mode=mode, per_task_limit=limit)

    guard = BudgetGuard(config_reader, spent_resolver)
    return OrchestrationPipeline(
        repository,
        decision_agent,
        supervisor=agents["run_supervisor"],
        budget_guard=guard,
        audit_recorder=audit_repository.record if audit_repository is not None else None,
    )


async def test_budget_guard_cap_blocks(env) -> None:
    _factory, repository, audit_repository, _budget, _service = env

    async def spent_resolver(plan_id: str) -> float:
        return 0.02  # already at cap

    async def config_reader():
        return BudgetConfig(mode="cap", per_task_limit=0.02)

    guard = BudgetGuard(config_reader, spent_resolver)
    with pytest.raises(BudgetExceededError):
        await guard.check_can_spend("plan-1", 0.01)


async def test_budget_guard_observe_never_blocks(env) -> None:
    _factory, _repository, _audit, _budget, _service = env

    async def spent_resolver(plan_id: str) -> float:
        return 999.0

    async def config_reader():
        return BudgetConfig(mode="observe", per_task_limit=0.01)

    guard = BudgetGuard(config_reader, spent_resolver)
    await guard.check_can_spend("plan-1", 1.0)  # no raise


async def test_cap_mode_blocks_pipeline_generation(env) -> None:
    _factory, repository, audit_repository, budget_repository, service = env
    service.pipeline = _pipeline(
        repository, audit_repository, budget_repository, mode="cap", limit=0.0001
    )
    from api.schemas.orchestration import PlanCreateRequest

    plan = await service.create_plan(PlanCreateRequest(request_text="短视频"))
    with pytest.raises(BudgetExceededError):
        await service.generate(plan.id)


async def test_llm_call_audited_with_cost(env) -> None:
    _factory, repository, audit_repository, budget_repository, service = env
    service.pipeline = _pipeline(repository, audit_repository, budget_repository)
    from api.schemas.orchestration import PlanCreateRequest

    plan = await service.create_plan(PlanCreateRequest(request_text="短视频"))
    await service.generate(plan.id)
    events, _ = await audit_repository.list(
        scope_type="content_plan", scope_id=plan.id, event_type="llm_call", limit=100
    )
    assert len(events) >= 4  # strategist x2 + copywriter + storyboard (+supervisor)
    for event in events:
        assert event.cost_snapshot is not None
        assert event.cost_snapshot["cost"] > 0
        assert "tokens_in" in event.details_json


async def test_generate_ends_awaiting_approval(env) -> None:
    _factory, repository, audit_repository, budget_repository, service = env
    service.pipeline = _pipeline(repository, audit_repository, budget_repository)
    from api.schemas.orchestration import PlanCreateRequest

    plan = await service.create_plan(PlanCreateRequest(request_text="短视频"))
    payload = await service.generate(plan.id)
    assert payload["status"] == "awaiting_approval"
    stored = await repository.get_plan(plan.id)
    assert stored.status == "awaiting_approval"


async def test_approve_changes_status(env) -> None:
    _factory, repository, _audit, _budget, service = env
    from api.schemas.orchestration import PlanCreateRequest

    plan = await service.create_plan(PlanCreateRequest(request_text="短视频"))
    await repository.update_plan(plan.id, status="awaiting_approval")
    result = await service.approve(plan.id)
    assert result["status"] == "approved"


async def test_reject_changes_status(env) -> None:
    _factory, repository, _audit, _budget, service = env
    from api.schemas.orchestration import PlanCreateRequest

    plan = await service.create_plan(PlanCreateRequest(request_text="短视频"))
    await repository.update_plan(plan.id, status="awaiting_approval")
    result = await service.reject(plan.id, "创意方向不符")
    assert result["status"] == "rejected"


async def test_approval_summary_contains_grade_and_cost(env) -> None:
    _factory, repository, audit_repository, budget_repository, service = env
    service.pipeline = _pipeline(repository, audit_repository, budget_repository)
    from api.schemas.orchestration import PlanCreateRequest

    plan = await service.create_plan(PlanCreateRequest(request_text="短视频"))
    await service.generate(plan.id)
    summary = await service.approval_summary(plan.id)
    assert summary["summary"]
    assert summary["grade"] == "B"
    assert summary["cost_estimate"] is not None
    assert summary["details"]["intent"] == "short_video"


@pytest.fixture
async def api_client(env):
    _factory, _repository, _audit, _budget, service = env
    app = FastAPI()
    app.include_router(orchestration_router, prefix="/api")
    app.dependency_overrides[get_orchestration_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, service


async def test_api_approve_and_reject(api_client) -> None:
    client, _factory, repository, service = api_client
    created = await client.post("/api/orchestration/plans", json={"request_text": "短视频"})
    plan_id = created.json()["id"]
    await repository.update_plan(plan_id, status="awaiting_approval")

    approved = await client.post(f"/api/orchestration/plans/{plan_id}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    await repository.update_plan(plan_id, status="awaiting_approval")
    rejected = await client.post(
        f"/api/orchestration/plans/{plan_id}/reject", json={"reason": "重做"}
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"


async def test_api_approval_summary(api_client) -> None:
    client, _factory, repository, service = api_client

    service.pipeline = _pipeline(repository, None, None)
    # audit/budget not needed for this path.
    created = await client.post("/api/orchestration/plans", json={"request_text": "短视频"})
    plan_id = created.json()["id"]
    await client.post(f"/api/orchestration/plans/{plan_id}/generate")
    summary = await client.get(f"/api/orchestration/plans/{plan_id}/approval-summary")
    assert summary.status_code == 200
    assert summary.json()["grade"] == "B"


async def test_api_cap_returns_402(api_client) -> None:
    client, _factory, repository, service = api_client
    from pixelle_video.orchestration.pipeline import OrchestrationPipeline

    async def spent_resolver(plan_id: str) -> float:
        return 999.0

    async def config_reader():
        return BudgetConfig(mode="cap", per_task_limit=0.001)

    from pixelle_video.orchestration.budget_guard import BudgetGuard

    guard = BudgetGuard(config_reader, spent_resolver)
    service.pipeline = OrchestrationPipeline(
        repository,
        DecisionAgent(_agents(), llm_caller=None),
        supervisor=_agents()["run_supervisor"],
        budget_guard=guard,
    )
    created = await client.post("/api/orchestration/plans", json={"request_text": "短视频"})
    plan_id = created.json()["id"]
    generated = await client.post(f"/api/orchestration/plans/{plan_id}/generate")
    assert generated.status_code == 402
    assert generated.json()["error"]["code"] == "budget_exceeded"
