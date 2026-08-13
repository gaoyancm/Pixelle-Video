"""Registry for the private-GPU ComfyUI workflows added in phase 1."""

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class ComfyUIWorkflowSpec:
    """Static metadata needed to route and inject one ComfyUI API workflow."""

    workflow_type: str
    filename: str
    requires_image: bool
    parameter_targets: Mapping[str, tuple[str, str]]

    @property
    def workflow_key(self) -> str:
        return f"selfhost/{self.filename}"

    def path(self, workflow_root: Path) -> Path:
        return workflow_root / self.filename


WORKFLOW_SPECS: dict[str, ComfyUIWorkflowSpec] = {
    "a800_wan22_t2v_33f": ComfyUIWorkflowSpec(
        workflow_type="a800_wan22_t2v_33f",
        filename="video_a800_wan22_t2v_4step_33f_api.json",
        requires_image=False,
        parameter_targets={
            "prompt": ("89", "text"),
            "negative_prompt": ("72", "text"),
            "width": ("74", "width"),
            "height": ("74", "height"),
            "frame_count": ("74", "length"),
            "seed": ("81", "noise_seed"),
            "output_prefix": ("80", "filename_prefix"),
        },
    ),
    "a800_wan22_t2v_81f": ComfyUIWorkflowSpec(
        workflow_type="a800_wan22_t2v_81f",
        filename="video_a800_wan22_t2v_4step_81f_api.json",
        requires_image=False,
        parameter_targets={
            "prompt": ("89", "text"),
            "negative_prompt": ("72", "text"),
            "width": ("74", "width"),
            "height": ("74", "height"),
            "frame_count": ("74", "length"),
            "seed": ("81", "noise_seed"),
            "output_prefix": ("80", "filename_prefix"),
        },
    ),
    "gpu_4090_wan21_i2v_33f": ComfyUIWorkflowSpec(
        workflow_type="gpu_4090_wan21_i2v_33f",
        filename="video_4090_wan21_i2v_fp8_512x512_33f_api.json",
        requires_image=True,
        parameter_targets={
            "input_image": ("52", "image"),
            "prompt": ("6", "text"),
            "negative_prompt": ("7", "text"),
            "width": ("50", "width"),
            "height": ("50", "height"),
            "frame_count": ("50", "length"),
            "seed": ("3", "seed"),
            "steps": ("3", "steps"),
            "cfg": ("3", "cfg"),
            "output_prefix": ("47", "filename_prefix"),
        },
    ),
    "gpu_4090_wan21_i2v_81f": ComfyUIWorkflowSpec(
        workflow_type="gpu_4090_wan21_i2v_81f",
        filename="video_4090_wan21_i2v_fp8_512x512_81f_api.json",
        requires_image=True,
        parameter_targets={
            "input_image": ("52", "image"),
            "prompt": ("6", "text"),
            "negative_prompt": ("7", "text"),
            "width": ("50", "width"),
            "height": ("50", "height"),
            "frame_count": ("50", "length"),
            "seed": ("3", "seed"),
            "steps": ("3", "steps"),
            "cfg": ("3", "cfg"),
            "output_prefix": ("47", "filename_prefix"),
        },
    ),
    "sdxl_img2img": ComfyUIWorkflowSpec(
        workflow_type="sdxl_img2img",
        filename="image_sdxl_img2img_api.json",
        requires_image=True,
        parameter_targets={
            "input_image": ("2", "image"),
            "prompt": ("3", "text"),
            "negative_prompt": ("4", "text"),
            "seed": ("6", "seed"),
            "steps": ("6", "steps"),
            "cfg": ("6", "cfg"),
            "output_prefix": ("8", "filename_prefix"),
        },
    ),
    "qwen_image_edit": ComfyUIWorkflowSpec(
        workflow_type="qwen_image_edit",
        filename="image_qwen_edit_api.json",
        requires_image=True,
        parameter_targets={
            "input_image": ("92", "image"),
            "prompt": ("6", "text"),
            "negative_prompt": ("7", "text"),
            "seed": ("3", "seed"),
            "steps": ("3", "steps"),
            "cfg": ("3", "cfg"),
            "output_prefix": ("60", "filename_prefix"),
        },
    ),
}

_WORKFLOW_KEYS = {spec.workflow_key: spec for spec in WORKFLOW_SPECS.values()}


def get_workflow_spec(workflow_type_or_key: str) -> ComfyUIWorkflowSpec:
    """Resolve either a workflow type or a Pixelle workflow key."""

    spec = WORKFLOW_SPECS.get(workflow_type_or_key) or _WORKFLOW_KEYS.get(workflow_type_or_key)
    if spec is None:
        available = ", ".join(sorted(WORKFLOW_SPECS))
        raise ValueError(
            f"Unsupported private ComfyUI workflow '{workflow_type_or_key}'. "
            f"Available workflow types: {available}"
        )
    return spec


def is_private_gpu_workflow(workflow_key: str | None) -> bool:
    """Return whether a Pixelle workflow key belongs to the phase-1 registry."""

    return bool(workflow_key and workflow_key in _WORKFLOW_KEYS)
