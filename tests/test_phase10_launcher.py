"""Phase 10 task 4: launcher readiness probes.

Covers the G4 acceptance gate without opening a browser or contacting a GPU:
- ``database.verify_connection`` proves the worker can reach its database;
- ``DispatchingJobProcessor.processor_kinds`` reports the loaded registry;
- ``worker_cli --check`` is the subprocess readiness probe the launcher uses;
- the preflight CLI exits 0/1 as the launcher expects.
"""

from __future__ import annotations

import functools
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from pixelle_video.config.schema import MediaJobsConfig, PixelleVideoConfig
from pixelle_video.media_jobs.database import (
    MediaJobsDatabase,
    MediaJobsDisabledError,
)
from pixelle_video.media_jobs.worker_cli import _build_llm_caller

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_worker_llm_caller_accepts_environment_key(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-secret")
    config = PixelleVideoConfig(
        llm={
            "api_key": "",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
        }
    )
    caller = _build_llm_caller(config)
    assert isinstance(caller, functools.partial)
    assert caller.keywords == {
        "model": "deepseek-chat",
        "api_key": "environment-secret",
        "base_url": "https://api.deepseek.com",
    }


def _write_config(tmp_path: Path, *, enabled: bool) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "project_name: test",
                "media_jobs:",
                f"  enabled: {str(enabled).lower()}",
                f"  database_url: sqlite+aiosqlite:///{(tmp_path / 'jobs.db').as_posix()}",
                "  private_comfyui_enabled: false",
                "comfyui:",
                "  nodes: []",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def test_verify_connection_round_trips_when_enabled(tmp_path: Path) -> None:
    import asyncio

    config = MediaJobsConfig(
        enabled=True,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'jobs.db').as_posix()}",
    )
    database = MediaJobsDatabase(config)

    async def _run() -> None:
        await database.verify_connection()
        await database.dispose()

    asyncio.run(_run())


def test_verify_connection_raises_when_disabled(tmp_path: Path) -> None:
    import asyncio

    config = MediaJobsConfig(enabled=False)
    database = MediaJobsDatabase(config)

    async def _run() -> None:
        with pytest.raises(MediaJobsDisabledError):
            await database.verify_connection()

    asyncio.run(_run())


def test_processor_kinds_reports_registry() -> None:
    from pixelle_video.media_jobs.dispatcher import DispatchingJobProcessor

    # processor_kinds() only reads the registry; no repository/DB is touched.
    dispatcher = DispatchingJobProcessor(
        repository=object(),  # type: ignore[arg-type]
        processors={"private_comfyui": object(), "llm_caption": object()},  # type: ignore[dict-item]
    )
    assert dispatcher.processor_kinds() == ["llm_caption", "private_comfyui"]


def test_worker_check_succeeds_when_enabled(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, enabled=True)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pixelle_video.media_jobs.worker_cli",
            "--config",
            str(config_path),
            "--check",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "worker ready" in result.stdout
    assert "private_comfyui" in result.stdout
    assert "llm_caption" in result.stdout


def test_worker_check_fails_when_disabled(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, enabled=False)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pixelle_video.media_jobs.worker_cli",
            "--config",
            str(config_path),
            "--check",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "persistent media jobs are disabled" in (result.stderr + result.stdout)


def test_preflight_cli_exits_zero_when_private_comfyui_is_disabled(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, enabled=False)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pixelle_video.services.preflight",
            "--config",
            str(config_path),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "Restricted mode" not in result.stdout


def test_start_script_check_only_invokes_real_launcher_guards(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, enabled=True)
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PROJECT_ROOT / "scripts" / "start.ps1"),
            "-NoBrowser",
            "-CheckOnly",
            "-ConfigPath",
            str(config_path),
            "-PythonPath",
            sys.executable,
            "-ApiPort",
            "43181",
            "-UiPort",
            "43182",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "no service was started" in result.stdout


def test_start_script_never_kills_foreign_port_owner(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, enabled=True)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    occupied_port = listener.getsockname()[1]
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(PROJECT_ROOT / "scripts" / "start.ps1"),
                "-NoBrowser",
                "-CheckOnly",
                "-ConfigPath",
                str(config_path),
                "-PythonPath",
                sys.executable,
                "-ApiPort",
                str(occupied_port),
                "-UiPort",
                str(occupied_port + 1),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 1
        assert "foreign process was NOT killed" in result.stdout
        assert listener.fileno() >= 0
    finally:
        listener.close()


def test_preflight_cli_exits_nonzero_for_unknown_workflow(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "project_name: test",
                "comfyui:",
                "  nodes:",
                "    - id: bad",
                "      name: bad",
                "      base_url: http://127.0.0.1:9999",
                "      workflow_types: [bogus_workflow]",
                "      enabled: true",
            ]
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pixelle_video.services.preflight",
            "--config",
            str(config_path),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "unknown workflow type 'bogus_workflow'" in result.stdout
