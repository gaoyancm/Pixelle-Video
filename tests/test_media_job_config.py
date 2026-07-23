from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from pixelle_video.config.manager import ConfigManager
from pixelle_video.config.schema import MediaJobsConfig, PixelleVideoConfig
from pixelle_video.media_jobs.database import (
    MediaJobsDatabase,
    MediaJobsDisabledError,
    resolve_database_url,
)

PROJECT_ROOT = Path(__file__).parents[1]


def test_config_example_parses_and_media_jobs_are_disabled() -> None:
    payload = yaml.safe_load((PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    config = PixelleVideoConfig(**payload)

    assert config.media_jobs.enabled is False
    assert config.media_jobs.worker_mode == "external"
    assert config.media_jobs.database_url == "sqlite+aiosqlite:///data/media_jobs.db"
    assert config.media_jobs.poll_interval_seconds == 2.0
    assert config.media_jobs.history_poll_interval_seconds == 2.0
    assert config.media_jobs.recovery_scan_interval_seconds == 10.0
    assert config.media_jobs.lease_seconds == 60
    assert config.media_jobs.heartbeat_seconds == 20
    assert config.media_jobs.private_comfyui_enabled is True
    assert config.media_jobs.legacy_providers_enabled is False
    assert config.media_jobs.managed_output_root == "output/media_jobs"
    assert config.media_jobs.managed_asset_root == "data/media_assets"


def test_only_external_worker_mode_is_accepted() -> None:
    with pytest.raises(ValidationError):
        MediaJobsConfig(worker_mode="embedded")


def test_heartbeat_must_be_shorter_than_lease() -> None:
    with pytest.raises(ValidationError, match="heartbeat_seconds"):
        MediaJobsConfig(lease_seconds=20, heartbeat_seconds=20)


def test_disabled_config_does_not_create_or_connect_database(tmp_path: Path) -> None:
    database_path = tmp_path / "must-not-exist.db"
    config = MediaJobsConfig(
        enabled=False,
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )
    database = MediaJobsDatabase(config)

    assert database.is_connected is False
    with pytest.raises(MediaJobsDisabledError):
        database.connect()
    assert not database_path.exists()


def test_example_has_no_remote_database_or_database_password() -> None:
    text = (PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8").lower()

    assert "postgresql://" not in text
    assert "postgresql+" not in text
    assert "database_password" not in text


def test_same_config_file_resolves_sqlite_path_independently_of_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config-home"
    config_dir.mkdir()
    config_path = config_dir / "app.yaml"
    config_path.write_text(
        "media_jobs:\n"
        "  enabled: true\n"
        "  database_url: sqlite+aiosqlite:///data/jobs.db\n",
        encoding="utf-8",
    )
    first_cwd = tmp_path / "first-cwd"
    second_cwd = tmp_path / "second-cwd"
    first_cwd.mkdir()
    second_cwd.mkdir()

    resolved = []
    for cwd in (first_cwd, second_cwd):
        monkeypatch.chdir(cwd)
        ConfigManager._instance = None
        manager = ConfigManager(str(config_path))
        resolved.append(
            resolve_database_url(
                manager.config.media_jobs.database_url,
                base_dir=manager.config.media_jobs.config_base_dir,
            )
        )

    assert resolved[0] == resolved[1]
    assert config_dir.resolve().as_posix() in resolved[0]
    assert not (config_dir / "data").exists()
    assert not (first_cwd / "data" / "jobs.db").exists()
    assert not (second_cwd / "data" / "jobs.db").exists()
    ConfigManager._instance = None


def test_programmatic_relative_sqlite_config_requires_explicit_base_dir() -> None:
    config = MediaJobsConfig(
        enabled=True,
        database_url="sqlite+aiosqlite:///data/jobs.db",
    )
    database = MediaJobsDatabase(config)

    with pytest.raises(ValueError, match="explicit config base directory"):
        database.connect()
    assert database.is_connected is False


def test_disabled_relative_config_has_no_resolution_or_directory_side_effect(
    tmp_path: Path,
) -> None:
    missing_base = tmp_path / "must-not-be-created"
    config = MediaJobsConfig(
        enabled=False,
        database_url="sqlite+aiosqlite:///nested/jobs.db",
    )
    database = MediaJobsDatabase(config, base_dir=missing_base)

    with pytest.raises(MediaJobsDisabledError):
        database.connect()
    assert database.is_connected is False
    assert not missing_base.exists()


def test_absolute_windows_and_posix_sqlite_urls_do_not_use_base_dir(tmp_path: Path) -> None:
    windows_url = "sqlite+aiosqlite:///C:/stable/jobs.db"
    posix_url = "sqlite+aiosqlite:////var/lib/pixelle/jobs.db"

    assert resolve_database_url(windows_url, base_dir=tmp_path) == windows_url
    assert resolve_database_url(posix_url, base_dir=tmp_path) == posix_url


def test_invalid_database_url_error_does_not_echo_credentials() -> None:
    secret = "do-not-expose"

    with pytest.raises(ValueError) as error:
        resolve_database_url(f"not a url {secret}", base_dir=None)
    assert secret not in str(error.value)
