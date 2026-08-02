from __future__ import annotations

import ast
import asyncio
import importlib
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.dependencies import get_media_job_service
from api.routers.media_jobs import router
from api.services.media_jobs import MediaJobApplicationService
from pixelle_video.config.schema import ComfyUINodeConfig, MediaJobsConfig
from pixelle_video.media_jobs import MediaJobsDisabledError
from pixelle_video.media_jobs.models import Base, MediaJob
from pixelle_video.media_jobs.repository import MediaJobRepository
from pixelle_video.media_jobs.state_machine import ErrorCategory, JobStatus
from pixelle_video.services.comfyui_adapter import ComfyUIAdapter, select_comfyui_node


def node_id_for_workflow(workflow: str) -> str:
    return "gpu-4090" if workflow.startswith("gpu_4090") else "a800"


@pytest.fixture
async def api_client(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    service = MediaJobApplicationService(
        MediaJobRepository(sessions),
        MediaJobsConfig(enabled=True, database_url="sqlite+aiosqlite:///:memory:"),
        node_selector=node_id_for_workflow,
    )
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_media_job_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, service, sessions
    await engine.dispose()


def request(workflow: str = "a800_wan22_t2v_33f") -> dict:
    return {"workflow": workflow, "parameters": {"prompt": "safe prompt"}}


@pytest.mark.parametrize(
    "workflow",
    [
        "a800_wan22_t2v_33f",
        "a800_wan22_t2v_81f",
        "gpu_4090_wan21_i2v_33f",
        "gpu_4090_wan21_i2v_81f",
    ],
)
async def test_four_registered_workflows_create_without_worker(api_client, workflow: str):
    client, _, sessions = api_client
    body = request(workflow)
    if workflow.startswith("gpu_4090"):
        body["asset_id"] = "asset-1"
    response = await client.post(
        "/api/media/jobs", json=body, headers={"Idempotency-Key": workflow}
    )
    assert response.status_code == 201
    assert response.json()["workflow"] == workflow
    assert response.json()["status"] == "queued"
    assert "node_id" not in response.json()
    async with sessions() as session:
        stored = await session.get(MediaJob, response.json()["job_id"])
    assert stored is not None
    assert stored.node_id == node_id_for_workflow(workflow)


@pytest.mark.parametrize(
    "body",
    [
        {"workflow": "not-allowed"},
        {**request(), "job_id": "client-controlled"},
        {**request(), "node_id": "client-controlled"},
        {**request(), "retry_of_job_id": "client-controlled"},
        {**request(), "parameters": {"output_prefix": "C:/secret"}},
        {**request(), "asset_ids": ["asset-1"]},
        {**request(), "unknown": True},
    ],
)
async def test_create_rejects_non_allowlisted_or_internal_input(api_client, body: dict):
    client, _, _ = api_client
    response = await client.post(
        "/api/media/jobs", json=body, headers={"Idempotency-Key": "valid-key"}
    )
    assert response.status_code == 422
    expected = (
        "workflow_not_allowed" if body.get("workflow") == "not-allowed" else "invalid_request"
    )
    assert response.json()["error"]["code"] == expected


async def test_create_requires_strict_idempotency_header(api_client):
    client, _, _ = api_client
    for value in (None, "contains space"):
        headers = {} if value is None else {"Idempotency-Key": value}
        response = await client.post("/api/media/jobs", json=request(), headers=headers)
        assert response.status_code == 422


async def test_create_idempotency_and_conflict(api_client):
    client, _, sessions = api_client
    headers = {"Idempotency-Key": "same"}
    first = await client.post("/api/media/jobs", json=request(), headers=headers)
    second = await client.post("/api/media/jobs", json=request(), headers=headers)
    conflict = await client.post(
        "/api/media/jobs",
        json={"workflow": "a800_wan22_t2v_81f", "parameters": {"prompt": "safe prompt"}},
        headers=headers,
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["job_id"] == first.json()["job_id"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    async with sessions() as session:
        stored = await session.get(MediaJob, first.json()["job_id"])
    assert stored is not None
    assert stored.node_id == "a800"


async def test_concurrent_create_has_one_row(api_client):
    client, _, _ = api_client

    async def submit():
        return await client.post(
            "/api/media/jobs",
            json=request(),
            headers={"Idempotency-Key": "concurrent"},
        )

    responses = await asyncio.gather(*(submit() for _ in range(8)))
    ids = {response.json()["job_id"] for response in responses}
    assert ids and len(ids) == 1
    assert sorted(response.status_code for response in responses) == [200] * 7 + [201]


async def test_get_list_filter_pagination_and_no_internal_fields(api_client):
    client, _, sessions = api_client
    ids = []
    for index in range(3):
        response = await client.post(
            "/api/media/jobs",
            json=request(),
            headers={"Idempotency-Key": f"list-{index}"},
        )
        ids.append(response.json()["job_id"])
    async with sessions() as session, session.begin():
        await session.execute(
            update(MediaJob)
            .where(MediaJob.job_id == ids[0])
            .values(status=JobStatus.FAILED.value, error_category="internal", error_message="safe")
        )
    page = await client.get("/api/media/jobs", params={"limit": 1, "offset": 0})
    filtered = await client.get("/api/media/jobs", params={"status": "failed"})
    assert page.status_code == 200
    assert page.json()["has_more"] is True
    assert "total" not in page.json()
    assert [item["job_id"] for item in filtered.json()["items"]] == [ids[0]]
    detail = (await client.get(f"/api/media/jobs/{ids[0]}")).json()
    forbidden = {
        "submission_token",
        "request_hash",
        "lease_owner",
        "workflow_key",
        "idempotency_key",
        "relative_path",
        "comfyui_prompt_id",
    }
    assert forbidden.isdisjoint(detail)
    assert detail["error"]["message"] == "The media job did not complete successfully."


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"status": "bogus"}]
)
async def test_list_validation_is_fixed_error(api_client, params: dict):
    client, _, _ = api_client
    response = await client.get("/api/media/jobs", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


async def test_not_found_and_cancel_is_idempotent(api_client):
    client, _, _ = api_client
    missing = await client.get("/api/media/jobs/absent")
    assert missing.status_code == 404
    created = await client.post(
        "/api/media/jobs", json=request(), headers={"Idempotency-Key": "cancel"}
    )
    url = f"/api/media/jobs/{created.json()['job_id']}/cancel"
    first = await client.post(url)
    second = await client.post(url)
    assert first.status_code == second.status_code == 200
    assert first.json()["cancel_requested"] is True
    assert second.json()["cancel_requested"] is True


async def test_terminal_job_cannot_be_cancelled(api_client):
    client, _, sessions = api_client
    created = await client.post(
        "/api/media/jobs", json=request(), headers={"Idempotency-Key": "terminal"}
    )
    job_id = created.json()["job_id"]
    async with sessions() as session, session.begin():
        await session.execute(
            update(MediaJob).where(MediaJob.job_id == job_id).values(status="succeeded")
        )
    response = await client.post(f"/api/media/jobs/{job_id}/cancel")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_not_cancelable"


async def test_retry_creates_new_lineage_and_isolated_idempotency(api_client):
    client, _, sessions = api_client
    source = await client.post(
        "/api/media/jobs", json=request(), headers={"Idempotency-Key": "shared"}
    )
    source_id = source.json()["job_id"]
    async with sessions() as session, session.begin():
        await session.execute(
            update(MediaJob)
            .where(MediaJob.job_id == source_id)
            .values(status="failed", error_category=ErrorCategory.INTERNAL.value, priority=2)
        )
    headers = {"Idempotency-Key": "shared"}
    first = await client.post(f"/api/media/jobs/{source_id}/retry", headers=headers)
    second = await client.post(f"/api/media/jobs/{source_id}/retry", headers=headers)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["job_id"] == second.json()["job_id"]
    assert first.json()["job_id"] != source_id
    assert first.json()["retry_of_job_id"] == source_id
    assert (await client.get(f"/api/media/jobs/{source_id}")).json()["status"] == "failed"
    async with sessions() as session:
        parent = await session.get(MediaJob, source_id)
        child = await session.get(MediaJob, first.json()["job_id"])
    assert parent is not None and child is not None
    assert parent.node_id == child.node_id == "a800"
    assert parent.priority == child.priority == 2


async def test_create_without_matching_enabled_node_is_redacted_and_inserts_nothing(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'no-node.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    def no_node(_workflow: str) -> str:
        raise RuntimeError("private node details must stay hidden")

    provider_calls: list[str] = []

    async def forbidden_provider(request: httpx.Request) -> httpx.Response:
        provider_calls.append(request.url.path)
        return httpx.Response(500)

    provider_transport = httpx.MockTransport(forbidden_provider)
    service = MediaJobApplicationService(
        MediaJobRepository(sessions),
        MediaJobsConfig(enabled=True),
        node_selector=no_node,
    )
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_media_job_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/media/jobs",
            json=request(),
            headers={"Idempotency-Key": "no-node"},
        )
    async with sessions() as session:
        count = (await session.execute(select(func.count()).select_from(MediaJob))).scalar_one()
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "service_unavailable",
            "message": "Persistent media jobs are unavailable.",
        }
    }
    assert count == 0
    assert provider_calls == []
    assert provider_transport is not None
    await engine.dispose()


async def test_api_binding_matches_adapter_first_enabled_node(api_client):
    _, _, sessions = api_client
    configured_nodes = [
        {
            "id": "disabled-first",
            "name": "disabled",
            "base_url": "http://disabled.invalid",
            "workflow_types": ["a800_wan22_t2v_33f"],
            "enabled": False,
        },
        {
            "id": "a800",
            "name": "selected",
            "base_url": "http://mock.invalid",
            "workflow_types": ["a800_wan22_t2v_33f"],
            "enabled": True,
        },
        {
            "id": "later-match",
            "name": "later",
            "base_url": "http://later.invalid",
            "workflow_types": ["a800_wan22_t2v_33f"],
            "enabled": True,
        },
    ]
    adapter = ComfyUIAdapter(configured_nodes)
    parsed_nodes = [ComfyUINodeConfig(**node) for node in configured_nodes]
    expected = select_comfyui_node(parsed_nodes, "a800_wan22_t2v_33f").id
    assert adapter.select_node("a800_wan22_t2v_33f").id == expected
    service = MediaJobApplicationService(
        MediaJobRepository(sessions),
        MediaJobsConfig(enabled=True),
        node_selector=lambda workflow: select_comfyui_node(parsed_nodes, workflow).id,
    )
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_media_job_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/media/jobs",
            json=request(),
            headers={"Idempotency-Key": "multi-node"},
        )
    async with sessions() as session:
        stored = await session.get(MediaJob, response.json()["job_id"])
    assert response.status_code == 201
    assert expected == "a800"
    assert stored is not None and stored.node_id == expected


@pytest.mark.parametrize("workflow", ["gpu_4090_wan21_i2v_33f", "gpu_4090_wan21_i2v_81f"])
async def test_i2v_accepts_single_asset_and_converts_to_internal_list(api_client, workflow):
    client, _, sessions = api_client
    response = await client.post(
        "/api/media/jobs",
        json={**request(workflow), "asset_id": "managed.asset-1"},
        headers={"Idempotency-Key": f"asset-{workflow}"},
    )
    assert response.status_code == 201
    async with sessions() as session:
        job = await session.get(MediaJob, response.json()["job_id"])
    assert job.input_assets_json == [{"asset_id": "managed.asset-1", "role": "input_image"}]


@pytest.mark.parametrize(
    "body",
    [
        request("gpu_4090_wan21_i2v_33f"),
        {**request("gpu_4090_wan21_i2v_33f"), "asset_ids": ["asset-1"]},
        {**request("a800_wan22_t2v_33f"), "asset_id": "asset-1"},
        {**request("gpu_4090_wan21_i2v_33f"), "asset_id": ""},
        {**request("gpu_4090_wan21_i2v_33f"), "asset_id": " "},
        {**request("gpu_4090_wan21_i2v_33f"), "asset_id": "bad/path"},
        {**request("gpu_4090_wan21_i2v_33f"), "asset_id": "a" * 129},
    ],
)
async def test_asset_contract_rejects_invalid_external_shapes_without_rows(api_client, body):
    client, _, sessions = api_client
    response = await client.post(
        "/api/media/jobs", json=body, headers={"Idempotency-Key": "invalid-asset"}
    )
    assert response.status_code == 422
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MediaJob)) == 0


@pytest.mark.parametrize(
    ("workflow", "parameters"),
    [
        ("a800_wan22_t2v_33f", {"prompt": "p", "width": 1}),
        ("a800_wan22_t2v_33f", {"prompt": "p" * 4096}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "width": 16_384}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "height": 1}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "height": 16_384}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "frame_count": 1}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "frame_count": 10_000}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "seed": 0}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "seed": 18_446_744_073_709_551_615}),
        (
            "gpu_4090_wan21_i2v_33f",
            {"prompt": "p", "negative_prompt": "", "steps": 1, "cfg": 0},
        ),
        (
            "gpu_4090_wan21_i2v_33f",
            {"prompt": "p", "negative_prompt": "n" * 4096, "steps": 1000, "cfg": 100},
        ),
    ],
)
async def test_public_parameter_valid_boundaries(api_client, workflow, parameters):
    client, _, _ = api_client
    body = {"workflow": workflow, "parameters": parameters}
    if workflow.startswith("gpu_4090"):
        body["asset_id"] = "asset-1"
    response = await client.post(
        "/api/media/jobs", json=body, headers={"Idempotency-Key": f"valid-{id(parameters)}"}
    )
    assert response.status_code == 201


@pytest.mark.parametrize(
    ("workflow", "parameters"),
    [
        ("a800_wan22_t2v_33f", {"prompt": ""}),
        ("a800_wan22_t2v_33f", {"prompt": "   "}),
        ("a800_wan22_t2v_33f", {"prompt": {"nested": "object"}}),
        ("a800_wan22_t2v_33f", {"prompt": "p" * 4097}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "negative_prompt": []}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "negative_prompt": 1}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "negative_prompt": "n" * 4097}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "width": "not-an-integer"}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "width": True}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "width": 0}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "width": 16_385}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "height": 0}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "height": "1"}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "height": 16_385}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "frame_count": 0}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "frame_count": "1"}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "frame_count": 10_001}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "seed": -1}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "seed": "1"}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "seed": 18_446_744_073_709_551_616}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "steps": 1}),
        ("gpu_4090_wan21_i2v_33f", {"prompt": "p", "steps": -999}),
        ("gpu_4090_wan21_i2v_33f", {"prompt": "p", "steps": "1"}),
        ("gpu_4090_wan21_i2v_33f", {"prompt": "p", "steps": 1001}),
        ("gpu_4090_wan21_i2v_33f", {"prompt": "p", "cfg": "bad"}),
        ("gpu_4090_wan21_i2v_33f", {"prompt": "p", "cfg": True}),
        ("gpu_4090_wan21_i2v_33f", {"prompt": "p", "cfg": -0.1}),
        ("gpu_4090_wan21_i2v_33f", {"prompt": "p", "cfg": 101}),
        ("a800_wan22_t2v_33f", {"prompt": "p", "unknown": 1}),
    ],
)
async def test_invalid_public_parameters_are_rejected_without_side_effects(
    api_client, workflow, parameters
):
    client, _, sessions = api_client
    body = {"workflow": workflow, "parameters": parameters}
    if workflow.startswith("gpu_4090"):
        body["asset_id"] = "asset-1"
    response = await client.post(
        "/api/media/jobs", json=body, headers={"Idempotency-Key": "invalid-parameter"}
    )
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "invalid_request", "message": "The request is invalid."}
    }
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MediaJob)) == 0


def test_non_finite_cfg_is_rejected_before_persistence():
    from pydantic import ValidationError

    from api.schemas.media_jobs import MediaJobRequest

    with pytest.raises(ValidationError):
        MediaJobRequest.model_validate(
            {
                "workflow": "gpu_4090_wan21_i2v_33f",
                "parameters": {"prompt": "p", "cfg": float("inf")},
                "asset_id": "asset-1",
            }
        )


async def test_parameter_serialized_size_limit_is_enforced_without_side_effects(api_client):
    client, _, sessions = api_client
    response = await client.post(
        "/api/media/jobs",
        json={
            "workflow": "a800_wan22_t2v_33f",
            "parameters": {"prompt": "😀" * 4096, "negative_prompt": "😀" * 4096},
        },
        headers={"Idempotency-Key": "oversized-parameters"},
    )
    assert response.status_code == 422
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MediaJob)) == 0


async def test_pagination_is_stable_complete_filtered_and_does_not_leak_extra_row(api_client):
    client, _, sessions = api_client
    ids = []
    for index in range(5):
        response = await client.post(
            "/api/media/jobs",
            json=request(),
            headers={"Idempotency-Key": f"page-{index}"},
        )
        ids.append(response.json()["job_id"])
    same_time = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    async with sessions() as session, session.begin():
        await session.execute(update(MediaJob).values(created_at=same_time))
        await session.execute(
            update(MediaJob)
            .where(MediaJob.job_id.in_(ids[:3]))
            .values(status=JobStatus.FAILED.value, error_category=ErrorCategory.INTERNAL.value)
        )

    expected = sorted(ids, reverse=True)
    first = (await client.get("/api/media/jobs", params={"limit": 2, "offset": 0})).json()
    second = (await client.get("/api/media/jobs", params={"limit": 2, "offset": 2})).json()
    last = (await client.get("/api/media/jobs", params={"limit": 2, "offset": 4})).json()
    combined = [item["job_id"] for page in (first, second, last) for item in page["items"]]
    assert combined == expected
    assert len(first["items"]) == len(second["items"]) == 2
    assert len(last["items"]) == 1
    assert first["has_more"] is second["has_more"] is True
    assert last["has_more"] is False
    assert all("total" not in page for page in (first, second, last))

    filtered = (
        await client.get("/api/media/jobs", params={"status": "failed", "limit": 2, "offset": 0})
    ).json()
    expected_failed = [job_id for job_id in expected if job_id in ids[:3]]
    assert [item["job_id"] for item in filtered["items"]] == expected_failed[:2]
    assert filtered["has_more"] is (len(expected_failed) > 2)


async def test_api_concurrent_retry_and_operation_source_scopes_are_isolated(api_client):
    client, _, sessions = api_client
    source_ids = []
    for index in range(2):
        created = await client.post(
            "/api/media/jobs",
            json=request(),
            headers={"Idempotency-Key": "shared-operation-key" if index == 0 else "source-two"},
        )
        source_ids.append(created.json()["job_id"])
    async with sessions() as session, session.begin():
        await session.execute(
            update(MediaJob)
            .where(MediaJob.job_id.in_(source_ids))
            .values(status=JobStatus.FAILED.value, error_category=ErrorCategory.INTERNAL.value)
        )

    async def retry(source_id):
        return await client.post(
            f"/api/media/jobs/{source_id}/retry",
            headers={"Idempotency-Key": "shared-operation-key"},
        )

    concurrent = await asyncio.gather(*(retry(source_ids[0]) for _ in range(8)))
    child_ids = {response.json()["job_id"] for response in concurrent}
    assert len(child_ids) == 1
    assert sorted(response.status_code for response in concurrent) == [200] * 7 + [201]
    other = await retry(source_ids[1])
    assert other.status_code == 201
    assert other.json()["job_id"] not in child_ids
    assert other.json()["retry_of_job_id"] == source_ids[1]
    async with sessions() as session:
        parents = (
            await session.execute(select(MediaJob).where(MediaJob.job_id.in_(source_ids)))
        ).scalars()
        children = (
            await session.execute(select(MediaJob).where(MediaJob.retry_of_job_id.in_(source_ids)))
        ).scalars()
        assert all(parent.status == JobStatus.FAILED.value for parent in parents)
        assert len(list(children)) == 2


async def test_unknown_exception_is_fixed_redacted_json():
    class BrokenService:
        async def get(self, job_id):
            raise RuntimeError("secret-token C:\\private\\database.db")

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_media_job_service] = lambda: BrokenService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/media/jobs/job-1")
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "error": {"code": "internal_error", "message": "An internal error occurred."}
    }
    assert "secret-token" not in response.text
    assert "private" not in response.text


async def test_service_unavailable_and_known_errors_keep_fixed_mappings():
    async def disabled():
        raise MediaJobsDisabledError("sensitive DSN")

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_media_job_service] = disabled
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        unavailable = await client.get("/api/media/jobs/job-1")
    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "error": {
            "code": "service_unavailable",
            "message": "Persistent media jobs are unavailable.",
        }
    }
    assert "DSN" not in unavailable.text


@pytest.mark.parametrize(
    ("status", "category"),
    [
        ("queued", None),
        ("running", None),
        ("succeeded", None),
        ("cancelled", None),
        ("failed", ErrorCategory.SUBMISSION_UNKNOWN.value),
    ],
)
async def test_non_retryable_statuses_are_rejected(api_client, status: str, category: str | None):
    client, _, sessions = api_client
    source = await client.post(
        "/api/media/jobs",
        json=request(),
        headers={"Idempotency-Key": f"source-{status}-{category}"},
    )
    source_id = source.json()["job_id"]
    async with sessions() as session, session.begin():
        await session.execute(
            update(MediaJob)
            .where(MediaJob.job_id == source_id)
            .values(status=status, error_category=category)
        )
    response = await client.post(
        f"/api/media/jobs/{source_id}/retry",
        headers={"Idempotency-Key": "retry-key"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_not_retryable"


def test_router_does_not_import_worker_executor_comfyui_or_legacy_task_manager():
    source = Path("api/routers/media_jobs.py").read_text(encoding="utf-8")
    assert "TaskManager" not in source
    assert "ComfyUI" not in source
    assert "MediaJobWorker" not in source
    assert "AsyncSession" not in source


def test_router_ast_dependencies_and_app_boundaries_remain_isolated():
    tree = ast.parse(Path("api/routers/media_jobs.py").read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    forbidden = (
        "api.tasks",
        "pixelle_video.media_jobs.worker",
        "pixelle_video.media_jobs.executor",
        "pixelle_video.services.comfyui",
    )
    assert not any(name.startswith(prefix) for name in imports for prefix in forbidden)

    from api.app import app

    paths = {route.path for route in app.routes}
    assert "/api/tasks/{task_id}" in paths
    assert "/api/video/generate/sync" in paths
    assert "/api/video/generate/async" in paths
    assert sum(path.startswith("/api/media/jobs") for path in paths) == 4


def test_importing_new_router_has_no_database_or_worker_side_effect(monkeypatch):
    import api.dependencies as dependencies

    def forbidden_connect(self):
        raise AssertionError("router import must not connect to a database")

    monkeypatch.setattr(dependencies.MediaJobsDatabase, "connect", forbidden_connect)
    reloaded = importlib.reload(importlib.import_module("api.routers.media_jobs"))
    assert len(reloaded.router.routes) == 5


def test_openapi_contains_exactly_the_five_phase2c_operations():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    operations = sum(
        len(methods)
        for path, methods in app.openapi()["paths"].items()
        if path.startswith("/api/media/jobs")
    )
    assert operations == 5
    schema = app.openapi()
    request_schema = schema["components"]["schemas"]["MediaJobRequest"]
    response_schema = schema["components"]["schemas"]["MediaJobResponse"]
    request_properties = request_schema["properties"]
    response_properties = response_schema["properties"]
    assert "asset_id" in request_properties
    assert "asset_ids" not in request_properties
    assert "node_id" not in request_properties
    assert "node_id" not in response_properties
    forbidden = {
        "submission_token",
        "request_hash",
        "lease_owner",
        "comfyui_prompt_id",
        "provider",
        "node_id",
        "version",
        "relative_path",
    }
    phase2c_paths = {
        path: methods
        for path, methods in schema["paths"].items()
        if path.startswith("/api/media/jobs")
    }
    assert all(name not in str(phase2c_paths) for name in forbidden)
