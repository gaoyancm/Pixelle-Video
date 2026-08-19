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
API Configuration
"""

from typing import Optional

from pydantic import BaseModel, model_validator


class APIConfig(BaseModel):
    """API configuration"""

    # Server settings
    host: str = "127.0.0.1"  # bind loopback only: local single-user deployment
    port: int = 8000
    reload: bool = False

    # CORS settings — local UI origins only. A wildcard with credentials is
    # rejected at construction so credentials can never leak cross-origin.
    cors_enabled: bool = True
    cors_origins: list[str] = ["http://127.0.0.1:8501", "http://localhost:8501"]

    # Task settings
    max_concurrent_tasks: int = 5
    task_cleanup_interval: int = 3600  # Clean completed tasks every hour
    task_retention_time: int = 86400   # Keep task results for 24 hours

    # File upload settings
    max_upload_size: int = 100 * 1024 * 1024  # 100MB

    # API settings
    api_prefix: str = "/api"
    docs_url: Optional[str] = "/docs"
    redoc_url: Optional[str] = "/redoc"
    openapi_url: Optional[str] = "/openapi.json"

    @model_validator(mode="after")
    def reject_wildcard_origin_with_credentials(self) -> "APIConfig":
        if self.cors_enabled and "*" in self.cors_origins:
            raise ValueError(
                "cors_origins must not use a wildcard: credentials are enabled and "
                "would be sent cross-origin. List the concrete local UI origins instead."
            )
        return self


# Global config instance
api_config = APIConfig()

