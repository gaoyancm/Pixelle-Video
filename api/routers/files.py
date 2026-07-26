# Copyright (C) 2025 AIDC-AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
File service endpoints

Provides access to generated files (videos, images, audio) and resource files.
"""

from pathlib import Path, PurePosixPath, PureWindowsPath

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from loguru import logger

router = APIRouter(prefix="/files", tags=["Files"])
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ALLOWED_ROOTS = (
    "output",
    "workflows",
    "templates",
    "bgm",
    "data/bgm",
    "data/templates",
    "resources",
)


@router.get("/{file_path:path}")
async def get_file(file_path: str):
    """
    Get file by path
    
    Serves files from allowed directories:
    - output/ - Generated files (videos, images, audio)
    - workflows/ - ComfyUI workflow files
    - templates/ - HTML templates
    - bgm/ - Background music
    - data/bgm/ - Custom background music
    - data/templates/ - Custom templates
    - resources/ - Other resources (images, fonts, etc.)
    
    - **file_path**: File path relative to allowed directories
    
    Examples:
    - "abc123.mp4" → output/abc123.mp4
    - "workflows/runninghub/image_flux.json" → workflows/runninghub/image_flux.json
    - "templates/1080x1920/default.html" → templates/1080x1920/default.html
    - "bgm/default.mp3" → bgm/default.mp3
    - "resources/example.png" → resources/example.png
    
    Returns file for download or preview.
    """
    try:
        if (
            not file_path
            or "\x00" in file_path
            or "\\" in file_path
            or PurePosixPath(file_path).is_absolute()
            or PureWindowsPath(file_path).is_absolute()
            or PureWindowsPath(file_path).drive
            or any(part in {"", ".", ".."} for part in file_path.split("/"))
        ):
            raise HTTPException(status_code=403, detail="Access denied")

        selected_root = "output"
        relative_parts = file_path.split("/")
        for allowed in sorted(_ALLOWED_ROOTS, key=len, reverse=True):
            allowed_parts = allowed.split("/")
            if relative_parts[: len(allowed_parts)] == allowed_parts:
                selected_root = allowed
                relative_parts = relative_parts[len(allowed_parts) :]
                break
        if not relative_parts:
            raise HTTPException(status_code=400, detail="Path is not a file")

        allowed_root = (_PROJECT_ROOT / selected_root).resolve()
        candidate = allowed_root.joinpath(*relative_parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(allowed_root)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="File not found") from None
        except ValueError:
            raise HTTPException(status_code=403, detail="Access denied") from None
        if candidate.is_symlink() or not resolved.is_file():
            raise HTTPException(status_code=400, detail="Path is not a regular file")

        # Determine media type
        suffix = resolved.suffix.lower()
        media_types = {
            '.mp4': 'video/mp4',
            '.mp3': 'audio/mpeg',
            '.wav': 'audio/wav',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.html': 'text/html',
            '.json': 'application/json',
        }
        media_type = media_types.get(suffix, 'application/octet-stream')
        
        # Use inline disposition for browser preview
        return FileResponse(
            path=str(resolved),
            media_type=media_type,
            headers={
                "Content-Disposition": f'inline; filename="{resolved.name}"'
            }
        )
        
    except HTTPException:
        raise
    except Exception:
        logger.error("Unhandled legacy file access error")
        raise HTTPException(status_code=500, detail="Internal file access error") from None
