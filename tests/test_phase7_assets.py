"""Phase 07 C1 character/scene/prop asset library tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import pixelle_video.management.models as _management_models  # noqa: F401
from api.dependencies import get_anime_service
from api.routers.anime import router as anime_router
from api.services.anime import AnimeApplicationService, auto_fill_anchors
from pixelle_video.anime.repository import AnimeRepository
from pixelle_video.media_jobs.models import Base


@pytest.fixture
async def env(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'c1.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repository = AnimeRepository(factory)
    service = AnimeApplicationService(repository)
    try:
        yield factory, repository, service
    finally:
        await engine.dispose()


def _character_body(**extra) -> dict:
    body = {
        "name": "李逍遥",
        "description": "蜀山弟子，潇洒不羁",
        "project_id": "project-x",
        "role_type": "主角",
        "identity_anchors": {
            "bone_structure": {"face_shape": "oval"},
            "facial_features": {"eye_shape": "almond"},
            "unique_marks": [],
            "color_palette": {"hair": "#1A1A1A"},
            "texture": {"skin_texture": "smooth"},
            "hair": {"hair_style": "束发高髻"},
        },
        "static_features": {"gender": "男", "height": "175"},
        "dynamic_features": {"costume": "蓝色道袍"},
        "reference_images": [{"view": "front", "asset_id": "asset-1"}],
    }
    body.update(extra)
    return body


async def test_repository_create_character(env) -> None:
    _factory, repository, _service = env
    character = await repository.create_character(**_character_body())
    assert character.name == "李逍遥"
    assert character.identity_anchors_json["bone_structure"]["face_shape"] == "oval"
    assert character.static_features_json["gender"] == "男"
    assert character.reference_images_json[0]["asset_id"] == "asset-1"


async def test_repository_get_and_update_character(env) -> None:
    _factory, repository, _service = env
    character = await repository.create_character(**_character_body())
    fetched = await repository.get_character(character.id)
    assert fetched is not None and fetched.role_type == "主角"
    updated = await repository.update_character(
        character.id, reference_images=[{"view": "back", "asset_id": "asset-2"}]
    )
    assert updated.reference_images_json[0]["asset_id"] == "asset-2"


async def test_repository_list_characters(env) -> None:
    _factory, repository, _service = env
    await repository.create_character(**_character_body())
    await repository.create_character(**_character_body(name="赵灵儿"))
    rows, has_more = await repository.list_characters("project-x", limit=10, offset=0)
    assert len(rows) == 2
    rows, _ = await repository.list_characters("other", limit=10, offset=0)
    assert len(rows) == 0


async def test_repository_scene_and_prop(env) -> None:
    _factory, repository, _service = env
    scene = await repository.create_scene_asset(
        name="蜀山剑派大殿",
        description="仙气缭绕的大殿",
        project_id="project-x",
        environment={"time": "黄昏", "weather": "晴朗"},
    )
    assert scene.environment_json["time"] == "黄昏"
    prop = await repository.create_prop(name="青铜宝剑", project_id="project-x")
    assert prop.name == "青铜宝剑"


async def test_auto_fill_anchors_has_six_layers(env) -> None:
    _factory, _repository, _service = env
    anchors = auto_fill_anchors("一位仙侠主角")
    assert {
        "bone_structure",
        "facial_features",
        "unique_marks",
        "color_palette",
        "texture",
        "hair",
    } <= set(anchors)


async def test_service_create_with_auto_fill(env) -> None:
    _factory, _repository, service = env
    body = _character_body(identity_anchors={}, auto_fill_anchors=True)
    from api.schemas.anime import CharacterCreate

    character = await service.create_character(CharacterCreate(**body))
    assert character.identity_anchors_json["hair"]["hair_style"] == "束发高髻"


async def test_service_update_character(env) -> None:
    _factory, repository, service = env
    character = await repository.create_character(**_character_body())
    from api.schemas.anime import CharacterUpdate

    updated = await service.update_character(
        character.id, CharacterUpdate(description="更新后的描述")
    )
    assert updated.description == "更新后的描述"


@pytest.fixture
async def api_client(env):
    _factory, _repository, service = env
    app = FastAPI()
    app.include_router(anime_router, prefix="/api")
    app.dependency_overrides[get_anime_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, _factory, _repository, service


async def test_api_create_character(api_client) -> None:
    client, _factory, _repository, _service = api_client
    created = await client.post("/api/anime/characters", json=_character_body())
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "李逍遥"
    assert body["identity_anchors"]["bone_structure"]["face_shape"] == "oval"


async def test_api_list_characters(api_client) -> None:
    client, _factory, repository, _service = api_client
    await repository.create_character(**_character_body())
    listing = await client.get("/api/anime/projects/project-x/characters")
    assert listing.status_code == 200
    assert len(listing.json()["items"]) == 1


async def test_api_create_scene_and_prop(api_client) -> None:
    client, _factory, _repository, _service = api_client
    scene = await client.post(
        "/api/anime/scenes",
        json={"name": "蜀山大殿", "description": "仙气缭绕", "project_id": "project-x"},
    )
    assert scene.status_code == 201
    assert scene.json()["name"] == "蜀山大殿"
    prop = await client.post("/api/anime/props", json={"name": "青铜剑", "project_id": "project-x"})
    assert prop.status_code == 201
