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
API Routers
"""

from api.routers.audit_budget import router as audit_budget_router
from api.routers.content import router as content_router
from api.routers.experiments import router as experiments_router
from api.routers.files import router as files_router
from api.routers.frame import router as frame_router
from api.routers.health import router as health_router
from api.routers.image import router as image_router
from api.routers.knowledge import router as knowledge_router
from api.routers.llm import router as llm_router
from api.routers.management import router as management_router
from api.routers.media_assets import router as media_assets_router
from api.routers.media_jobs import router as media_jobs_router
from api.routers.products import router as products_router
from api.routers.prompts import router as prompts_router
from api.routers.qc import router as qc_router
from api.routers.resources import router as resources_router
from api.routers.tasks import router as tasks_router
from api.routers.tts import router as tts_router
from api.routers.video import router as video_router
from api.routers.videos import router as videos_router

__all__ = [
    "health_router",
    "llm_router",
    "tts_router",
    "image_router",
    "content_router",
    "video_router",
    "videos_router",
    "tasks_router",
    "files_router",
    "resources_router",
    "frame_router",
    "media_jobs_router",
    "media_assets_router",
    "management_router",
    "audit_budget_router",
    "prompts_router",
    "products_router",
    "qc_router",
    "knowledge_router",
    "experiments_router",
]
