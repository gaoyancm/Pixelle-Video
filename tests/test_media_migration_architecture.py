from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_ROOT = PROJECT_ROOT / "pixelle_video" / "media_migration"

FORBIDDEN_REFERENCES = {
    "ComfyUIAdapter",
    "MediaJobRepository",
    "TaskManager",
    "task_manager",
    "create_job",
    "create_job_with_assets",
}


def forbidden_references(source: str) -> set[str]:
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_REFERENCES:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_REFERENCES:
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            leaf = node.name.rsplit(".", 1)[-1]
            if leaf in FORBIDDEN_REFERENCES:
                found.add(leaf)
    return found


def test_declared_facade_scope_has_no_execution_or_dual_state_bypass():
    violations = {}
    for path in MIGRATION_ROOT.rglob("*.py"):
        found = forbidden_references(path.read_text(encoding="utf-8"))
        if found:
            violations[str(path.relative_to(PROJECT_ROOT))] = sorted(found)
    assert violations == {}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from x import ComfyUIAdapter\nComfyUIAdapter()", {"ComfyUIAdapter"}),
        ("repository.create_job(payload)", {"create_job"}),
        ("from api.tasks import task_manager", {"task_manager"}),
        (
            "from pixelle_video.media_jobs import MediaJobRepository as Repo",
            {"MediaJobRepository"},
        ),
    ],
)
def test_architecture_gate_rejects_multiple_bypass_spellings(source, expected):
    assert forbidden_references(source) == expected


def test_architecture_gate_accepts_injected_single_route_submitters():
    source = """
async def submit(request, persistent_submitter, legacy_submitter):
    if request.is_leaf:
        return await persistent_submitter(request)
    return await legacy_submitter(request)
"""
    assert forbidden_references(source) == set()
