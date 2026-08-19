"""Startup preflight validation for the private-GPU ComfyUI configuration.

This module answers one question before any media task is created: is the
configured private-GPU topology self-consistent?

The check is deliberately static and never contacts a real GPU. It only reads
the node configuration and the workflow JSON files that ``WORKFLOW_SPECS``
points at, so it is safe to run in tests, in the launcher, and in CI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx

from pixelle_video.config.schema import ComfyUINodeConfig
from pixelle_video.services.comfyui_workflows import WORKFLOW_SPECS


@dataclass
class PreflightReport:
    """Result of validating the private-GPU configuration before startup."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    restricted_mode: bool = False

    @property
    def ok(self) -> bool:
        """True when no blocking error was found."""

        return not self.errors

    @property
    def degraded(self) -> bool:
        """True when configuration is consistent but has capability gaps.

        A degraded report is safe to boot in a clearly-marked restricted mode
        (management/planning only); private media tasks must stay forbidden.
        """

        return not self.errors and bool(self.warnings)

    @property
    def restricted(self) -> bool:
        """True only when no private-media work may be accepted at all."""

        return not self.errors and self.restricted_mode

    def __str__(self) -> str:
        lines = []
        for error in self.errors:
            lines.append(f"[ERROR] {error}")
        for warning in self.warnings:
            lines.append(f"[WARN ] {warning}")
        if not lines:
            lines.append("[OK] private-GPU configuration is consistent")
        return "\n".join(lines)


def default_workflow_root() -> Path:
    """Return the repository's bundled selfhost workflow directory."""

    return Path(__file__).resolve().parents[2] / "workflows" / "selfhost"


def _parse_node(raw: ComfyUINodeConfig | Mapping[str, Any]) -> ComfyUINodeConfig:
    if isinstance(raw, ComfyUINodeConfig):
        return raw
    return ComfyUINodeConfig(**raw)


def validate_comfyui_configuration(
    nodes: Sequence[ComfyUINodeConfig | Mapping[str, Any]],
    *,
    workflow_root: str | Path,
    private_comfyui_enabled: bool = True,
) -> PreflightReport:
    """Validate the private-GPU configuration without contacting any GPU.

    Validation rules (all failures are reported before any task is created):

    - node ids must be unique;
    - every workflow type a node declares must be a registered spec;
    - every registered spec must be served by at least one *enabled* node,
      distinguishing "no node at all" from "only disabled nodes";
    - every registered spec's workflow JSON must exist and parse;
    - when ``private_comfyui_enabled`` is true there must be at least one
      enabled node.
    """

    report = PreflightReport()
    root = Path(workflow_root)

    parsed: list[ComfyUINodeConfig] = []
    seen_ids: set[str] = set()
    for raw in nodes:
        node = _parse_node(raw)
        if node.id in seen_ids:
            report.errors.append(f"Duplicate ComfyUI node id '{node.id}'")
        seen_ids.add(node.id)
        parsed.append(node)

    # A node must not advertise a workflow type the registry does not know.
    for node in parsed:
        for workflow_type in node.workflow_types:
            if workflow_type not in WORKFLOW_SPECS:
                report.errors.append(
                    f"Node '{node.id}' declares unknown workflow type '{workflow_type}'"
                )

    # Every registered spec's workflow file must exist and be parseable.
    for workflow_type, spec in sorted(WORKFLOW_SPECS.items()):
        path = spec.path(root)
        if not path.is_file():
            report.errors.append(f"Workflow '{workflow_type}' JSON missing: {path}")
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            report.errors.append(f"Workflow '{workflow_type}' JSON unparseable: {exc}")

    # Every registered spec must be served by an enabled node. A missing or
    # disabled node is a *capacity* gap (degraded/restricted mode), not a
    # configuration error, so these are warnings rather than blocking errors.
    enabled_owners: dict[str, list[str]] = {}
    disabled_owners: dict[str, list[str]] = {}
    for node in parsed:
        for workflow_type in node.workflow_types:
            bucket = enabled_owners if node.enabled else disabled_owners
            bucket.setdefault(workflow_type, []).append(node.id)

    for workflow_type in sorted(WORKFLOW_SPECS):
        if workflow_type in enabled_owners:
            continue
        if workflow_type in disabled_owners:
            report.warnings.append(
                f"Workflow '{workflow_type}' has no enabled node "
                f"(declared by disabled node(s): {', '.join(disabled_owners[workflow_type])})"
            )
        else:
            report.warnings.append(f"Workflow '{workflow_type}' has no matching node")

    # private_comfyui enabled but no enabled node at all.
    if private_comfyui_enabled and not any(node.enabled for node in parsed):
        report.warnings.append("private_comfyui_enabled is true but no ComfyUI node is enabled")
        report.restricted_mode = True

    return report


def validate_enabled_node_health(
    nodes: Sequence[ComfyUINodeConfig | Mapping[str, Any]],
    *,
    transport: httpx.BaseTransport | None = None,
) -> list[str]:
    """Probe enabled nodes before startup without inheriting host proxy settings."""

    errors: list[str] = []
    with httpx.Client(
        transport=transport,
        trust_env=False,
        follow_redirects=True,
        timeout=10,
    ) as client:
        for raw in nodes:
            node = _parse_node(raw)
            if not node.enabled:
                continue
            try:
                response = client.get(f"{node.base_url}/system_stats")
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not payload.get("devices"):
                    raise ValueError("response does not report any device")
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"ComfyUI node '{node.id}' health check failed: {exc}")
    return errors


def run_preflight_from_config(config_path: str | Path) -> PreflightReport:
    """Load a config file and validate its private-GPU configuration.

    This is the launcher-facing entry point: it reads ``config.yaml``'s
    ``comfyui.nodes`` and runs the static check without contacting any GPU.
    """

    from pixelle_video.config.loader import load_config_dict
    from pixelle_video.config.schema import PixelleVideoConfig

    # Load directly rather than via ConfigManager: its module-level singleton is
    # bound to the default config.yaml on first import, so `ConfigManager(path)`
    # silently ignores a caller-supplied path.
    config = PixelleVideoConfig(**load_config_dict(str(config_path)))
    return validate_comfyui_configuration(
        config.comfyui.nodes,
        workflow_root=default_workflow_root(),
        private_comfyui_enabled=config.media_jobs.private_comfyui_enabled,
    )


def main() -> int:
    """CLI entry point: print the report and return 0/1 as an exit code.

    Exit codes:
      0 -- configuration is consistent and private GPU capacity is available;
      1 -- a blocking configuration error was found;
      2 -- configuration is consistent but startup must use restricted mode.
    """

    import argparse

    parser = argparse.ArgumentParser(description="Validate private-GPU configuration")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument(
        "--check-network",
        action="store_true",
        help="also require every enabled ComfyUI node to answer /system_stats",
    )
    args = parser.parse_args()

    report = run_preflight_from_config(args.config)
    if args.check_network and report.ok and not report.restricted:
        from pixelle_video.config.loader import load_config_dict
        from pixelle_video.config.schema import PixelleVideoConfig

        config = PixelleVideoConfig(**load_config_dict(str(args.config)))
        report.errors.extend(validate_enabled_node_health(config.comfyui.nodes))
    print(str(report))
    if report.restricted:
        print(
            "[INFO] Restricted mode: private media tasks will be rejected "
            "until an enabled GPU node is configured."
        )
    if not report.ok:
        return 1
    return 2 if report.restricted else 0


if __name__ == "__main__":
    raise SystemExit(main())
