from __future__ import annotations

from types import SimpleNamespace

import pytest
from comfykit.comfyui.workflow_parser import WorkflowParser

from pixelle_video.config.loader import load_config_dict
from pixelle_video.config.schema import PixelleVideoConfig
from pixelle_video.services.tts_service import TTSService


class RecordingKit:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, workflow: str, params: dict):
        self.calls.append((workflow, params))
        return SimpleNamespace(
            status="completed",
            msg=None,
            audios=["https://gpu.example/output/pixelle_tts.flac"],
            files=[],
            outputs={},
        )


@pytest.mark.asyncio
async def test_default_tts_routes_to_4090_cosyvoice_workflow() -> None:
    config = PixelleVideoConfig(**load_config_dict("config.yaml")).to_dict()
    kit = RecordingKit()
    core = SimpleNamespace(_get_or_create_comfykit=lambda: _return(kit))
    service = TTSService(config, core=core)

    output = await service("GPU 配音链路测试")

    assert output == "https://gpu.example/output/pixelle_tts.flac"
    assert len(kit.calls) == 1
    workflow_path, params = kit.calls[0]
    assert workflow_path.replace("\\", "/").endswith(
        "/workflows/selfhost/tts_4090_cosyvoice_api.json"
    )
    assert params == {"text": "GPU 配音链路测试"}


def test_cosyvoice_workflow_is_parseable_by_installed_comfykit() -> None:
    metadata = WorkflowParser().parse_workflow_file(
        "workflows/selfhost/tts_4090_cosyvoice_api.json"
    )
    assert metadata is not None
    assert set(metadata.params) == {"text"}
    assert metadata.params["text"].required is True
    assert metadata.mapping_info.param_mappings[0].node_id == "2"
    assert metadata.mapping_info.param_mappings[0].input_field == "text"
    assert metadata.mapping_info.output_mappings[0].node_id == "14"


async def _return(value):
    return value


@pytest.mark.asyncio
async def test_core_cleanup_accepts_http_executor_without_close_hook() -> None:
    from pixelle_video.service import PixelleVideoCore

    core = PixelleVideoCore()
    core._comfykit = object()
    core._comfykit_config_hash = "loaded"

    await core.cleanup()

    assert core._comfykit is None
    assert core._comfykit_config_hash is None
