"""Safe local object store for managed media assets."""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from .contracts import StoredObject, validate_object_key


class StoreBoundaryError(ValueError):
    pass


class ObjectTooLargeError(ValueError):
    pass


class LocalAssetStore:
    """Filesystem implementation with opaque keys and repeated boundary checks."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _path(self, object_key: str, *, require_exists: bool = False) -> Path:
        key = validate_object_key(object_key)
        candidate = self.root.joinpath(*key.split("/"))
        resolved = candidate.resolve(strict=require_exists)
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise StoreBoundaryError("managed object escapes the configured root") from None
        current = self.root
        for part in key.split("/")[:-1]:
            current = current / part
            if current.exists() and current.is_symlink():
                raise StoreBoundaryError("managed object traverses a link")
        if candidate.exists() and candidate.is_symlink():
            raise StoreBoundaryError("managed object is a link")
        return candidate

    @staticmethod
    def new_object_key(asset_id: str, suffix: str) -> str:
        safe_suffix = suffix.lower() if suffix and len(suffix) <= 10 else ""
        return validate_object_key(f"objects/{asset_id[:2]}/{asset_id}{safe_suffix}")

    def write_stream(
        self,
        source: BinaryIO,
        *,
        object_key: str,
        max_bytes: int,
        chunk_size: int = 1024 * 1024,
    ) -> StoredObject:
        destination = self._path(object_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.root / ".tmp" / f"{uuid.uuid4().hex}.part"
        temporary.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as target:
                while True:
                    chunk = source.read(chunk_size)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise ObjectTooLargeError("asset exceeds the configured size limit")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if size == 0:
                raise ValueError("empty assets are not allowed")
            if destination.exists():
                raise FileExistsError("managed object already exists")
            temporary.replace(destination)
            return StoredObject(object_key, size, digest.hexdigest())
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def open(self, object_key: str) -> BinaryIO:
        path = self._path(object_key, require_exists=True)
        if not path.is_file():
            raise FileNotFoundError("managed object was not found")
        return path.open("rb")

    def local_path(self, object_key: str) -> Path:
        """Return a validated internal path for trusted local Provider adapters."""

        path = self._path(object_key, require_exists=True)
        if not path.is_file():
            raise FileNotFoundError("managed object was not found")
        return path

    def exists(self, object_key: str) -> bool:
        try:
            path = self._path(object_key, require_exists=True)
        except FileNotFoundError:
            return False
        return path.is_file()

    def delete(self, object_key: str) -> None:
        path = self._path(object_key, require_exists=True)
        if not path.is_file():
            raise FileNotFoundError("managed object was not found")
        path.unlink()

    def iter_object_keys(self) -> Iterator[str]:
        if not self.root.exists():
            return
        objects = self.root / "objects"
        if not objects.exists():
            return
        for path in objects.rglob("*"):
            if path.is_file() and not path.is_symlink():
                resolved = path.resolve()
                try:
                    resolved.relative_to(self.root)
                except ValueError:
                    continue
                yield path.relative_to(self.root).as_posix()

    def iter_stale_temporary_keys(self, *, older_than_timestamp: float) -> Iterator[str]:
        temporary_root = self.root / ".tmp"
        if not temporary_root.exists():
            return
        for path in temporary_root.glob("*.part"):
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime < older_than_timestamp:
                yield path.relative_to(self.root).as_posix()
