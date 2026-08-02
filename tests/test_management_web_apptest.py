from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from web.management.client import ManagementAPIError
from web.management.runtime import set_management_client_factory


class FakeManagementClient:
    def __init__(self, *, batch_state="draft", items=None):
        self.calls = []
        self.batch_state = batch_state
        self.items = items

    def _call(self, name, *args):
        self.calls.append((name, *args))

    def list_projects(self, **params):
        self._call("list_projects", params)
        return {
            "items": [
                {
                    "project_id": "p1",
                    "name": "Project One",
                    "description": "Persistent project",
                    "archived": False,
                }
            ],
            "has_more": False,
        }

    def create_project(self, payload):
        self._call("create_project", payload)
        return {"project_id": "p2", **payload, "archived": False}

    def update_project(self, project_id, payload):
        self._call("update_project", project_id, payload)
        return {"project_id": project_id, **payload, "archived": False}

    def archive_project(self, project_id):
        self._call("archive_project", project_id)
        return {"project_id": project_id, "archived": True}

    def get_project(self, project_id):
        self._call("get_project", project_id)
        return {"project_id": project_id, "name": "Project One", "archived": False}

    def workflows(self):
        self._call("workflows")
        return {
            "items": [
                {
                    "workflow": "a800_wan22_t2v_33f",
                    "display_name": "T2V 33F",
                    "requires_image": False,
                    "input_asset_count": 0,
                    "available": True,
                    "parameters": [
                        {
                            "name": "prompt",
                            "required": True,
                            "type": "str | None",
                            "constraints": {"maxLength": 1000},
                        }
                    ],
                }
            ]
        }

    def list_batches(self, project_id, **params):
        self._call("list_batches", project_id, params)
        return {"items": [self.get_batch("b1")], "has_more": False}

    def create_batch(self, project_id, payload):
        self._call("create_batch", project_id, payload)
        return self.get_batch("b1")

    def archive_batch(self, batch_id):
        self._call("archive_batch", batch_id)
        return self.get_batch(batch_id) | {"archived": True}

    def get_batch(self, batch_id):
        self._call("get_batch", batch_id)
        return {
            "batch_id": batch_id,
            "project_id": "p1",
            "name": "Batch One",
            "workflow": "a800_wan22_t2v_33f",
            "state": self.batch_state,
            "common_parameters": {"prompt": "safe"},
            "default_priority": "normal",
            "version": 3,
            "archived": False,
            "items": self.items
            if self.items is not None
            else [
                {
                    "item_id": "i1",
                    "position": 0,
                    "parameter_overrides": {},
                    "priority_override": None,
                    "assets": [],
                }
            ],
        }

    def update_batch(self, batch_id, payload):
        self._call("update_batch", batch_id, payload)
        return self.get_batch(batch_id) | {"version": payload["expected_batch_version"] + 1}

    def replace_items(self, batch_id, payload):
        self._call("replace_items", batch_id, payload)
        return {"batch_id": batch_id, "batch_version": payload["expected_batch_version"] + 1}

    def list_assets(self, **params):
        self._call("list_assets", params)
        return {"items": [], "has_more": False}

    def upload_asset(self, **kwargs):
        self._call("upload_asset", kwargs["filename"], kwargs["idempotency_key"])
        return {"asset_id": "a1", "original_filename": kwargs["filename"]}

    def preflight(self, batch_id, expected_version):
        self._call("preflight", batch_id, expected_version)
        return {"valid": True, "issues": [], "batch_version": expected_version}

    def submit(self, batch_id, expected_version, key):
        self._call("submit", batch_id, expected_version, key)
        return {"operation_id": "submit-op", "jobs": []}

    def progress(self, batch_id):
        self._call("progress", batch_id)
        return {
            "batch_id": batch_id,
            "batch_state": "submitted",
            "completed_items": 0,
            "total_items": 1,
            "derived_result": None,
            "counts": {
                "queued": 1,
                "submitting": 0,
                "running": 0,
                "cancel_requested": 0,
                "succeeded": 0,
                "failed": 0,
                "timed_out": 0,
                "cancelled": 0,
            },
        }

    def results(self, batch_id):
        self._call("results", batch_id)
        return {
            "batch_id": batch_id,
            "items": [
                {
                    "item_id": "i1",
                    "position": 0,
                    "current_attempt_no": 1,
                    "attempts": [
                        {
                            "attempt_no": 1,
                            "job_id": "j1",
                            "workflow": "a800_wan22_t2v_33f",
                            "retry_of_attempt_no": None,
                            "retry_of_job_id": None,
                            "status": "queued",
                            "effective_priority": "normal",
                            "cancel_requested": False,
                            "can_retry": False,
                            "created_at": "2026-08-02T00:00:00Z",
                            "updated_at": "2026-08-02T00:00:00Z",
                            "error": None,
                            "outputs": [],
                        }
                    ],
                }
            ],
        }

    def update_batch_priority(self, batch_id, expected_version, priority):
        self._call("update_batch_priority", batch_id, expected_version, priority)
        return {
            "updated": 1,
            "unchanged": 0,
            "overridden": 0,
            "skipped_claimed_or_nonqueued": 0,
        }

    def update_item_priority(self, item_id, expected_version, priority):
        self._call("update_item_priority", item_id, expected_version, priority)
        return {"job_priority_updated": True}

    def cancel(self, batch_id, expected_version, key):
        self._call("cancel", batch_id, expected_version, key)
        return {
            "requested": 1,
            "already_requested": 0,
            "skipped_terminal": 0,
            "not_found": 0,
        }

    def retry_eligible(self, batch_id, expected_version, key):
        self._call("retry_eligible", batch_id, expected_version, key)
        return {"retried": 0, "skipped": 1}

    def get_content(self, href):
        self._call("get_content", href)
        return b"managed"


def page(prefix: str) -> Path:
    return next(Path("web/pages").glob(f"{prefix}_*.py"))


@pytest.mark.parametrize(
    ("prefix", "title", "expected_call"),
    [
        ("3", "Projects", "list_projects"),
        ("4", "Batches", "list_batches"),
        ("5", "Batch Editor", "list_assets"),
        ("6", "Batch Monitor", "progress"),
        ("7", "Results", "results"),
    ],
)
def test_each_management_view_renders_offline_through_injected_client(prefix, title, expected_call):
    fake = FakeManagementClient()
    set_management_client_factory(lambda: fake)
    app = AppTest.from_file(str(page(prefix)))
    app.session_state["management_project_id"] = "p1"
    app.session_state["management_batch_id"] = "b1"
    app.run(timeout=10)
    assert not app.exception
    assert any(element.value == title for element in app.title)
    assert expected_call in [call[0] for call in fake.calls]


def test_editor_submit_rerun_reuses_the_same_intent_key():
    fake = FakeManagementClient()
    set_management_client_factory(lambda: fake)
    app = AppTest.from_file(str(page("5")))
    app.session_state["management_batch_id"] = "b1"
    app.run(timeout=10)
    preflight = next(button for button in app.button if button.label == "Run preflight")
    preflight.click().run(timeout=10)
    confirm = next(box for box in app.checkbox if box.label == "Confirm batch submission")
    confirm.check().run(timeout=10)
    submit = next(button for button in app.button if button.label == "Submit batch")
    submit.click().run(timeout=10)
    first = [call for call in fake.calls if call[0] == "submit"][-1]
    confirm = next(box for box in app.checkbox if box.label == "Confirm batch submission")
    confirm.check().run(timeout=10)
    submit = next(button for button in app.button if button.label == "Submit batch")
    submit.click().run(timeout=10)
    second = [call for call in fake.calls if call[0] == "submit"][-1]
    assert first[-1] == second[-1]


def test_projects_and_batches_create_actions_use_injected_api_client():
    fake = FakeManagementClient()
    set_management_client_factory(lambda: fake)
    projects = AppTest.from_file(str(page("3"))).run(timeout=10)
    next(field for field in projects.text_input if field.label == "Project name").set_value(
        "New Project"
    )
    next(field for field in projects.text_area if field.label == "Description").set_value("Managed")
    next(button for button in projects.button if button.label == "Create project").click().run(
        timeout=10
    )
    assert [call for call in fake.calls if call[0] == "create_project"][-1][1] == {
        "name": "New Project",
        "description": "Managed",
    }

    batches = AppTest.from_file(str(page("4")))
    batches.session_state["management_project_id"] = "p1"
    batches.run(timeout=10)
    next(field for field in batches.text_input if field.label == "Batch name").set_value(
        "New Batch"
    )
    next(button for button in batches.button if button.label == "Create batch").click().run(
        timeout=10
    )
    created = [call for call in fake.calls if call[0] == "create_batch"][-1]
    assert created[1] == "p1" and created[2]["workflow"] == "a800_wan22_t2v_33f"


def test_editor_save_sends_full_item_collection_and_latest_server_version():
    fake = FakeManagementClient()
    set_management_client_factory(lambda: fake)
    app = AppTest.from_file(str(page("5")))
    app.session_state["management_batch_id"] = "b1"
    app.run(timeout=10)
    next(button for button in app.button if button.label == "Save draft").click().run(timeout=10)
    update_call = [call for call in fake.calls if call[0] == "update_batch"][-1]
    replace_call = [call for call in fake.calls if call[0] == "replace_items"][-1]
    assert update_call[2]["expected_batch_version"] == 3
    assert replace_call[2]["expected_batch_version"] == 4
    assert replace_call[2]["items"] == [
        {
            "item_id": "i1",
            "position": 0,
            "parameter_overrides": {},
            "priority_override": None,
            "assets": [],
        }
    ]
    assert len([call for call in fake.calls if call[0] == "get_batch"]) >= 2


def test_monitor_distinguishes_cancel_request_truth_and_has_no_fake_gpu_percentage():
    fake = FakeManagementClient()
    set_management_client_factory(lambda: fake)
    app = AppTest.from_file(str(page("6")))
    app.session_state["management_batch_id"] = "b1"
    app.run(timeout=10)
    text = " ".join(
        str(element.value)
        for collection in (app.caption, app.info, app.markdown)
        for element in collection
    )
    assert "Remote computation is not guaranteed to stop immediately" in text
    assert "GPU" not in text and "estimated" not in text.lower()


def test_monitor_actions_use_batch_operations_stable_keys_and_zero_retry_is_success():
    fake = FakeManagementClient(batch_state="submitted")
    set_management_client_factory(lambda: fake)
    app = AppTest.from_file(str(page("6")))
    app.session_state["management_batch_id"] = "b1"
    app.run(timeout=10)

    cancel_confirm = next(
        box for box in app.checkbox if box.label == "Confirm cancellation request"
    )
    cancel_confirm.check().run(timeout=10)
    cancel_button = next(
        button for button in app.button if button.label == "Request batch cancellation"
    )
    cancel_button.click().run(timeout=10)
    first_cancel = [call for call in fake.calls if call[0] == "cancel"][-1]
    cancel_confirm = next(
        box for box in app.checkbox if box.label == "Confirm cancellation request"
    )
    cancel_confirm.check().run(timeout=10)
    cancel_button = next(
        button for button in app.button if button.label == "Request batch cancellation"
    )
    cancel_button.click().run(timeout=10)
    second_cancel = [call for call in fake.calls if call[0] == "cancel"][-1]
    assert first_cancel[-1] == second_cancel[-1]

    retry_confirm = next(
        box
        for box in app.checkbox
        if box.label == "Confirm retry of currently eligible failed items"
    )
    retry_confirm.check().run(timeout=10)
    retry_button = next(button for button in app.button if button.label == "Retry eligible items")
    retry_button.click().run(timeout=10)
    assert [call for call in fake.calls if call[0] == "retry_eligible"][-1][1:3] == ("b1", 3)


def test_editor_shows_structured_preflight_issues_and_blocks_unsaved_changes():
    class IssueClient(FakeManagementClient):
        def preflight(self, batch_id, expected_version):
            self._call("preflight", batch_id, expected_version)
            return {
                "valid": False,
                "batch_version": expected_version,
                "issues": [
                    {
                        "code": "missing_prompt",
                        "message": "Prompt is required.",
                        "item_id": "i1",
                        "position": 0,
                        "field": "parameters.prompt",
                    }
                ],
            }

    fake = IssueClient()
    set_management_client_factory(lambda: fake)
    app = AppTest.from_file(str(page("5")))
    app.session_state["management_batch_id"] = "b1"
    app.run(timeout=10)
    next(button for button in app.button if button.label == "Run preflight").click().run(timeout=10)
    rendered = " ".join(
        str(element.value) for collection in (app.warning, app.markdown) for element in collection
    )
    assert "missing_prompt" in rendered
    assert "position=0" in rendered and "parameters.prompt" in rendered

    next(field for field in app.text_input if field.label == "Batch name").set_value(
        "Unsaved name"
    ).run(timeout=10)
    assert next(button for button in app.button if button.label == "Run preflight").disabled
    assert next(button for button in app.button if button.label == "Submit batch").disabled
    assert "Save the current draft" in " ".join(str(item.value) for item in app.info)


def test_editor_submission_indeterminate_reuses_same_intent_without_blind_resubmit():
    class IndeterminateClient(FakeManagementClient):
        def submit(self, batch_id, expected_version, key):
            self._call("submit", batch_id, expected_version, key)
            raise ManagementAPIError(
                "submission_indeterminate",
                "Submission result is uncertain; retry with the same intent.",
                status_code=503,
                retry_same_intent=True,
            )

    fake = IndeterminateClient()
    set_management_client_factory(lambda: fake)
    app = AppTest.from_file(str(page("5")))
    app.session_state["management_batch_id"] = "b1"
    app.run(timeout=10)
    next(button for button in app.button if button.label == "Run preflight").click().run(timeout=10)
    next(box for box in app.checkbox if box.label == "Confirm batch submission").check().run(
        timeout=10
    )
    next(button for button in app.button if button.label == "Submit batch").click().run(timeout=10)
    first = [call for call in fake.calls if call[0] == "submit"][-1]
    next(button for button in app.button if button.label == "Submit batch").click().run(timeout=10)
    second = [call for call in fake.calls if call[0] == "submit"][-1]
    assert first[-1] == second[-1]
    assert not app.exception
    assert "uncertain" in " ".join(str(item.value).lower() for item in app.error)


def test_monitor_uses_explicitly_stale_snapshot_after_temporary_api_failure():
    class StaleClient(FakeManagementClient):
        def __init__(self):
            super().__init__(batch_state="submitted")
            self.fail_reads = False

        def progress(self, batch_id):
            if self.fail_reads:
                raise ManagementAPIError("unreachable", "The management service is unavailable.")
            return super().progress(batch_id)

    fake = StaleClient()
    set_management_client_factory(lambda: fake)
    app = AppTest.from_file(str(page("6")))
    app.session_state["management_batch_id"] = "b1"
    app.run(timeout=10)
    fake.fail_reads = True
    app.run(timeout=10)
    assert not app.exception
    assert "stale durable state" in " ".join(str(item.value).lower() for item in app.warning)
    assert "0 / 1" in [str(item.value) for item in app.metric]


def test_results_render_attempt_lineage_and_isolate_one_managed_output_failure():
    class ResultClient(FakeManagementClient):
        def results(self, batch_id):
            self._call("results", batch_id)
            base = {
                "workflow": "a800_wan22_t2v_33f",
                "effective_priority": "normal",
                "cancel_requested": False,
                "can_retry": False,
                "created_at": "2026-08-02T00:00:00Z",
                "updated_at": "2026-08-02T00:00:01Z",
                "error": None,
            }
            return {
                "batch_id": batch_id,
                "items": [
                    {
                        "item_id": "i1",
                        "position": 0,
                        "current_attempt_no": 2,
                        "attempts": [
                            {
                                **base,
                                "attempt_no": 1,
                                "job_id": "j1",
                                "retry_of_attempt_no": None,
                                "retry_of_job_id": None,
                                "status": "failed",
                                "error": {"code": "remote_failed", "message": "Safe failure."},
                                "outputs": [],
                            },
                            {
                                **base,
                                "attempt_no": 2,
                                "job_id": "j2",
                                "retry_of_attempt_no": 1,
                                "retry_of_job_id": "j1",
                                "status": "succeeded",
                                "outputs": [
                                    {
                                        "asset_id": "bad",
                                        "original_filename": "bad.mp3",
                                        "mime_type": "audio/mpeg",
                                        "size_bytes": 3,
                                        "content_href": "/api/assets/bad/content",
                                        "preview_href": "/api/assets/bad/content",
                                    },
                                    {
                                        "asset_id": "good",
                                        "original_filename": "good.mp3",
                                        "mime_type": "audio/mpeg",
                                        "size_bytes": 6,
                                        "content_href": "/api/assets/good/content",
                                        "preview_href": "/api/assets/good/content",
                                    },
                                ],
                            },
                        ],
                    }
                ],
            }

        def get_content(self, href):
            self._call("get_content", href)
            if "/bad/" in href:
                raise ManagementAPIError("unreachable", "Managed content is unavailable.")
            return b"ID3safe"

    fake = ResultClient(batch_state="submitted")
    set_management_client_factory(lambda: fake)
    app = AppTest.from_file(str(page("7")))
    app.session_state["management_batch_id"] = "b1"
    app.run(timeout=10)
    assert not app.exception
    rendered = " ".join(
        str(element.value)
        for collection in (app.caption, app.markdown, app.info, app.warning, app.error)
        for element in collection
    )
    assert "retry_of_attempt=1" in rendered and "retry_of_job=j1" in rendered
    assert len([call for call in fake.calls if call[0] == "get_content"]) == 2
    assert len(app.get("download_button")) == 1
    assert "unavailable" in rendered.lower()


def test_submitted_editor_is_read_only_but_keeps_monitor_and_results_navigation():
    fake = FakeManagementClient(batch_state="submitted")
    set_management_client_factory(lambda: fake)
    app = AppTest.from_file(str(page("5")))
    app.session_state["management_batch_id"] = "b1"
    app.run(timeout=10)
    assert not app.exception
    assert not app.get("data_editor")
    labels = [button.label for button in app.button]
    assert "Open Monitor" in labels and "Open Results" in labels
    assert "Save draft" not in labels
    assert "read-only" in " ".join(str(item.value).lower() for item in app.info)


@pytest.mark.parametrize("row_count", [0, 100])
def test_editor_apptest_saves_zero_and_one_hundred_row_drafts(row_count):
    rows = [
        {
            "item_id": f"i{index}",
            "position": index,
            "parameter_overrides": {},
            "priority_override": None,
            "assets": [],
        }
        for index in range(row_count)
    ]
    fake = FakeManagementClient(items=rows)
    set_management_client_factory(lambda: fake)
    app = AppTest.from_file(str(page("5")))
    app.session_state["management_batch_id"] = "b1"
    app.run(timeout=10)
    next(button for button in app.button if button.label == "Save draft").click().run(timeout=10)
    saved = [call for call in fake.calls if call[0] == "replace_items"][-1][2]["items"]
    assert len(saved) == row_count


def test_monitor_error_is_redacted_and_does_not_crash_page():
    class FailingClient(FakeManagementClient):
        def progress(self, batch_id):
            self._call("progress", batch_id)
            raise ManagementAPIError("service_error", "A safe management error occurred.")

    fake = FailingClient(batch_state="submitted")
    set_management_client_factory(lambda: fake)
    app = AppTest.from_file(str(page("6")))
    app.session_state["management_batch_id"] = "b1"
    app.run(timeout=10)
    assert not app.exception
    text = " ".join(str(item.value) for item in app.error)
    assert "safe management error" in text.lower()
    assert "token" not in text.lower() and "sql" not in text.lower()
