"""Unified HTTP adapter for private ComfyUI GPU nodes."""

import asyncio
import copy
import json
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx

from pixelle_video.config.schema import ComfyUINodeConfig
from pixelle_video.services.comfyui_workflows import get_workflow_spec
from pixelle_video.utils.os_util import get_resource_path


class ComfyUIJobState(str, Enum):
    """Normalized job states exposed by the adapter."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class ComfyUIJob:
    """A submitted job and the node selected for it."""

    prompt_id: str
    node_id: str
    workflow_type: str


@dataclass(frozen=True)
class ComfyUIJobStatus:
    """Normalized status response."""

    state: ComfyUIJobState
    message: str | None = None


@dataclass(frozen=True)
class ComfyUIOutput:
    """One output file reported by ComfyUI history."""

    node_id: str
    filename: str
    subfolder: str
    storage_type: str
    media_type: str
    url: str


@dataclass(frozen=True)
class PreparedComfyUISubmission:
    """A validated workflow ready for the remote side-effecting POST /prompt."""

    node_id: str
    workflow_type: str
    workflow: dict[str, Any]


def select_comfyui_node(
    nodes: Sequence[ComfyUINodeConfig],
    workflow_type: str,
) -> ComfyUINodeConfig:
    """Select the first enabled configured node for a registered workflow."""

    get_workflow_spec(workflow_type)
    for node in nodes:
        if node.enabled and workflow_type in node.workflow_types:
            return node
    raise RuntimeError(
        f"No enabled ComfyUI node is configured for workflow type '{workflow_type}'"
    )


class ComfyUIAdapter:
    """Route workflows to configured nodes and speak the native ComfyUI API."""

    def __init__(
        self,
        nodes: Sequence[ComfyUINodeConfig | Mapping[str, Any]],
        *,
        workflow_root: str | Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        parsed_nodes = [
            node if isinstance(node, ComfyUINodeConfig) else ComfyUINodeConfig(**node)
            for node in nodes
        ]
        self._nodes = {node.id: node for node in parsed_nodes}
        if len(self._nodes) != len(parsed_nodes):
            raise ValueError("ComfyUI node ids must be unique")

        self._workflow_root = Path(workflow_root) if workflow_root is not None else None
        self._transport = transport
        self._semaphores = {
            node.id: asyncio.Semaphore(node.concurrency) for node in parsed_nodes
        }
        self._active_jobs: set[tuple[str, str]] = set()

    @property
    def nodes(self) -> tuple[ComfyUINodeConfig, ...]:
        """Configured nodes, including disabled ones."""

        return tuple(self._nodes.values())

    def select_node(self, workflow_type: str) -> ComfyUINodeConfig:
        """Select the first enabled node that declares the workflow type."""

        return select_comfyui_node(tuple(self._nodes.values()), workflow_type)

    def build_workflow(
        self,
        workflow_type: str,
        *,
        prompt: str,
        negative_prompt: str | None = None,
        input_image: str | None = None,
        width: int | None = None,
        height: int | None = None,
        frame_count: int | None = None,
        seed: int | None = None,
        steps: int | None = None,
        cfg: float | None = None,
        output_prefix: str | None = None,
    ) -> dict[str, Any]:
        """Load a pristine workflow and inject only its registered node inputs."""

        spec = get_workflow_spec(workflow_type)
        workflow_path = (
            spec.path(self._workflow_root)
            if self._workflow_root is not None
            else Path(get_resource_path("workflows", "selfhost", spec.filename))
        )
        with workflow_path.open("r", encoding="utf-8") as file:
            workflow = json.load(file)
        workflow = copy.deepcopy(workflow)

        values = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "input_image": input_image,
            "width": width,
            "height": height,
            "frame_count": frame_count,
            "seed": seed,
            "steps": steps,
            "cfg": cfg,
            "output_prefix": output_prefix,
        }
        if spec.requires_image and not input_image:
            raise ValueError(f"Workflow '{workflow_type}' requires an input image")

        for parameter, value in values.items():
            if value is None:
                continue
            target = spec.parameter_targets.get(parameter)
            if target is None:
                raise ValueError(
                    f"Parameter '{parameter}' is not supported by workflow '{workflow_type}'"
                )
            node_id, input_name = target
            try:
                workflow[node_id]["inputs"][input_name] = value
            except KeyError as error:
                raise ValueError(
                    f"Workflow '{workflow_type}' is missing registered input "
                    f"{node_id}.{input_name}"
                ) from error

        return workflow

    async def upload_image(self, node: ComfyUINodeConfig, image_path: str | Path) -> str:
        """Upload an image to a ComfyUI node and return its LoadImage value."""

        path = Path(image_path)
        if not path.is_file():
            raise FileNotFoundError(f"Input image not found: {path}")
        async with self._client(node) as client:
            with path.open("rb") as image_file:
                response = await client.post(
                    "/upload/image",
                    files={"image": (path.name, image_file, "application/octet-stream")},
                    data={"overwrite": "true"},
                )
        response.raise_for_status()
        payload = response.json()
        name = payload.get("name")
        if not name:
            raise RuntimeError("ComfyUI image upload response did not include a name")
        subfolder = payload.get("subfolder") or ""
        return f"{subfolder}/{name}" if subfolder else name

    async def submit(
        self,
        workflow_type: str,
        *,
        prompt: str,
        negative_prompt: str | None = None,
        image_path: str | Path | None = None,
        width: int | None = None,
        height: int | None = None,
        frame_count: int | None = None,
        seed: int | None = None,
        steps: int | None = None,
        cfg: float | None = None,
        output_prefix: str | None = None,
        submission_token: str | None = None,
    ) -> ComfyUIJob:
        """Inject and submit one workflow, respecting the node concurrency limit."""

        node = self.select_node(workflow_type)
        semaphore = self._semaphores[node.id]
        await semaphore.acquire()
        try:
            prepared = await self.prepare_submission(
                workflow_type,
                prompt=prompt,
                negative_prompt=negative_prompt,
                image_path=image_path,
                width=width,
                height=height,
                frame_count=frame_count,
                seed=seed,
                steps=steps,
                cfg=cfg,
                output_prefix=output_prefix,
            )
            job = await self.submit_prepared(
                prepared,
                submission_token=submission_token,
            )
            self._active_jobs.add((job.node_id, job.prompt_id))
            return job
        except BaseException:
            semaphore.release()
            raise

    async def prepare_submission(
        self,
        workflow_type: str,
        *,
        prompt: str,
        negative_prompt: str | None = None,
        image_path: str | Path | None = None,
        width: int | None = None,
        height: int | None = None,
        frame_count: int | None = None,
        seed: int | None = None,
        steps: int | None = None,
        cfg: float | None = None,
        output_prefix: str | None = None,
    ) -> PreparedComfyUISubmission:
        """Build and validate a request without sending POST /prompt."""

        node = self.select_node(workflow_type)
        spec = get_workflow_spec(workflow_type)
        input_image = (
            await self.upload_image(node, image_path)
            if spec.requires_image and image_path is not None
            else None
        )
        workflow = self.build_workflow(
            workflow_type,
            prompt=prompt,
            negative_prompt=negative_prompt,
            input_image=input_image,
            width=width,
            height=height,
            frame_count=frame_count,
            seed=seed,
            steps=steps,
            cfg=cfg,
            output_prefix=output_prefix,
        )
        return PreparedComfyUISubmission(
            node_id=node.id,
            workflow_type=workflow_type,
            workflow=workflow,
        )

    async def submit_prepared(
        self,
        prepared: PreparedComfyUISubmission,
        *,
        submission_token: str | None = None,
    ) -> ComfyUIJob:
        """Perform the single remote side-effecting submission request."""

        node = self._nodes[prepared.node_id]
        client_id = submission_token or uuid.uuid4().hex
        payload: dict[str, Any] = {
            "prompt": prepared.workflow,
            "client_id": client_id,
        }
        if submission_token is not None:
            payload["extra_data"] = {"pixelle_submission_token": submission_token}
        async with self._client(node) as client:
            response = await client.post("/prompt", json=payload)
        response.raise_for_status()
        prompt_id = response.json().get("prompt_id")
        if not prompt_id:
            raise RuntimeError("ComfyUI submit response did not include prompt_id")
        return ComfyUIJob(
            prompt_id=str(prompt_id),
            node_id=node.id,
            workflow_type=prepared.workflow_type,
        )

    async def query_status(self, job: ComfyUIJob) -> ComfyUIJobStatus:
        """Query history and queue data and return a normalized status."""

        node = self._get_job_node(job)
        history = await self._get_history(node, job.prompt_id)
        record = history.get(job.prompt_id)
        if record:
            status = record.get("status") or {}
            status_text = str(status.get("status_str") or "").lower()
            completed = bool(status.get("completed"))
            if completed or (record.get("outputs") and status_text not in {"error", "failed"}):
                result = ComfyUIJobStatus(ComfyUIJobState.COMPLETED)
                self._release_job(job)
                return result
            if status_text in {"error", "failed"}:
                messages = status.get("messages") or []
                message = str(messages[-1]) if messages else "ComfyUI execution failed"
                result = ComfyUIJobStatus(ComfyUIJobState.FAILED, message)
                self._release_job(job)
                return result
            return ComfyUIJobStatus(ComfyUIJobState.RUNNING)

        async with self._client(node) as client:
            response = await client.get("/queue")
        response.raise_for_status()
        queue = response.json()
        if self._queue_contains(
            queue.get("queue_running"), job.prompt_id
        ) or self._queue_contains(queue.get("queue_pending"), job.prompt_id):
            return ComfyUIJobStatus(ComfyUIJobState.RUNNING)
        return ComfyUIJobStatus(ComfyUIJobState.QUEUED)

    async def get_outputs(self, job: ComfyUIJob) -> list[ComfyUIOutput]:
        """Return output metadata and downloadable /view URLs for a completed job."""

        node = self._get_job_node(job)
        history = await self._get_history(node, job.prompt_id)
        record = history.get(job.prompt_id)
        if not record:
            raise RuntimeError(f"ComfyUI job '{job.prompt_id}' has no history record yet")

        outputs: list[ComfyUIOutput] = []
        for node_output in (record.get("outputs") or {}).values():
            if not isinstance(node_output, dict):
                continue
            for media_type, items in node_output.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict) or not item.get("filename"):
                        continue
                    filename = str(item["filename"])
                    subfolder = str(item.get("subfolder") or "")
                    storage_type = (
                        "output" if "type" not in item else str(item.get("type") or "")
                    )
                    url = str(
                        httpx.URL(f"{node.base_url.rstrip('/')}/view").copy_merge_params(
                            {
                                "filename": filename,
                                "subfolder": subfolder,
                                "type": storage_type,
                            }
                        )
                    )
                    outputs.append(
                        ComfyUIOutput(
                            node_id=node.id,
                            filename=filename,
                            subfolder=subfolder,
                            storage_type=storage_type,
                            media_type=str(media_type),
                            url=url,
                        )
                    )
        self._release_job(job)
        return outputs

    async def download_output(self, output: ComfyUIOutput) -> bytes:
        """Download one reported output without inheriting environment proxies."""

        try:
            node = self._nodes[output.node_id]
        except KeyError as error:
            raise ValueError(f"Unknown ComfyUI node id '{output.node_id}'") from error
        async with self._client(node) as client:
            response = await client.get(
                "/view",
                params={
                    "filename": output.filename,
                    "subfolder": output.subfolder,
                    "type": output.storage_type,
                },
            )
        response.raise_for_status()
        return response.content

    async def find_prompt_ids_by_submission_token(
        self,
        node_id: str,
        submission_token: str,
    ) -> tuple[str, ...]:
        """Return only prompt IDs with an explicit echoed token in queue/history."""

        try:
            node = self._nodes[node_id]
        except KeyError as error:
            raise ValueError(f"Unknown ComfyUI node id '{node_id}'") from error
        async with self._client(node) as client:
            queue_response = await client.get("/queue")
            queue_response.raise_for_status()
            history_response = await client.get("/history")
            history_response.raise_for_status()
        matches: set[str] = set()
        queue = queue_response.json()
        if isinstance(queue, dict):
            for key in ("queue_running", "queue_pending"):
                for item in queue.get(key) or []:
                    prompt_id, extra_data = self._queue_item_identity(item)
                    if prompt_id and self._token_matches(extra_data, submission_token):
                        matches.add(prompt_id)
        history = history_response.json()
        if isinstance(history, dict):
            for prompt_id, record in history.items():
                if not isinstance(record, dict):
                    continue
                prompt_data = record.get("prompt")
                extra_data = (
                    prompt_data[3]
                    if isinstance(prompt_data, list) and len(prompt_data) > 3
                    else record.get("extra_data")
                )
                if self._token_matches(extra_data, submission_token):
                    matches.add(str(prompt_id))
        return tuple(sorted(matches))

    async def execute(
        self,
        workflow_type: str,
        *,
        poll_interval: float = 2.0,
        **parameters: Any,
    ) -> list[ComfyUIOutput]:
        """Submit, poll until terminal, and return outputs."""

        job = await self.submit(workflow_type, **parameters)
        try:
            node = self._get_job_node(job)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + node.timeout_seconds
            while loop.time() < deadline:
                status = await self.query_status(job)
                if status.state is ComfyUIJobState.COMPLETED:
                    return await self.get_outputs(job)
                if status.state is ComfyUIJobState.FAILED:
                    raise RuntimeError(status.message or "ComfyUI execution failed")
                await asyncio.sleep(poll_interval)
            raise TimeoutError(
                f"ComfyUI job '{job.prompt_id}' exceeded {node.timeout_seconds} seconds"
            )
        finally:
            self._release_job(job)

    def _client(self, node: ComfyUINodeConfig) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=node.base_url.rstrip("/"),
            timeout=node.timeout_seconds,
            transport=self._transport,
            trust_env=False,
        )

    def _get_job_node(self, job: ComfyUIJob) -> ComfyUINodeConfig:
        try:
            return self._nodes[job.node_id]
        except KeyError as error:
            raise ValueError(f"Unknown ComfyUI node id '{job.node_id}'") from error

    async def _get_history(
        self, node: ComfyUINodeConfig, prompt_id: str
    ) -> dict[str, Any]:
        async with self._client(node) as client:
            response = await client.get(f"/history/{prompt_id}")
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _queue_contains(queue_items: Any, prompt_id: str) -> bool:
        if not isinstance(queue_items, list):
            return False
        return any(
            isinstance(item, list) and len(item) > 1 and str(item[1]) == prompt_id
            for item in queue_items
        )

    @staticmethod
    def _queue_item_identity(item: Any) -> tuple[str | None, Any]:
        if not isinstance(item, list) or len(item) < 2:
            return None, None
        return str(item[1]), item[3] if len(item) > 3 else None

    @staticmethod
    def _token_matches(extra_data: Any, submission_token: str) -> bool:
        return (
            isinstance(extra_data, dict)
            and extra_data.get("pixelle_submission_token") == submission_token
        )

    def _release_job(self, job: ComfyUIJob) -> None:
        key = (job.node_id, job.prompt_id)
        if key in self._active_jobs:
            self._active_jobs.remove(key)
            self._semaphores[job.node_id].release()
