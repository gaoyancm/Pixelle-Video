"""Single synchronous HTTP boundary used by the Streamlit management pages."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, BinaryIO
from urllib.parse import unquote, urljoin, urlsplit

import httpx

DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
API_BASE_URL_ENV = "PIXELLE_MANAGEMENT_API_URL"


@dataclass(frozen=True)
class ManagementAPIError(RuntimeError):
    code: str
    message: str
    status_code: int | None = None
    issues: tuple[dict[str, Any], ...] = ()
    retry_same_intent: bool = False

    def __str__(self) -> str:
        return self.message


class ManagementAPIClient:
    """Typed-enough wrapper around all 20 management operations and managed assets."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ):
        configured = (base_url or os.getenv(API_BASE_URL_ENV) or DEFAULT_API_BASE_URL).strip()
        parsed = urlsplit(configured)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("management API base URL must be HTTP(S)")
        self.base_url = configured.rstrip("/")
        self._origin = (parsed.scheme.lower(), parsed.netloc.lower())
        self._origin_url = f"{parsed.scheme}://{parsed.netloc}"
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def _json(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise ManagementAPIError("timeout", "The management service timed out.") from None
        except httpx.RequestError:
            raise ManagementAPIError(
                "unreachable", "The management service is unavailable."
            ) from None
        if response.is_success:
            try:
                payload = response.json()
            except ValueError:
                raise ManagementAPIError(
                    "invalid_response", "The management service returned an invalid response."
                ) from None
            if not isinstance(payload, dict):
                raise ManagementAPIError(
                    "invalid_response", "The management service returned an invalid response."
                )
            return payload
        self._raise_response_error(response)

    @staticmethod
    def _expect(payload: dict[str, Any], *fields: str) -> dict[str, Any]:
        if any(field not in payload for field in fields):
            raise ManagementAPIError(
                "invalid_response", "The management service returned an incomplete response."
            )
        return payload

    @staticmethod
    def _segment(value: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or any(token in value for token in ("/", "\\", "?", "#", "%"))
            or value in {".", ".."}
        ):
            raise ValueError("management resource id is invalid")
        return value

    @staticmethod
    def _raise_response_error(response: httpx.Response) -> None:
        code = "service_error"
        issues: tuple[dict[str, Any], ...] = ()
        try:
            detail = response.json().get("error", {})
            if isinstance(detail, dict):
                code = str(detail.get("code") or code)
                raw_issues = detail.get("issues")
                if isinstance(raw_issues, list):
                    issues = tuple(item for item in raw_issues if isinstance(item, dict))
        except (ValueError, AttributeError):
            pass
        messages = {
            "not_found": "The requested project or batch no longer exists.",
            "asset_not_found": "The requested managed asset no longer exists.",
            "version_conflict": "The batch changed. Reload it before merging your edits.",
            "state_conflict": "The requested action is not valid for the current state.",
            "idempotency_conflict": "This action key belongs to different content.",
            "invalid_request": "The request is invalid.",
            "invalid_asset": "The uploaded file is not a supported managed asset.",
            "preflight_failed": "Batch preflight found issues that must be resolved.",
            "submission_indeterminate": (
                "Submission outcome is uncertain. Confirm again with the same action key."
            ),
            "operation_indeterminate": (
                "Operation outcome is uncertain. Confirm again with the same action key."
            ),
            "service_unavailable": "The management service is temporarily unavailable.",
        }
        if response.status_code == 404 and code == "service_error":
            code = "not_found"
        elif response.status_code == 409 and code == "service_error":
            code = "state_conflict"
        elif response.status_code == 422 and code == "service_error":
            code = "invalid_request"
        retry_same = code in {"submission_indeterminate", "operation_indeterminate"}
        raise ManagementAPIError(
            code,
            messages.get(code, "The management service could not complete the request."),
            response.status_code,
            issues,
            retry_same,
        )

    def resolve_managed_href(self, href: str) -> str:
        if not isinstance(href, str) or not href:
            raise ValueError("managed content href is missing")
        parsed = urlsplit(href)
        if parsed.scheme or parsed.netloc:
            if (parsed.scheme.lower(), parsed.netloc.lower()) != self._origin:
                raise ValueError("managed content href must be same-origin")
            path = parsed.path
        else:
            path = parsed.path
        decoded_path = unquote(path)
        if (
            parsed.query
            or parsed.fragment
            or "%" in path
            or "\\" in decoded_path
            or ".." in decoded_path.split("/")
        ):
            raise ValueError("managed content href is unsafe")
        parts = path.split("/")
        if len(parts) != 5 or parts[:3] != ["", "api", "assets"] or parts[4] != "content":
            raise ValueError("managed content href is outside the asset boundary")
        if not parts[3]:
            raise ValueError("managed content href is missing an asset id")
        return urljoin(f"{self._origin_url}/", path.lstrip("/"))

    def get_content(self, href: str) -> bytes:
        url = self.resolve_managed_href(href)
        try:
            response = self._client.get(url)
        except (httpx.TimeoutException, httpx.RequestError):
            raise ManagementAPIError(
                "unreachable", "The managed asset could not be downloaded."
            ) from None
        if not response.is_success:
            self._raise_response_error(response)
        return response.content

    def upload_asset(
        self, *, filename: str, mime_type: str, stream: BinaryIO | bytes, idempotency_key: str
    ) -> dict[str, Any]:
        return self._expect(
            self._json(
                "POST",
                "/api/assets",
                files={"file": (filename, stream, mime_type)},
                headers={"Idempotency-Key": idempotency_key},
            ),
            "asset_id",
            "original_filename",
            "mime_type",
        )

    def list_assets(self, **params) -> dict[str, Any]:
        return self._expect(self._json("GET", "/api/assets", params=params), "items")

    def list_projects(self, **params) -> dict[str, Any]:
        return self._expect(self._json("GET", "/api/admin/projects", params=params), "items")

    def create_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._expect(self._json("POST", "/api/admin/projects", json=payload), "project_id")

    def get_project(self, project_id: str) -> dict[str, Any]:
        project_id = self._segment(project_id)
        return self._expect(self._json("GET", f"/api/admin/projects/{project_id}"), "project_id")

    def update_project(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._segment(project_id)
        return self._json("PATCH", f"/api/admin/projects/{project_id}", json=payload)

    def archive_project(self, project_id: str) -> dict[str, Any]:
        project_id = self._segment(project_id)
        return self._json("POST", f"/api/admin/projects/{project_id}/archive")

    def list_batches(self, project_id: str, **params) -> dict[str, Any]:
        project_id = self._segment(project_id)
        return self._expect(
            self._json("GET", f"/api/admin/projects/{project_id}/batches", params=params), "items"
        )

    def create_batch(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = self._segment(project_id)
        return self._expect(
            self._json("POST", f"/api/admin/projects/{project_id}/batches", json=payload),
            "batch_id",
        )

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        batch_id = self._segment(batch_id)
        return self._expect(
            self._json("GET", f"/api/admin/batches/{batch_id}"),
            "batch_id",
            "version",
            "state",
            "items",
        )

    def update_batch(self, batch_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        batch_id = self._segment(batch_id)
        return self._json("PATCH", f"/api/admin/batches/{batch_id}", json=payload)

    def archive_batch(self, batch_id: str) -> dict[str, Any]:
        batch_id = self._segment(batch_id)
        return self._json("POST", f"/api/admin/batches/{batch_id}/archive")

    def replace_items(self, batch_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        batch_id = self._segment(batch_id)
        return self._expect(
            self._json("PUT", f"/api/admin/batches/{batch_id}/items", json=payload),
            "batch_version",
            "items",
        )

    def preflight(self, batch_id: str, expected_version: int) -> dict[str, Any]:
        batch_id = self._segment(batch_id)
        return self._expect(
            self._json(
                "POST",
                f"/api/admin/batches/{batch_id}/preflight",
                json={"expected_batch_version": expected_version},
            ),
            "valid",
            "issues",
        )

    def submit(self, batch_id: str, expected_version: int, key: str) -> dict[str, Any]:
        return self._idempotent("submit", batch_id, expected_version, key)

    def workflows(self) -> dict[str, Any]:
        return self._expect(self._json("GET", "/api/admin/workflows"), "items")

    def update_batch_priority(
        self, batch_id: str, expected_version: int, priority: str
    ) -> dict[str, Any]:
        batch_id = self._segment(batch_id)
        return self._json(
            "PATCH",
            f"/api/admin/batches/{batch_id}/priority",
            json={"expected_batch_version": expected_version, "priority": priority},
        )

    def update_item_priority(
        self, item_id: str, expected_version: int, priority: str | None
    ) -> dict[str, Any]:
        item_id = self._segment(item_id)
        return self._json(
            "PATCH",
            f"/api/admin/items/{item_id}/priority",
            json={"expected_batch_version": expected_version, "priority": priority},
        )

    def progress(self, batch_id: str) -> dict[str, Any]:
        batch_id = self._segment(batch_id)
        return self._expect(
            self._json("GET", f"/api/admin/batches/{batch_id}/progress"),
            "completed_items",
            "total_items",
            "counts",
        )

    def results(self, batch_id: str) -> dict[str, Any]:
        batch_id = self._segment(batch_id)
        return self._expect(self._json("GET", f"/api/admin/batches/{batch_id}/results"), "items")

    def cancel(self, batch_id: str, expected_version: int, key: str) -> dict[str, Any]:
        return self._idempotent("cancel", batch_id, expected_version, key)

    def retry_eligible(self, batch_id: str, expected_version: int, key: str) -> dict[str, Any]:
        return self._idempotent("retry-eligible", batch_id, expected_version, key)

    def _idempotent(self, action: str, batch_id: str, expected_version: int, key: str):
        batch_id = self._segment(batch_id)
        return self._expect(
            self._json(
                "POST",
                f"/api/admin/batches/{batch_id}/{action}",
                json={"expected_batch_version": expected_version},
                headers={"Idempotency-Key": key},
            ),
            "operation_id",
        )
