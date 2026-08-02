from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from web.management.runtime import set_management_client_factory


class FakeManagementClient:
    def __init__(self, *, batch_state="draft"):
        self.calls = []
        self.batch_state = batch_state

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
            "items": [
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
