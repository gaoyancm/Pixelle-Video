"""Provider-independent contracts for managed media assets."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath


class AssetKind(str, Enum):
    INPUT = "input"
    OUTPUT = "output"


class AssetState(str, Enum):
    AVAILABLE = "available"
    DISABLED = "disabled"
    DELETED = "deleted"
    MISSING = "missing"


class AssetSource(str, Enum):
    UPLOAD = "upload"
    GENERATED = "generated"
    TRUSTED_IMPORT = "trusted_import"


class AssetDirection(str, Enum):
    INPUT = "input"
    OUTPUT = "output"


ALLOWED_MEDIA_TYPES = {
    "image": {
        "image/jpeg": {".jpg", ".jpeg"},
        "image/png": {".png"},
        "image/webp": {".webp"},
    },
    "video": {
        "video/mp4": {".mp4"},
        "video/quicktime": {".mov"},
        "video/webm": {".webm"},
    },
    "audio": {
        "audio/mpeg": {".mp3"},
        "audio/wav": {".wav"},
    },
}

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def new_asset_id() -> str:
    return str(uuid.uuid4())


def require_asset_uuid(value: str) -> str:
    normalized = value.lower()
    if not _UUID.fullmatch(normalized):
        raise ValueError("asset_id must be a UUID")
    return normalized


def validate_object_key(value: str) -> str:
    """Validate an opaque POSIX object key without touching the filesystem."""

    if not value or "\x00" in value or "\\" in value:
        raise ValueError("invalid object key")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise ValueError("object key must be relative")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("object key contains an unsafe segment")
    return posix.as_posix()


@dataclass(frozen=True)
class StoredObject:
    object_key: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ReconciliationReport:
    missing_assets: tuple[str, ...]
    orphan_objects: tuple[str, ...]
    stale_temporary_objects: tuple[str, ...]
    succeeded_jobs_without_outputs: tuple[str, ...]
    unavailable_output_relations: tuple[str, ...]
