"""Phase 04-E L2 agent orchestration tests."""

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
from pixelle_video.orchestration.agents.decision_agent import TOOLS, DecisionAgent
from pixelle_video.orchestration.agents.sub_agents import (
    ContentStrategist,
    Copywriter,
    StoryboardPlanner,
    SubAgent,
    Supervisor,
)
from pixelle_video.orchestration.repository import ContentPlanRepository


async def _mock_llm(text: str) -> str:
    if "storyboard_planner" in text:
        return '{"scenes": [{"index": 1}], "camera_notes": "推近"}'
    if "copywriter" in text:
        return '{"hooks": ["h1"], "ctas": ["c1"], "body_copy": "b"}'
    if "supervisor" in text:
        return '{"grade": "A", "severe_issues": 0, "medium_issues": 1, "suggestions": []}'
    return '{"target_audience": "a", "creative_directions": [{"hook": "h"}], "visual_style": {"mood": "m"}}'


def _sub_agents() -> dict:
    return {
        "run_content_strategist": ContentStrategist(_mock_llm),
        "run_copywriter": Copywriter(_mock_llm),
        "run_storyboard_planner": StoryboardPlanner(_mock_llm),
        "run_supervisor": Supervisor(_mock_llm),
    }


async def test_four_sub_agents_are_available() -> None:
    agents = _sub_agents()
    assert set(agents) == set(TOOLS)
    assert all(isinstance(agent, SubAgent) for agent in agents.values())


async def test_content_strategist_output_is_pydantic(env) -> None:
    _factory, _repository, _service, _router = env
    result = await _sub_agents()["run_content_strategist"].run("分析受众")
    assert result.content["target_audience"] == "a"
    assert result.content["creative_directions"][0]["hook"] == "h"
    assert result.cost > 0 and result.total_tokens > 0


async def test_copywriter_output_is_pydantic(env) -> None:
    _factory, _repository, _service, _router = env
    result = await _sub_agents()["run_copywriter"].run("[run_copywriter] 写文案")
    assert result.content["hooks"] == ["h1"]
    assert result.content["ctas"] == ["c1"]


async def test_storyboard_planner_output_is_pydantic(env) -> None:
    _factory, _repository, _service, _router = env
    result = await _sub_agents()["run_storyboard_planner"].run("[run_storyboard_planner] 设计分镜")
    assert result.content["scenes"][0]["index"] == 1


async def test_supervisor_grade_rules(env) -> None:
    _factory, _repository, _service, _router = env
    assert Supervisor.grade_from_counts(0, 0) == "A"
    assert Supervisor.grade_from_counts(0, 2) == "A"
    assert Supervisor.grade_from_counts(0, 5) == "B"
    assert Supervisor.grade_from_counts(1, 0) == "C"
    assert Supervisor.grade_from_counts(2, 9) == "C"
    assert Supervisor.grade_from_counts(3, 0) == "D"


async def test_decision_agent_parses_tool_call() -> None:
    agent = DecisionAgent(_sub_agents(), llm_caller=_mock_llm)
    parsed = agent._parse_tool_call('决策：{"tool": "run_copywriter", "prompt": "写一条Hook"}')
    assert parsed["tool"] == "run_copywriter"


async def test_decision_agent_unknown_tool_raises() -> None:
    agent = DecisionAgent(_sub_agents(), llm_caller=_mock_llm)
    with pytest.raises(ValueError):
        agent._parse_tool_call('{"tool": "run_nonexistent"}')


async def test_decision_agent_executes_tool(env) -> None:
    _factory, _repository, _service, _router = env
    agent = DecisionAgent(_sub_agents(), llm_caller=_mock_llm)
    executed = await agent.execute_tool("run_copywriter", "写文案")
    assert executed["tool"] == "run_copywriter"
    assert executed["result"].content["hooks"] == ["h1"]


async def test_sub_agent_retries_on_invalid_output(env) -> None:
    _factory, _repository, _service, _router = env
    calls = {"count": 0}

    async def flaky_llm(text: str) -> str:
        calls["count"] += 1
        if calls["count"] < 3:
            return "not json at all"
        return '{"hooks": ["h"], "ctas": ["c"], "body_copy": "b"}'

    agent = Copywriter(flaky_llm)
    result = await agent.run("写文案")
    assert calls["count"] == 3
    assert result.content["hooks"] == ["h"]


async def test_sub_agent_gives_up_after_max_retries(env) -> None:
    _factory, _repository, _service, _router = env

    async def bad_llm(text: str) -> str:
        return "never valid"

    agent = Copywriter(bad_llm)
    with pytest.raises(RuntimeError, match="failed after"):
        await agent.run("写文案")


async def test_decision_agent_heuristic_dispatch() -> None:
    agent = DecisionAgent(_sub_agents(), llm_caller=None)
    dispatch = await agent.decide("需求", context="")
    assert dispatch["tool"] == "run_content_strategist"


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'l2.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = ContentPlanRepository(factory)
    service = OrchestrationService(repository)
    try:
        yield factory, repository, service, None
    finally:
        await engine.dispose()


async def test_service_uses_decision_agent_pipeline(env) -> None:
    _factory, repository, service, _router = env
    # Build a full pipeline over the mock sub-agents.
    from pixelle_video.orchestration.pipeline import OrchestrationPipeline

    decision_agent = DecisionAgent(_sub_agents(), llm_caller=_mock_llm)
    pipeline = OrchestrationPipeline(
        repository,
        decision_agent,
        supervisor=_sub_agents()["run_supervisor"],
    )
    service.pipeline = pipeline
    from api.schemas.orchestration import PlanCreateRequest

    plan = await service.create_plan(PlanCreateRequest(request_text="做一个短视频"))
    payload = await service.generate(plan.id)
    assert payload["status"] == "awaiting_approval"
    checkpoint = payload["checkpoint"]
    assert set(checkpoint["completed_stages"]) == {
        "intent_classification",
        "audience_analysis",
        "copy_writing",
        "storyboard",
        "supervision",
    }


async def test_api_generate_flow(api_client) -> None:
    client, _factory, _repository, service = api_client
    from pixelle_video.orchestration.agents.decision_agent import DecisionAgent
    from pixelle_video.orchestration.pipeline import OrchestrationPipeline

    decision_agent = DecisionAgent(_sub_agents(), llm_caller=_mock_llm)
    service.pipeline = OrchestrationPipeline(
        _repository, decision_agent, supervisor=_sub_agents()["run_supervisor"]
    )
    created = await client.post("/api/orchestration/plans", json={"request_text": "动画剧集"})
    plan_id = created.json()["id"]
    generated = await client.post(f"/api/orchestration/plans/{plan_id}/generate")
    assert generated.status_code == 200
    assert generated.json()["status"] == "awaiting_approval"


@pytest.fixture
async def api_client(env):
    _factory, _repository, service, _router = env
    app = FastAPI()
    app.include_router(orchestration_router, prefix="/api")
    app.dependency_overrides[get_orchestration_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, service
