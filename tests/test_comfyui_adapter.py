import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from pixelle_video.config.schema import ComfyUINodeConfig, PixelleVideoConfig
from pixelle_video.models.media import MediaResult
from pixelle_video.services.comfyui_adapter import (
    ComfyUIAdapter,
    ComfyUIJobState,
    ComfyUIOutput,
)
from pixelle_video.services.comfyui_workflows import WORKFLOW_SPECS
from pixelle_video.services.media import MediaService

WORKFLOW_ROOT = Path(__file__).parents[1] / "workflows" / "selfhost"


class RejectingAdapter:
    async def execute(self, workflow_type: str, **parameters):
        raise AssertionError(f"private adapter intercepted {workflow_type}")


class RecordingComfyKit:
    def __init__(self):
        self.calls = []

    async def execute(self, workflow, parameters):
        self.calls.append((workflow, parameters))
        return SimpleNamespace(
            status="completed",
            msg=None,
            images=[],
            videos=["http://legacy-comfy/video.mp4"],
            duration=1.0,
        )


class LegacyCore:
    def __init__(self):
        self.comfyui_adapter = RejectingAdapter()
        self.kit = RecordingComfyKit()

    async def _get_or_create_comfykit(self):
        return self.kit


def make_a800_adapter(handler, *, timeout_seconds: float = 1.0) -> ComfyUIAdapter:
    return ComfyUIAdapter(
        [
            {
                "id": "a800",
                "name": "A800",
                "base_url": "http://mock-comfyui",
                "workflow_types": ["a800_wan22_t2v_33f"],
                "enabled": True,
                "timeout_seconds": timeout_seconds,
                "concurrency": 1,
            }
        ],
        workflow_root=WORKFLOW_ROOT,
        transport=httpx.MockTransport(handler),
    )


async def assert_second_job_can_acquire(adapter: ComfyUIAdapter) -> None:
    second_job = await asyncio.wait_for(
        adapter.submit("a800_wan22_t2v_33f", prompt="second task"),
        timeout=0.5,
    )
    second_status = await adapter.query_status(second_job)
    assert second_status.state is ComfyUIJobState.COMPLETED


def test_workflow_copies_preserve_verified_json() -> None:
    expected_hashes = {
        "video_a800_wan22_t2v_4step_33f_api.json": (
            "b4bcc081c9256461f076e16a135a98091ef185e5dac97171c47e5aac8fc450d5"
        ),
        "video_a800_wan22_t2v_4step_81f_api.json": (
            "8822b0a2d86fcb57f7c0d6b1232e0e4c35353bead1096fb38bacb557b9501615"
        ),
        "video_4090_wan21_i2v_fp8_512x512_33f_api.json": (
            "068efa3b2f9cfe387b609f9404d7ea05768287bba479f667da2bce2ff3a90885"
        ),
    }
    for filename, expected_hash in expected_hashes.items():
        # apply_patch stores text files with a final LF; the verified source files did not.
        content = (WORKFLOW_ROOT / filename).read_bytes().removesuffix(b"\n")
        assert hashlib.sha256(content).hexdigest() == expected_hash


def test_a800_parameter_injection_does_not_modify_workflow_file() -> None:
    workflow_path = WORKFLOW_ROOT / WORKFLOW_SPECS["a800_wan22_t2v_33f"].filename
    original = workflow_path.read_bytes()
    adapter = ComfyUIAdapter([], workflow_root=WORKFLOW_ROOT)

    workflow = adapter.build_workflow(
        "a800_wan22_t2v_33f",
        prompt="phase one prompt",
        negative_prompt="phase one negative",
        width=768,
        height=432,
        frame_count=33,
        seed=1234,
        output_prefix="video/phase1",
    )

    assert workflow["89"]["inputs"]["text"] == "phase one prompt"
    assert workflow["72"]["inputs"]["text"] == "phase one negative"
    assert {
        key: workflow["74"]["inputs"][key] for key in ("width", "height", "length")
    } == {
        "width": 768,
        "height": 432,
        "length": 33,
    }
    assert workflow["81"]["inputs"]["noise_seed"] == 1234
    assert workflow["80"]["inputs"]["filename_prefix"] == "video/phase1"
    assert workflow_path.read_bytes() == original


def test_4090_81_frame_workflow_has_independent_registered_baseline() -> None:
    adapter = ComfyUIAdapter([], workflow_root=WORKFLOW_ROOT)
    workflow = adapter.build_workflow(
        "gpu_4090_wan21_i2v_81f",
        prompt="81 frame validation",
        input_image="input.jpg",
    )
    assert workflow["50"]["inputs"]["length"] == 81
    assert workflow["6"]["inputs"]["text"] == "81 frame validation"


def test_private_node_config_is_backward_compatible() -> None:
    assert PixelleVideoConfig().comfyui.nodes == []
    node = ComfyUINodeConfig(
        id="a800",
        name="A800",
        base_url="http://127.0.0.1:18188/",
        workflow_types=["a800_wan22_t2v_33f"],
        concurrency=2,
    )
    assert node.base_url == "http://127.0.0.1:18188"
    assert node.enabled is False
    assert node.concurrency == 2


@pytest.mark.asyncio
async def test_private_node_client_does_not_inherit_environment_proxy() -> None:
    node = ComfyUINodeConfig(
        id="gpu-4090",
        name="RTX 4090",
        base_url="http://127.0.0.1:28188",
        workflow_types=["gpu_4090_wan21_i2v_33f"],
    )
    adapter = ComfyUIAdapter([node], workflow_root=WORKFLOW_ROOT)
    client = adapter._client(node)
    try:
        assert client._trust_env is False
    finally:
        await client.aclose()


def test_private_workflows_are_registered_by_media_scanner() -> None:
    service = MediaService(
        {"comfyui": {"image": {"default_workflow": next(iter(WORKFLOW_SPECS.values())).workflow_key}}}
    )
    available = set(service.available)
    assert {spec.workflow_key for spec in WORKFLOW_SPECS.values()} <= available


@pytest.mark.parametrize(
    "workflow_type",
    [
        "a800_wan22_t2v_33f",
        "a800_wan22_t2v_81f",
        "gpu_4090_wan21_i2v_33f",
        "gpu_4090_wan21_i2v_81f",
    ],
)
@pytest.mark.asyncio
async def test_media_service_routes_registered_workflow_to_adapter(
    workflow_type: str,
) -> None:
    adapter_calls = []
    api_provider_calls = []

    class FakeAdapter:
        async def execute(self, routed_workflow_type: str, **parameters):
            adapter_calls.append((routed_workflow_type, parameters))
            return [
                ComfyUIOutput(
                    node_id="private-gpu",
                    filename=f"{routed_workflow_type}.mp4",
                    subfolder="video",
                    storage_type="output",
                    media_type="videos",
                    url=f"http://mock-comfyui/view?filename={routed_workflow_type}.mp4",
                )
            ]

    class RejectingAPIProvider:
        async def __call__(self, **parameters):
            api_provider_calls.append(parameters)
            raise AssertionError("API provider must not handle private workflows")

    class FakeCore:
        def __init__(self):
            self.comfyui_adapter = FakeAdapter()
            self.api_media = RejectingAPIProvider()
            self.comfykit_calls = 0

        async def _get_or_create_comfykit(self):
            self.comfykit_calls += 1
            raise AssertionError("ComfyKit must not handle private workflows")

    core = FakeCore()
    spec = WORKFLOW_SPECS[workflow_type]
    service = MediaService(
        {"comfyui": {"image": {"default_workflow": spec.workflow_key}}},
        core=core,
    )

    result = await service(
        prompt="phase one",
        workflow=spec.workflow_key,
        media_type="video",
        image_path="test-input.jpg" if spec.requires_image else None,
        frame_count=81 if workflow_type.endswith("81f") else 33,
        seed=7,
    )

    assert result.is_video
    assert result.url.endswith(f"{workflow_type}.mp4")
    assert len(adapter_calls) == 1
    assert adapter_calls[0][0] == workflow_type
    assert adapter_calls[0][1]["frame_count"] == (
        81 if workflow_type.endswith("81f") else 33
    )
    assert adapter_calls[0][1]["seed"] == 7
    assert core.comfykit_calls == 0
    assert api_provider_calls == []


@pytest.mark.asyncio
async def test_non_private_selfhost_workflow_uses_original_comfykit_path() -> None:
    core = LegacyCore()
    service = MediaService({"comfyui": {"image": {}}}, core=core)

    result = await service(
        prompt="legacy selfhost",
        workflow="selfhost/video_wan2.1_fusionx.json",
        media_type="video",
    )

    assert result.url == "http://legacy-comfy/video.mp4"
    assert len(core.kit.calls) == 1
    assert core.kit.calls[0][0].endswith("video_wan2.1_fusionx.json")


@pytest.mark.asyncio
async def test_runninghub_workflow_uses_original_comfykit_path() -> None:
    core = LegacyCore()
    service = MediaService({"comfyui": {"image": {}}}, core=core)

    result = await service(
        prompt="legacy runninghub",
        workflow="runninghub/video_wan2.1_fusionx.json",
        media_type="video",
    )

    assert result.url == "http://legacy-comfy/video.mp4"
    assert core.kit.calls == [
        ("1985909483975188481", {"prompt": "legacy runninghub"})
    ]


@pytest.mark.asyncio
async def test_api_provider_workflow_bypasses_private_adapter_and_comfykit() -> None:
    calls = []

    class FakeAPIProvider:
        async def __call__(self, **parameters):
            calls.append(parameters)
            return MediaResult(media_type="video", url="http://provider/video.mp4")

    class APIProviderCore:
        comfyui_adapter = RejectingAdapter()
        api_media = FakeAPIProvider()

        async def _get_or_create_comfykit(self):
            raise AssertionError("ComfyKit must not handle API provider workflows")

    service = MediaService({"comfyui": {"image": {}}}, core=APIProviderCore())

    result = await service(
        prompt="provider",
        workflow="api/dashscope/test-model",
        media_type="video",
    )

    assert result.url == "http://provider/video.mp4"
    assert calls[0]["workflow"] == "api/dashscope/test-model"


@pytest.mark.asyncio
async def test_4090_submit_status_and_output_with_mock(tmp_path: Path) -> None:
    submitted_workflow = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/image":
            return httpx.Response(
                200, json={"name": "input.jpg", "subfolder": "phase1", "type": "input"}
            )
        if request.url.path == "/prompt":
            payload = json.loads(request.content)
            submitted_workflow.update(payload["prompt"])
            return httpx.Response(200, json={"prompt_id": "mock-prompt-1", "number": 1})
        if request.url.path == "/history/mock-prompt-1":
            return httpx.Response(
                200,
                json={
                    "mock-prompt-1": {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {
                            "47": {
                                "videos": [
                                    {
                                        "filename": "phase1_00001.webm",
                                        "subfolder": "video",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                },
            )
        if request.url.path == "/queue":
            return httpx.Response(200, json={"queue_running": [], "queue_pending": []})
        return httpx.Response(404)

    image_path = tmp_path / "input.jpg"
    image_path.write_bytes(b"mock-image")
    adapter = ComfyUIAdapter(
        [
            {
                "id": "gpu-4090",
                "name": "RTX 4090",
                "base_url": "http://mock-comfyui",
                "workflow_types": ["gpu_4090_wan21_i2v_33f"],
                "enabled": True,
                "timeout_seconds": 10,
                "concurrency": 1,
            }
        ],
        workflow_root=WORKFLOW_ROOT,
        transport=httpx.MockTransport(handler),
    )

    job = await adapter.submit(
        "gpu_4090_wan21_i2v_33f",
        prompt="a moving cat",
        negative_prompt="static",
        image_path=image_path,
        width=512,
        height=512,
        frame_count=33,
        seed=42,
        steps=20,
        cfg=6,
        output_prefix="phase1",
    )
    status = await adapter.query_status(job)
    outputs = await adapter.get_outputs(job)

    assert submitted_workflow["52"]["inputs"]["image"] == "phase1/input.jpg"
    assert submitted_workflow["6"]["inputs"]["text"] == "a moving cat"
    assert submitted_workflow["7"]["inputs"]["text"] == "static"
    assert submitted_workflow["50"]["inputs"]["length"] == 33
    assert {
        key: submitted_workflow["3"]["inputs"][key] for key in ("seed", "steps", "cfg")
    } == {"seed": 42, "steps": 20, "cfg": 6}
    assert submitted_workflow["47"]["inputs"]["filename_prefix"] == "phase1"
    assert status.state is ComfyUIJobState.COMPLETED
    assert len(outputs) == 1
    assert outputs[0].filename == "phase1_00001.webm"
    assert outputs[0].url.startswith("http://mock-comfyui/view?")


@pytest.mark.asyncio
async def test_a800_does_not_upload_an_unneeded_image(tmp_path: Path) -> None:
    requested_paths = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "a800-prompt", "number": 1})
        return httpx.Response(404)

    image_path = tmp_path / "unused.jpg"
    image_path.write_bytes(b"not-needed-for-t2v")
    adapter = ComfyUIAdapter(
        [
            {
                "id": "a800",
                "name": "A800",
                "base_url": "http://mock-comfyui",
                "workflow_types": ["a800_wan22_t2v_33f"],
                "enabled": True,
            }
        ],
        workflow_root=WORKFLOW_ROOT,
        transport=httpx.MockTransport(handler),
    )

    job = await adapter.submit(
        "a800_wan22_t2v_33f",
        prompt="text-to-video",
        image_path=image_path,
    )

    assert job.prompt_id == "a800-prompt"
    assert requested_paths == ["/prompt"]


def test_disabled_node_is_not_selected() -> None:
    adapter = ComfyUIAdapter(
        [
            {
                "id": "a800",
                "name": "A800",
                "base_url": "http://mock-comfyui",
                "workflow_types": ["a800_wan22_t2v_33f"],
                "enabled": False,
            }
        ],
        workflow_root=WORKFLOW_ROOT,
    )

    with pytest.raises(RuntimeError, match="No enabled ComfyUI node"):
        adapter.select_node("a800_wan22_t2v_33f")


def test_no_matching_enabled_node_is_rejected() -> None:
    adapter = ComfyUIAdapter(
        [
            {
                "id": "a800",
                "name": "A800",
                "base_url": "http://mock-comfyui",
                "workflow_types": ["a800_wan22_t2v_33f"],
                "enabled": True,
            }
        ],
        workflow_root=WORKFLOW_ROOT,
    )

    with pytest.raises(RuntimeError, match="No enabled ComfyUI node"):
        adapter.select_node("gpu_4090_wan21_i2v_33f")


@pytest.mark.asyncio
async def test_comfyui_failed_status_releases_concurrency_for_next_job() -> None:
    prompt_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal prompt_count
        if request.url.path == "/prompt":
            prompt_count += 1
            return httpx.Response(200, json={"prompt_id": f"job-{prompt_count}"})
        if request.url.path == "/history/job-1":
            return httpx.Response(
                200,
                json={
                    "job-1": {
                        "status": {
                            "status_str": "error",
                            "completed": False,
                            "messages": ["mock failure"],
                        },
                        "outputs": {},
                    }
                },
            )
        if request.url.path == "/history/job-2":
            return httpx.Response(
                200,
                json={
                    "job-2": {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {"80": {"images": [{"filename": "second.mp4"}]}},
                    }
                },
            )
        return httpx.Response(404)

    adapter = make_a800_adapter(handler)

    with pytest.raises(RuntimeError, match="mock failure"):
        await adapter.execute("a800_wan22_t2v_33f", prompt="first task")

    await assert_second_job_can_acquire(adapter)


@pytest.mark.asyncio
async def test_private_workflow_completed_without_outputs_is_rejected() -> None:
    class EmptyOutputAdapter:
        async def execute(self, workflow_type: str, **parameters):
            return []

    core = type("FakeCore", (), {"comfyui_adapter": EmptyOutputAdapter()})()
    workflow_key = WORKFLOW_SPECS["a800_wan22_t2v_33f"].workflow_key
    service = MediaService({"comfyui": {"image": {}}}, core=core)

    with pytest.raises(RuntimeError, match="without output files"):
        await service(
            prompt="empty output",
            workflow=workflow_key,
            media_type="video",
        )


@pytest.mark.parametrize("extension", ["mp4", "webm"])
@pytest.mark.asyncio
async def test_video_in_images_field_is_selected_by_extension(extension: str) -> None:
    class ImagesFieldAdapter:
        async def execute(self, workflow_type: str, **parameters):
            return [
                ComfyUIOutput(
                    node_id="a800",
                    filename="preview.png",
                    subfolder="",
                    storage_type="output",
                    media_type="images",
                    url="http://mock-comfyui/view?filename=preview.png",
                ),
                ComfyUIOutput(
                    node_id="a800",
                    filename=f"result.{extension}",
                    subfolder="video",
                    storage_type="output",
                    media_type="images",
                    url=f"http://mock-comfyui/view?filename=result.{extension}",
                ),
            ]

    core = type("FakeCore", (), {"comfyui_adapter": ImagesFieldAdapter()})()
    workflow_key = WORKFLOW_SPECS["a800_wan22_t2v_33f"].workflow_key
    service = MediaService({"comfyui": {"image": {}}}, core=core)

    result = await service(
        prompt="images field video",
        workflow=workflow_key,
        media_type="video",
    )

    assert result.url.endswith(f"result.{extension}")


@pytest.mark.asyncio
async def test_normal_completion_releases_concurrency_for_next_job() -> None:
    prompt_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal prompt_count
        if request.url.path == "/prompt":
            prompt_count += 1
            return httpx.Response(200, json={"prompt_id": f"job-{prompt_count}"})
        if request.url.path in {"/history/job-1", "/history/job-2"}:
            prompt_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    prompt_id: {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {
                            "80": {"images": [{"filename": f"{prompt_id}.mp4"}]}
                        },
                    }
                },
            )
        return httpx.Response(404)

    adapter = make_a800_adapter(handler)

    outputs = await adapter.execute("a800_wan22_t2v_33f", prompt="first task")
    assert outputs[0].filename == "job-1.mp4"
    await assert_second_job_can_acquire(adapter)


@pytest.mark.asyncio
async def test_timeout_releases_concurrency_for_next_job() -> None:
    prompt_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal prompt_count
        if request.url.path == "/prompt":
            prompt_count += 1
            return httpx.Response(200, json={"prompt_id": f"job-{prompt_count}"})
        if request.url.path == "/history/job-1":
            return httpx.Response(200, json={})
        if request.url.path == "/queue":
            return httpx.Response(
                200,
                json={"queue_running": [[1, "job-1"]], "queue_pending": []},
            )
        if request.url.path == "/history/job-2":
            return httpx.Response(
                200,
                json={
                    "job-2": {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {"80": {"images": [{"filename": "second.mp4"}]}},
                    }
                },
            )
        return httpx.Response(404)

    adapter = make_a800_adapter(handler, timeout_seconds=0.02)

    with pytest.raises(TimeoutError):
        await adapter.execute(
            "a800_wan22_t2v_33f",
            prompt="first task",
            poll_interval=0.001,
        )

    await assert_second_job_can_acquire(adapter)


@pytest.mark.asyncio
async def test_cancellation_releases_concurrency_for_next_job() -> None:
    prompt_count = 0
    first_history_requested = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal prompt_count
        if request.url.path == "/prompt":
            prompt_count += 1
            return httpx.Response(200, json={"prompt_id": f"job-{prompt_count}"})
        if request.url.path == "/history/job-1":
            first_history_requested.set()
            return httpx.Response(200, json={})
        if request.url.path == "/queue":
            return httpx.Response(
                200,
                json={"queue_running": [[1, "job-1"]], "queue_pending": []},
            )
        if request.url.path == "/history/job-2":
            return httpx.Response(
                200,
                json={
                    "job-2": {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {"80": {"images": [{"filename": "second.mp4"}]}},
                    }
                },
            )
        return httpx.Response(404)

    adapter = make_a800_adapter(handler)
    first_task = asyncio.create_task(
        adapter.execute(
            "a800_wan22_t2v_33f",
            prompt="first task",
            poll_interval=10,
        )
    )
    await asyncio.wait_for(first_history_requested.wait(), timeout=0.5)
    first_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await first_task

    await assert_second_job_can_acquire(adapter)


@pytest.mark.asyncio
async def test_execute_releases_concurrency_after_status_error() -> None:
    prompt_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal prompt_count
        if request.url.path == "/prompt":
            prompt_count += 1
            return httpx.Response(200, json={"prompt_id": f"job-{prompt_count}"})
        if request.url.path == "/history/job-1":
            return httpx.Response(503, json={"error": "temporarily unavailable"})
        if request.url.path == "/history/job-2":
            return httpx.Response(
                200,
                json={
                    "job-2": {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {"80": {"images": [{"filename": "second.mp4"}]}},
                    }
                },
            )
        return httpx.Response(404)

    adapter = make_a800_adapter(handler)

    with pytest.raises(httpx.HTTPStatusError):
        await adapter.execute("a800_wan22_t2v_33f", prompt="test")

    await assert_second_job_can_acquire(adapter)
