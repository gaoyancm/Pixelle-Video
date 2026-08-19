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
Pipeline UI Package

Exports registry functions and automatically registers available pipelines.
"""

# Import all pipeline UI modules to ensure they register themselves
from web.pipelines import (
    action_transfer as action_transfer,
)
from web.pipelines import (
    asset_based as asset_based,
)
from web.pipelines import (
    digital_human as digital_human,
)
from web.pipelines import (
    i2v as i2v,
)
from web.pipelines import (
    standard as standard,
)
from web.pipelines.base import (
    PipelineUI,
    get_all_pipeline_uis,
    get_pipeline_ui,
    register_pipeline_ui,
)

__all__ = [
    "PipelineUI",
    "register_pipeline_ui",
    "get_pipeline_ui",
    "get_all_pipeline_uis"
]
