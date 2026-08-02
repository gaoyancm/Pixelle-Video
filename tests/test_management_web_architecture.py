from __future__ import annotations

import ast
from pathlib import Path

MANAGEMENT_WEB = Path("web/management")
MANAGEMENT_PAGES = tuple(Path("web/pages").glob("[3-7]_*.py"))


def test_navigation_preserves_home_history_and_exposes_five_management_views():
    source = Path("web/app.py").read_text(encoding="utf-8")
    for title in (
        "Home",
        "History",
        "Projects",
        "Batches",
        "Batch Editor",
        "Batch Monitor",
        "Results",
    ):
        assert f'title="{title}"' in source
    assert len(MANAGEMENT_PAGES) == 5


def test_management_web_has_one_http_boundary_and_no_repository_core_provider_bypass():
    files = tuple(MANAGEMENT_WEB.rglob("*.py")) + MANAGEMENT_PAGES
    forbidden_imports = (
        "pixelle_video.management",
        "pixelle_video.media_jobs",
        "pixelle_video.media_assets",
        "pixelle_video.service",
        "pixelle_video.services",
        "api.routers",
        "api.services",
    )
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(
                name == forbidden or name.startswith(f"{forbidden}.")
                for name in names
                for forbidden in forbidden_imports
            ), f"forbidden management Web import in {path}: {names}"
    httpx_imports = [path for path in files if "import httpx" in path.read_text(encoding="utf-8")]
    assert httpx_imports == [MANAGEMENT_WEB / "client.py"]


def test_management_pages_do_not_use_local_paths_fake_gpu_progress_or_forbidden_actions():
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (*tuple(MANAGEMENT_WEB.rglob("*.py")), *MANAGEMENT_PAGES)
    ).lower()
    for forbidden in (
        "pixellevideocore",
        "historymanager",
        "persistenceservice",
        "progress_event",
        "remote interrupt",
        "force stop",
        "automatic retry",
        "batch zip",
        "zipfile",
        "open(",
        "sleep(",
    ):
        assert forbidden not in source
    assert "completed_items" in source and "total_items" in source
    assert "get_content" in source and "resolve_managed_href" in source


def test_management_client_is_the_only_place_with_api_paths_and_has_no_secret_logging():
    client = (MANAGEMENT_WEB / "client.py").read_text(encoding="utf-8")
    tree = ast.parse(client)
    methods = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert {
        "list_projects",
        "create_project",
        "get_project",
        "update_project",
        "archive_project",
        "list_batches",
        "create_batch",
        "get_batch",
        "update_batch",
        "archive_batch",
        "replace_items",
        "preflight",
        "submit",
        "workflows",
        "update_batch_priority",
        "update_item_priority",
        "progress",
        "results",
        "cancel",
        "retry_eligible",
    }.issubset(methods)
    for path in tuple(MANAGEMENT_WEB.rglob("*.py")) + MANAGEMENT_PAGES:
        if path.name == "client.py":
            continue
        assert "/api/admin" not in path.read_text(encoding="utf-8")
    assert "logger" not in client.lower()
    assert "idempotency-key" in client.lower()
