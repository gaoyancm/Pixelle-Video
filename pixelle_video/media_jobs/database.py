"""Lazy asynchronous database initialization for persistent media jobs."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from pixelle_video.config.schema import MediaJobsConfig


class MediaJobsDisabledError(RuntimeError):
    pass


def sqlite_url_for_path(path: str | Path) -> str:
    """Build a cross-platform aiosqlite URL for an absolute filesystem path."""

    return f"sqlite+aiosqlite:///{Path(path).resolve().as_posix()}"


def resolve_database_url(database_url: str, *, base_dir: str | Path | None) -> str:
    """Resolve a relative SQLite file against a stable, explicit directory."""

    try:
        url = make_url(database_url)
    except Exception:
        raise ValueError("invalid media jobs database URL") from None
    if url.get_backend_name() != "sqlite" or url.database in {None, "", ":memory:"}:
        return database_url

    database = url.database
    is_absolute = (
        Path(database).is_absolute()
        or PureWindowsPath(database).is_absolute()
        or PurePosixPath(database).is_absolute()
    )
    if is_absolute:
        return database_url
    if base_dir is None:
        raise ValueError(
            "relative SQLite media-jobs URL requires an explicit config base directory"
        )
    absolute_path = Path(base_dir).resolve() / Path(database)
    return url.set(database=absolute_path.resolve().as_posix()).render_as_string(
        hide_password=False
    )


def create_media_jobs_engine(database_url: str) -> AsyncEngine:
    """Create an async engine without logging its potentially sensitive URL."""

    try:
        url = make_url(database_url)
        engine = create_async_engine(database_url, pool_pre_ping=True)
    except Exception:
        raise RuntimeError("failed to initialize media jobs database engine") from None
    if url.get_backend_name() == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _configure_sqlite(dbapi_connection, connection_record) -> None:
            del connection_record
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

    return engine


class MediaJobsDatabase:
    """Own a lazy engine and session factory.

    Constructing this object while jobs are disabled performs no connection and
    therefore cannot create a SQLite database file.
    """

    def __init__(self, config: MediaJobsConfig, *, base_dir: str | Path | None = None):
        self.config = config
        self._base_dir = base_dir if base_dir is not None else config.config_base_dir
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def is_connected(self) -> bool:
        return self._engine is not None

    def connect(self) -> async_sessionmaker[AsyncSession]:
        if not self.config.enabled:
            raise MediaJobsDisabledError("persistent media jobs are disabled")
        if self._session_factory is None:
            resolved_url = resolve_database_url(
                self.config.database_url,
                base_dir=self._base_dir,
            )
            self._engine = create_media_jobs_engine(resolved_url)
            self._session_factory = async_sessionmaker(
                self._engine,
                expire_on_commit=False,
                class_=AsyncSession,
            )
        return self._session_factory

    async def dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
        self._engine = None
        self._session_factory = None

    async def verify_connection(self) -> None:
        """Execute a lightweight round-trip to prove the database is reachable.

        Used by the worker readiness probe (``--check``) so a launcher can
        distinguish "worker initialized" from "worker can actually talk to the
        database". Raises ``MediaJobsDisabledError`` when jobs are disabled.
        """

        factory = self.connect()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
