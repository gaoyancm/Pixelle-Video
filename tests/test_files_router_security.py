from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

import api.routers.files as files_module
from api.app import app


@pytest.mark.asyncio
async def test_legacy_files_preserves_valid_output_and_redacts_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(files_module, "_PROJECT_ROOT", tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    (output / "ok.mp4").write_bytes(b"video")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        valid = await client.get("/api/files/ok.mp4")
        assert valid.status_code == 200
        assert valid.content == b"video"
        missing = await client.get("/api/files/missing.mp4")
        assert missing.status_code == 404
        assert str(tmp_path) not in missing.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "../secret.txt",
        "%2e%2e/secret.txt",
        "output/%2e%2e/secret.txt",
        r"output%5csecret.txt",
        "C:%5csecret.txt",
    ],
)
async def test_legacy_files_rejects_traversal_and_windows_paths(tmp_path, monkeypatch, path):
    monkeypatch.setattr(files_module, "_PROJECT_ROOT", tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/files/{path}")
    assert response.status_code in {403, 404}
    assert str(tmp_path) not in response.text


@pytest.mark.asyncio
async def test_legacy_files_rejects_symlink_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(files_module, "_PROJECT_ROOT", tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mp4").write_bytes(b"secret")
    output = tmp_path / "output"
    output.mkdir()
    try:
        (output / "link.mp4").symlink_to(outside / "secret.mp4")
    except OSError:
        assert not (output / "link.mp4").exists()
        return
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/files/link.mp4")
    assert response.status_code in {400, 403}
    assert response.content != b"secret"
