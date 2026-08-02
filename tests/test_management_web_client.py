from __future__ import annotations

import io
import json

import httpx
import pytest

from web.management.client import ManagementAPIClient, ManagementAPIError


def test_client_wraps_all_twenty_management_operations_and_assets_without_network():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"managed")
        return httpx.Response(
            200,
            json={
                "items": [],
                "project_id": "p1",
                "batch_id": "b1",
                "batch_version": 2,
                "version": 1,
                "state": "draft",
                "valid": True,
                "issues": [],
                "completed_items": 0,
                "total_items": 0,
                "counts": {},
                "operation_id": "op1",
                "asset_id": "a1",
                "original_filename": "input.png",
                "mime_type": "image/png",
            },
        )

    client = ManagementAPIClient(
        "https://management.example.test", transport=httpx.MockTransport(handler)
    )
    client.list_projects(include_archived=True)
    client.create_project({"name": "p"})
    client.get_project("p1")
    client.update_project("p1", {"name": "p"})
    client.archive_project("p1")
    client.list_batches("p1", include_archived=True)
    client.create_batch("p1", {"name": "b"})
    client.get_batch("b1")
    client.update_batch("b1", {"name": "b"})
    client.archive_batch("b1")
    client.replace_items("b1", {"expected_batch_version": 1, "items": []})
    client.preflight("b1", 1)
    client.submit("b1", 1, "submit-key")
    client.workflows()
    client.update_batch_priority("b1", 1, "high")
    client.update_item_priority("i1", 1, None)
    client.progress("b1")
    client.results("b1")
    client.cancel("b1", 1, "cancel-key")
    client.retry_eligible("b1", 1, "retry-key")
    client.list_assets(kind="input")
    client.upload_asset(
        filename="input.png",
        mime_type="image/png",
        stream=io.BytesIO(b"png"),
        idempotency_key="asset-key",
    )
    assert client.get_content("/api/assets/a1/content") == b"managed"

    management = [request for request in requests if request.url.path.startswith("/api/admin")]
    assert len(management) == 20
    assert {(request.method, request.url.path) for request in management} == {
        ("GET", "/api/admin/projects"),
        ("POST", "/api/admin/projects"),
        ("GET", "/api/admin/projects/p1"),
        ("PATCH", "/api/admin/projects/p1"),
        ("POST", "/api/admin/projects/p1/archive"),
        ("GET", "/api/admin/projects/p1/batches"),
        ("POST", "/api/admin/projects/p1/batches"),
        ("GET", "/api/admin/batches/b1"),
        ("PATCH", "/api/admin/batches/b1"),
        ("POST", "/api/admin/batches/b1/archive"),
        ("PUT", "/api/admin/batches/b1/items"),
        ("POST", "/api/admin/batches/b1/preflight"),
        ("POST", "/api/admin/batches/b1/submit"),
        ("GET", "/api/admin/workflows"),
        ("PATCH", "/api/admin/batches/b1/priority"),
        ("PATCH", "/api/admin/items/i1/priority"),
        ("GET", "/api/admin/batches/b1/progress"),
        ("GET", "/api/admin/batches/b1/results"),
        ("POST", "/api/admin/batches/b1/cancel"),
        ("POST", "/api/admin/batches/b1/retry-eligible"),
    }
    keyed = {
        request.url.path: request.headers.get("Idempotency-Key")
        for request in requests
        if request.headers.get("Idempotency-Key")
    }
    assert keyed == {
        "/api/admin/batches/b1/submit": "submit-key",
        "/api/admin/batches/b1/cancel": "cancel-key",
        "/api/admin/batches/b1/retry-eligible": "retry-key",
        "/api/assets": "asset-key",
    }
    upload = next(
        request
        for request in requests
        if request.url.path == "/api/assets" and request.method == "POST"
    )
    assert b'filename="input.png"' in upload.content and b"png" in upload.content


@pytest.mark.parametrize(
    ("status", "code", "expected", "same_key"),
    [
        (404, "not_found", "not_found", False),
        (409, "version_conflict", "version_conflict", False),
        (409, "idempotency_conflict", "idempotency_conflict", False),
        (422, "invalid_request", "invalid_request", False),
        (422, "preflight_failed", "preflight_failed", False),
        (503, "submission_indeterminate", "submission_indeterminate", True),
        (503, "operation_indeterminate", "operation_indeterminate", True),
        (500, "internal_error", "internal_error", False),
    ],
)
def test_client_maps_safe_errors_and_preserves_structured_issues(status, code, expected, same_key):
    payload = {
        "error": {
            "code": code,
            "message": "sensitive database path C:/private.db",
            "issues": [{"code": "bad_item", "position": 2, "field": "prompt"}],
        }
    }
    client = ManagementAPIClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(status, json=payload))
    )
    with pytest.raises(ManagementAPIError) as raised:
        client.list_projects()
    assert raised.value.code == expected
    assert raised.value.retry_same_intent is same_key
    assert raised.value.issues == ({"code": "bad_item", "position": 2, "field": "prompt"},)
    assert "private.db" not in str(raised.value)


def test_client_maps_timeout_connection_non_json_and_invalid_shape_without_sensitive_detail():
    def timeout(_request):
        raise httpx.ReadTimeout("secret URL")

    with pytest.raises(ManagementAPIError, match="timed out"):
        ManagementAPIClient(transport=httpx.MockTransport(timeout)).list_projects()

    def disconnected(_request):
        raise httpx.ConnectError("token=secret")

    with pytest.raises(ManagementAPIError, match="unavailable"):
        ManagementAPIClient(transport=httpx.MockTransport(disconnected)).list_projects()

    for response in (
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"unexpected": True}),
    ):
        client = ManagementAPIClient(
            transport=httpx.MockTransport(lambda _request, response=response: response)
        )
        with pytest.raises(ManagementAPIError, match="response"):
            client.list_projects()


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example/api/assets/a/content",
        "file:///C:/secret",
        "//evil.example/api/assets/a/content",
        "C:\\secret\\file.mp4",
        "/api/assets/../secret/content",
        "/api/assets/a/content?token=secret",
        "/api/assets/%2e%2e/content",
        "/api/files/a",
        "",
    ],
)
def test_client_rejects_non_managed_or_unsafe_content_hrefs(href):
    client = ManagementAPIClient("https://management.example.test")
    with pytest.raises(ValueError):
        client.resolve_managed_href(href)


def test_client_allows_relative_and_same_origin_managed_content_hrefs():
    client = ManagementAPIClient("https://management.example.test/root")
    assert client.resolve_managed_href("/api/assets/asset-1/content") == (
        "https://management.example.test/api/assets/asset-1/content"
    )
    assert (
        client.resolve_managed_href("https://management.example.test/api/assets/asset-1/content")
        == "https://management.example.test/api/assets/asset-1/content"
    )


@pytest.mark.parametrize("resource_id", ("../b2", "a/b", "a\\b", "a?token=secret", ""))
def test_client_rejects_untrusted_context_ids_before_building_paths(resource_id):
    client = ManagementAPIClient(transport=httpx.MockTransport(lambda _request: None))
    with pytest.raises(ValueError, match="resource id"):
        client.get_batch(resource_id)


def test_error_dataclass_never_serializes_original_response():
    error = ManagementAPIError("service_error", "Safe message", 500)
    assert json.loads(json.dumps(error.__dict__)) == {
        "code": "service_error",
        "message": "Safe message",
        "status_code": 500,
        "issues": [],
        "retry_same_intent": False,
    }
