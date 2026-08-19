"""Phase 10 task 3: startup preflight for private-GPU configuration.

Covers the G3 acceptance gate: empty nodes, disabled nodes, unknown workflow,
missing JSON, duplicate node id, workflow with no matching node, and a fully
valid configuration. Every failure must be reported without contacting a GPU.
"""

from __future__ import annotations

import httpx
import pytest

from pixelle_video.services.comfyui_workflows import WORKFLOW_SPECS
from pixelle_video.services.preflight import (
    default_workflow_root,
    validate_comfyui_configuration,
    validate_enabled_node_health,
)


def _node(
    node_id: str,
    workflow_types: list[str],
    *,
    enabled: bool = True,
) -> dict:
    return {
        "id": node_id,
        "name": node_id,
        "base_url": f"http://127.0.0.1/{node_id}",
        "workflow_types": workflow_types,
        "enabled": enabled,
        "timeout_seconds": 60,
        "concurrency": 1,
    }


def _all_workflows_nodes(enabled: bool = True) -> list[dict]:
    """One enabled/disabled node that serves every registered workflow."""

    return [
        _node("everything", sorted(WORKFLOW_SPECS), enabled=enabled),
    ]


def test_preflight_all_valid_configuration() -> None:
    report = validate_comfyui_configuration(
        _all_workflows_nodes(enabled=True),
        workflow_root=default_workflow_root(),
    )
    assert report.ok, str(report)
    assert report.errors == []


def test_preflight_empty_nodes_are_degraded() -> None:
    # No enabled node is a *capacity* gap (restricted mode), not a hard error.
    report = validate_comfyui_configuration(
        [],
        workflow_root=default_workflow_root(),
    )
    assert report.ok
    assert report.degraded
    assert report.restricted
    assert any("no matching node" in w for w in report.warnings)
    assert any("no ComfyUI node is enabled" in w for w in report.warnings)


def test_preflight_disabled_node_is_reported_per_workflow() -> None:
    report = validate_comfyui_configuration(
        _all_workflows_nodes(enabled=False),
        workflow_root=default_workflow_root(),
    )
    assert report.ok
    assert report.degraded
    assert report.restricted
    # Each workflow is declared only by a disabled node.
    assert any("no enabled node" in w and "disabled node" in w for w in report.warnings)
    assert any("no ComfyUI node is enabled" in w for w in report.warnings)


def test_preflight_unknown_workflow_is_rejected() -> None:
    nodes = _all_workflows_nodes(enabled=True)
    nodes[0]["workflow_types"] = [*sorted(WORKFLOW_SPECS), "bogus_workflow"]
    report = validate_comfyui_configuration(nodes, workflow_root=default_workflow_root())
    assert not report.ok
    assert any("unknown workflow type 'bogus_workflow'" in e for e in report.errors)


def test_preflight_missing_json_is_reported(tmp_path) -> None:
    # Place every workflow JSON except sdxl_img2img's.
    missing = "sdxl_img2img"
    for workflow_type, spec in WORKFLOW_SPECS.items():
        if workflow_type == missing:
            continue
        (tmp_path / spec.filename).write_text("{}", encoding="utf-8")

    report = validate_comfyui_configuration(
        _all_workflows_nodes(enabled=True),
        workflow_root=tmp_path,
    )
    assert not report.ok
    assert any(f"'{missing}' JSON missing" in e for e in report.errors)
    # No other workflow should be reported missing.
    assert not any("JSON missing" in e and missing not in e for e in report.errors)


def test_preflight_unparseable_json_is_reported(tmp_path) -> None:
    for spec in WORKFLOW_SPECS.values():
        (tmp_path / spec.filename).write_text("{ not json", encoding="utf-8")

    report = validate_comfyui_configuration(
        _all_workflows_nodes(enabled=True),
        workflow_root=tmp_path,
    )
    assert not report.ok
    assert any("JSON unparseable" in e for e in report.errors)


def test_preflight_duplicate_node_id_is_rejected() -> None:
    nodes = [
        _node("dup", ["sdxl_img2img"]),
        _node("dup", ["qwen_image_edit"]),
    ]
    report = validate_comfyui_configuration(nodes, workflow_root=default_workflow_root())
    assert not report.ok
    assert any("Duplicate ComfyUI node id 'dup'" in e for e in report.errors)


def test_preflight_workflow_without_matching_node_is_degraded() -> None:
    # Serve every workflow except one on a single enabled node.
    served = sorted(WORKFLOW_SPECS)[:-1]
    uncovered = sorted(WORKFLOW_SPECS)[-1]
    report = validate_comfyui_configuration(
        [_node("partial", served, enabled=True)],
        workflow_root=default_workflow_root(),
    )
    assert report.ok
    assert report.degraded
    assert not report.restricted
    assert any(f"'{uncovered}' has no matching node" in w for w in report.warnings)


def test_preflight_private_comfyui_disabled_allows_no_nodes() -> None:
    report = validate_comfyui_configuration(
        [],
        workflow_root=default_workflow_root(),
        private_comfyui_enabled=False,
    )
    # When the feature is disabled, the blanket "must have an enabled node"
    # warning must not fire, though per-workflow coverage gaps still surface.
    assert report.ok
    assert not any("no ComfyUI node is enabled" in w for w in report.warnings)
    assert not report.restricted


def test_enabled_node_health_uses_system_stats_and_requires_device() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"devices": [{"name": "RTX 4090"}]})

    errors = validate_enabled_node_health(
        [_node("gpu-4090", ["sdxl_img2img"])],
        transport=httpx.MockTransport(handler),
    )
    assert errors == []
    assert seen == ["http://127.0.0.1/gpu-4090/system_stats"]


def test_enabled_node_health_reports_safe_node_error() -> None:
    errors = validate_enabled_node_health(
        [_node("gpu-4090", ["sdxl_img2img"])],
        transport=httpx.MockTransport(lambda _request: httpx.Response(503)),
    )
    assert len(errors) == 1
    assert "gpu-4090" in errors[0]


@pytest.mark.parametrize("workflow_type", sorted(WORKFLOW_SPECS))
def test_every_spec_file_exists_in_bundled_root(workflow_type: str) -> None:
    spec = WORKFLOW_SPECS[workflow_type]
    assert spec.path(default_workflow_root()).is_file(), (
        f"bundled workflow JSON for '{workflow_type}' is missing"
    )


def test_config_example_covers_every_private_workflow() -> None:
    """config.example.yaml must declare a node for every registered spec."""

    from pathlib import Path

    import yaml

    from pixelle_video.config.schema import PixelleVideoConfig

    root = Path(__file__).resolve().parents[1]
    payload = yaml.safe_load((root / "config.example.yaml").read_text(encoding="utf-8"))
    config = PixelleVideoConfig(**payload)

    declared: set[str] = set()
    for node in config.comfyui.nodes:
        declared.update(node.workflow_types)

    assert declared == set(WORKFLOW_SPECS), (
        f"config.example.yaml nodes cover {sorted(declared)}, "
        f"but the registry has {sorted(WORKFLOW_SPECS)}"
    )
