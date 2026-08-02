"""Streamlit renderers for the five Phase 03 management workbench views."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import streamlit as st

from web.components.header import render_header
from web.state.session import init_i18n, init_session_state

from .client import ManagementAPIError
from .helpers import (
    PRIORITIES,
    STATUS_LABELS,
    attempt_label,
    canonical_fingerprint,
    complete_intent,
    editor_rows,
    group_preflight_issues,
    intent_key,
    media_renderer,
    normalize_item_rows,
    progress_ratio,
    safe_filename,
)
from .runtime import get_management_client

TEXT = {
    "projects": "Projects",
    "batches": "Batches",
    "editor": "Batch Editor",
    "monitor": "Batch Monitor",
    "results": "Results",
    "trusted": "Trusted-network administrator workbench. Application login is not included.",
    "cancel_truth": (
        "Cancellation requests are recorded for later Worker coordination. Remote computation "
        "is not guaranteed to stop immediately; the durable terminal state remains authoritative."
    ),
}


def _setup(title: str) -> Any:
    init_session_state()
    init_i18n()
    render_header()
    st.title(title)
    st.caption(TEXT["trusted"])
    return get_management_client()


def _show_error(error: Exception) -> None:
    if isinstance(error, ManagementAPIError):
        st.error(f"{error.code}: {error.message}")
        if error.retry_same_intent:
            st.warning("Retry confirmation will reuse the same action key.")
        return
    if isinstance(error, (ValueError, json.JSONDecodeError)):
        st.error(str(error))
        return
    st.error("The management workbench could not complete this action.")


def _context_id(name: str) -> str | None:
    query = st.query_params.get(name)
    value = str(query) if query else st.session_state.get(f"management_{name}")
    if value:
        st.session_state[f"management_{name}"] = value
        return value
    return None


def _set_context(**values: str) -> None:
    for name, value in values.items():
        st.session_state[f"management_{name}"] = value
        st.query_params[name] = value


def _go(page: str, **context: str) -> None:
    _set_context(**context)
    st.switch_page(page)


def render_projects() -> None:
    client = _setup(TEXT["projects"])
    include_archived = st.checkbox("Include archived projects", key="projects_include_archived")
    offset = int(st.session_state.get("projects_offset", 0))
    try:
        result = client.list_projects(include_archived=include_archived, limit=20, offset=offset)
    except Exception as error:
        _show_error(error)
        return

    with st.expander("Create project", expanded=not result.get("items")):
        with st.form("create_project"):
            name = st.text_input("Project name")
            description = st.text_area("Description")
            if st.form_submit_button("Create project", type="primary"):
                try:
                    client.create_project({"name": name, "description": description or None})
                    st.success("Project created.")
                    st.rerun()
                except Exception as error:
                    _show_error(error)

    if not result.get("items"):
        st.info("No projects match this view.")
    for project in result.get("items", []):
        with st.container(border=True):
            st.subheader(project["name"])
            st.caption(project.get("description") or "No description")
            st.write("Archived" if project.get("archived") else "Active")
            open_col, edit_col, archive_col = st.columns(3)
            if open_col.button("Open batches", key=f"project_open_{project['project_id']}"):
                _go(
                    "pages/4_📦_Batches.py",
                    project_id=project["project_id"],
                )
            with edit_col.expander("Edit"):
                with st.form(f"project_edit_{project['project_id']}"):
                    edit_name = st.text_input("Name", value=project["name"])
                    edit_description = st.text_area(
                        "Description", value=project.get("description") or ""
                    )
                    if st.form_submit_button("Save project", disabled=project.get("archived")):
                        try:
                            client.update_project(
                                project["project_id"],
                                {"name": edit_name, "description": edit_description or None},
                            )
                            st.success("Project saved.")
                            st.rerun()
                        except Exception as error:
                            _show_error(error)
            with archive_col:
                confirmed = st.checkbox(
                    "Confirm archive",
                    key=f"project_archive_confirm_{project['project_id']}",
                    disabled=project.get("archived"),
                )
                if st.button(
                    "Archive project",
                    key=f"project_archive_{project['project_id']}",
                    disabled=project.get("archived") or not confirmed,
                ):
                    try:
                        client.archive_project(project["project_id"])
                        st.success("Project archived; historical batches remain available.")
                        st.rerun()
                    except Exception as error:
                        _show_error(error)
    prev_col, next_col = st.columns(2)
    if prev_col.button("Previous", disabled=offset == 0):
        st.session_state.projects_offset = max(0, offset - 20)
        st.rerun()
    if next_col.button("Next", disabled=not result.get("has_more")):
        st.session_state.projects_offset = offset + 20
        st.rerun()


def render_batches() -> None:
    client = _setup(TEXT["batches"])
    project_id = _context_id("project_id")
    if not project_id:
        st.warning("Choose a project from Projects first.")
        return
    try:
        project = client.get_project(project_id)
        include_archived = st.checkbox("Include archived batches", key="batches_include_archived")
        result = client.list_batches(
            project_id, include_archived=include_archived, limit=100, offset=0
        )
        catalog = client.workflows().get("items", [])
    except Exception as error:
        _show_error(error)
        return
    st.caption(f"Project: {project['name']} · {project_id}")
    if st.button("Back to Projects"):
        st.switch_page("pages/3_🗂️_Projects.py")

    available = [item for item in catalog if item.get("available")]
    with st.expander("Create batch", expanded=not result.get("items")):
        with st.form("create_batch"):
            name = st.text_input("Batch name")
            workflow = st.selectbox(
                "Workflow",
                options=[item["workflow"] for item in available],
                format_func=lambda value: next(
                    item["display_name"] for item in available if item["workflow"] == value
                ),
            )
            priority = st.selectbox("Default priority", PRIORITIES, index=1)
            if st.form_submit_button("Create batch", type="primary", disabled=not available):
                try:
                    client.create_batch(
                        project_id,
                        {
                            "name": name,
                            "workflow": workflow,
                            "common_parameters": {},
                            "default_priority": priority,
                        },
                    )
                    st.success("Draft batch created.")
                    st.rerun()
                except Exception as error:
                    _show_error(error)

    if not result.get("items"):
        st.info("No batches match this view.")
    for batch in result.get("items", []):
        with st.container(border=True):
            st.subheader(batch["name"])
            st.caption(
                f"{batch['workflow']} · {batch['state']} · version {batch['version']}"
                + (" · archived" if batch.get("archived") else "")
            )
            editor_col, monitor_col, results_col, archive_col = st.columns(4)
            if editor_col.button("Editor", key=f"batch_editor_{batch['batch_id']}"):
                _go(
                    "pages/5_📝_Batch_Editor.py",
                    project_id=project_id,
                    batch_id=batch["batch_id"],
                )
            if monitor_col.button("Monitor", key=f"batch_monitor_{batch['batch_id']}"):
                _go(
                    "pages/6_📊_Batch_Monitor.py",
                    project_id=project_id,
                    batch_id=batch["batch_id"],
                )
            if results_col.button("Results", key=f"batch_results_{batch['batch_id']}"):
                _go(
                    "pages/7_✅_Results.py",
                    project_id=project_id,
                    batch_id=batch["batch_id"],
                )
            with archive_col:
                confirmed = st.checkbox(
                    "Confirm archive",
                    key=f"batch_archive_confirm_{batch['batch_id']}",
                    disabled=batch.get("archived"),
                )
                if st.button(
                    "Archive",
                    key=f"batch_archive_{batch['batch_id']}",
                    disabled=batch.get("archived") or not confirmed,
                ):
                    try:
                        client.archive_batch(batch["batch_id"])
                        st.success("Batch archived; jobs and results remain intact.")
                        st.rerun()
                    except Exception as error:
                        _show_error(error)


def _parameter_editor(workflow: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for parameter in workflow.get("parameters", []):
        name = parameter["name"]
        constraints = parameter.get("constraints") or {}
        field_type = parameter.get("type", "str")
        existing = current.get(name, constraints.get("default"))
        if "int" in field_type or "float" in field_type:
            default = float(existing or constraints.get("minimum") or 0)
            value = st.number_input(
                name,
                value=default,
                min_value=float(constraints["minimum"]) if "minimum" in constraints else None,
                max_value=float(constraints["maximum"]) if "maximum" in constraints else None,
                key=f"common_parameter_{name}",
            )
            values[name] = (
                int(value) if "int" in field_type and "float" not in field_type else value
            )
        else:
            value = st.text_input(
                name,
                value="" if existing is None else str(existing),
                key=f"common_parameter_{name}",
            )
            if value or parameter.get("required"):
                values[name] = value
    return values


def render_editor() -> None:
    client = _setup(TEXT["editor"])
    batch_id = _context_id("batch_id")
    if not batch_id:
        st.warning("Choose a batch from Batches first.")
        return
    try:
        batch = client.get_batch(batch_id)
        workflows = client.workflows().get("items", [])
        assets = client.list_assets(kind="input", state="available", limit=100, offset=0).get(
            "items", []
        )
    except Exception as error:
        _show_error(error)
        return
    workflow = next((item for item in workflows if item["workflow"] == batch["workflow"]), None)
    if workflow is None:
        st.error("The batch workflow is no longer present in the public catalog.")
        return
    st.caption(
        f"Batch: {batch['name']} · {workflow['display_name']} · {batch['state']} · "
        f"version {batch['version']}"
    )
    read_only = batch["state"] != "draft" or batch.get("archived")
    if read_only:
        st.info("This batch is read-only. Use Monitor and Results for durable execution facts.")
        monitor_col, results_col = st.columns(2)
        if monitor_col.button("Open Monitor"):
            _go("pages/6_📊_Batch_Monitor.py", batch_id=batch_id)
        if results_col.button("Open Results"):
            _go("pages/7_✅_Results.py", batch_id=batch_id)
        return

    uploaded = st.file_uploader(
        "Upload managed input assets",
        accept_multiple_files=True,
        key=f"asset_upload_{batch_id}",
    )
    if st.button("Upload selected files", disabled=not uploaded):
        for upload in uploaded or []:
            fingerprint = canonical_fingerprint(
                {"name": upload.name, "type": upload.type, "size": upload.size}
            )
            key = intent_key(
                st.session_state,
                action="asset_upload",
                scope_id=f"{batch_id}:{fingerprint}",
                fingerprint=fingerprint,
            )
            try:
                client.upload_asset(
                    filename=safe_filename(upload.name),
                    mime_type=upload.type or "application/octet-stream",
                    stream=upload.getvalue(),
                    idempotency_key=key,
                )
                complete_intent(
                    st.session_state,
                    action="asset_upload",
                    scope_id=f"{batch_id}:{fingerprint}",
                    key=key,
                )
                st.success(f"Uploaded {safe_filename(upload.name)}")
            except Exception as error:
                st.warning(f"{safe_filename(upload.name)} was not uploaded.")
                _show_error(error)

    st.subheader("Workflow parameters")
    common_parameters = _parameter_editor(workflow, batch.get("common_parameters") or {})
    batch_name = st.text_input("Batch name", value=batch["name"], key=f"batch_name_{batch_id}")
    default_priority = st.selectbox(
        "Default priority",
        PRIORITIES,
        index=PRIORITIES.index(batch["default_priority"]),
        key=f"batch_default_priority_{batch_id}",
    )

    st.subheader("Logical items")
    rows = editor_rows(batch.get("items") or [])
    edited = st.data_editor(
        rows,
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "priority_override": st.column_config.SelectboxColumn(
                "Priority override", options=["", *PRIORITIES]
            ),
            "asset_id": st.column_config.SelectboxColumn(
                "Managed asset", options=["", *[asset["asset_id"] for asset in assets]]
            ),
        },
        key=f"items_editor_{batch_id}_{batch['version']}",
    )
    items_valid = True
    try:
        item_payload = normalize_item_rows(edited, requires_image=workflow["requires_image"])
    except Exception as error:
        item_payload = []
        items_valid = False
        _show_error(error)

    saved_items = batch.get("items") or []
    dirty = canonical_fingerprint(
        {
            "name": batch_name,
            "common_parameters": common_parameters,
            "default_priority": default_priority,
            "items": item_payload,
        }
    ) != canonical_fingerprint(
        {
            "name": batch["name"],
            "common_parameters": batch.get("common_parameters") or {},
            "default_priority": batch["default_priority"],
            "items": saved_items,
        }
    )
    if dirty:
        st.info("Save the current draft before preflight or submission.")

    if st.button("Save draft", type="primary", disabled=not items_valid):
        try:
            updated = client.update_batch(
                batch_id,
                {
                    "name": batch_name,
                    "workflow": batch["workflow"],
                    "common_parameters": common_parameters,
                    "default_priority": default_priority,
                    "expected_batch_version": batch["version"],
                },
            )
            client.replace_items(
                batch_id,
                {"expected_batch_version": updated["version"], "items": item_payload},
            )
            st.success("Draft saved to the management service.")
            st.rerun()
        except Exception as error:
            _show_error(error)

    preflight_col, submit_col = st.columns(2)
    if preflight_col.button("Run preflight", disabled=dirty or not items_valid):
        try:
            result = client.preflight(batch_id, batch["version"])
            st.session_state[f"preflight:{batch_id}"] = result
        except ManagementAPIError as error:
            st.session_state[f"preflight:{batch_id}"] = {
                "valid": False,
                "issues": list(error.issues),
            }
            _show_error(error)
    preflight = st.session_state.get(f"preflight:{batch_id}")
    if preflight:
        if preflight.get("valid"):
            st.success("Preflight passed.")
        for group, issues in group_preflight_issues(preflight.get("issues", [])).items():
            if issues:
                st.warning(f"{group.title()} issues")
                for issue in issues:
                    st.write(
                        f"{issue.get('code')}: {issue.get('message')} · "
                        f"position={issue.get('position')} · field={issue.get('field')}"
                    )
    preflight_valid = bool(
        preflight and preflight.get("valid") and preflight.get("batch_version") == batch["version"]
    )
    confirmed = submit_col.checkbox("Confirm batch submission", key=f"submit_confirm_{batch_id}")
    if submit_col.button(
        "Submit batch", disabled=not confirmed or dirty or not items_valid or not preflight_valid
    ):
        fingerprint = canonical_fingerprint(
            {"batch_id": batch_id, "version": batch["version"], "items": item_payload}
        )
        key = intent_key(
            st.session_state,
            action="submit",
            scope_id=batch_id,
            fingerprint=fingerprint,
        )
        try:
            result = client.submit(batch_id, batch["version"], key)
            complete_intent(st.session_state, action="submit", scope_id=batch_id, key=key)
            st.success(f"Batch submitted as operation {result['operation_id']}.")
            st.rerun()
        except Exception as error:
            _show_error(error)


def render_monitor() -> None:
    client = _setup(TEXT["monitor"])
    batch_id = _context_id("batch_id")
    if not batch_id:
        st.warning("Choose a batch from Batches first.")
        return
    try:
        batch = client.get_batch(batch_id)
    except Exception as error:
        _show_error(error)
        return
    try:
        progress = client.progress(batch_id)
        results = client.results(batch_id)
        st.session_state[f"monitor_snapshot:{batch_id}"] = {
            "progress": progress,
            "results": results,
            "read_at": last_read_caption(),
        }
    except Exception as error:
        cached = st.session_state.get(f"monitor_snapshot:{batch_id}")
        if not cached:
            _show_error(error)
            return
        progress, results = cached["progress"], cached["results"]
        st.warning(f"Showing stale durable state read at {cached['read_at']}.")
        _show_error(error)
    st.caption(f"Batch: {batch['name']} · {batch['workflow']} · {batch['state']}")
    operable = batch["state"] == "submitted" and not batch.get("archived")
    st.metric(
        "Completed logical items", f"{progress['completed_items']} / {progress['total_items']}"
    )
    st.progress(progress_ratio(progress))
    columns = st.columns(4)
    for index, status in enumerate(STATUS_LABELS):
        columns[index % 4].metric(STATUS_LABELS[status], progress["counts"].get(status, 0))
    if progress.get("derived_result"):
        st.success(f"Derived result: {progress['derived_result']}")
    else:
        st.info("Batch execution remains in progress.")
    if st.button("Refresh durable state"):
        st.rerun()

    st.subheader("Priority")
    priority = st.selectbox(
        "New batch default priority", PRIORITIES, index=PRIORITIES.index(batch["default_priority"])
    )
    if st.button("Update batch priority", disabled=not operable):
        try:
            summary = client.update_batch_priority(batch_id, batch["version"], priority)
            st.success(
                "Priority saved: "
                f"updated={summary['updated']}, unchanged={summary['unchanged']}, "
                f"overridden={summary['overridden']}, "
                f"skipped={summary['skipped_claimed_or_nonqueued']}."
            )
            st.rerun()
        except Exception as error:
            _show_error(error)

    for item in batch.get("items") or []:
        attempt = next(
            (entry for entry in results.get("items", []) if entry["item_id"] == item["item_id"]),
            None,
        )
        current = None if not attempt or not attempt["attempts"] else attempt["attempts"][-1]
        with st.expander(f"Item {item['position']} · {item['item_id']}"):
            if current:
                status = (
                    "Cancellation requested"
                    if (
                        current["cancel_requested"]
                        and current["status"]
                        not in {"succeeded", "failed", "timed_out", "cancelled"}
                    )
                    else STATUS_LABELS.get(current["status"], current["status"])
                )
                st.write(f"Current: attempt {current['attempt_no']} · {status}")
            options = ["inherit", *PRIORITIES]
            selected = st.selectbox(
                "Item priority",
                options,
                index=0
                if item.get("priority_override") is None
                else options.index(item["priority_override"]),
                key=f"item_priority_{item['item_id']}",
            )
            if st.button(
                "Save item priority",
                key=f"item_priority_save_{item['item_id']}",
                disabled=not operable,
            ):
                try:
                    client.update_item_priority(
                        item["item_id"],
                        batch["version"],
                        None if selected == "inherit" else selected,
                    )
                    st.success("Item preference saved; only an eligible queued job is changed.")
                    st.rerun()
                except Exception as error:
                    _show_error(error)

    st.subheader("Batch operations")
    cancel_confirmed = st.checkbox("Confirm cancellation request")
    st.caption(TEXT["cancel_truth"])
    if st.button("Request batch cancellation", disabled=not cancel_confirmed or not operable):
        fingerprint = canonical_fingerprint({"batch_id": batch_id, "version": batch["version"]})
        key = intent_key(
            st.session_state, action="cancel", scope_id=batch_id, fingerprint=fingerprint
        )
        try:
            summary = client.cancel(batch_id, batch["version"], key)
            complete_intent(st.session_state, action="cancel", scope_id=batch_id, key=key)
            st.success(
                f"Cancellation: requested={summary['requested']}, "
                f"already_requested={summary['already_requested']}, "
                f"skipped_terminal={summary['skipped_terminal']}, not_found={summary['not_found']}."
            )
            st.rerun()
        except Exception as error:
            _show_error(error)
    retry_confirmed = st.checkbox("Confirm retry of currently eligible failed items")
    if st.button("Retry eligible items", disabled=not retry_confirmed or not operable):
        fingerprint = canonical_fingerprint({"batch_id": batch_id, "version": batch["version"]})
        key = intent_key(
            st.session_state, action="retry-eligible", scope_id=batch_id, fingerprint=fingerprint
        )
        try:
            summary = client.retry_eligible(batch_id, batch["version"], key)
            complete_intent(st.session_state, action="retry-eligible", scope_id=batch_id, key=key)
            st.success(
                f"Retry operation: retried={summary['retried']}, skipped={summary['skipped']}."
            )
            st.rerun()
        except Exception as error:
            _show_error(error)


def render_results() -> None:
    client = _setup(TEXT["results"])
    batch_id = _context_id("batch_id")
    if not batch_id:
        st.warning("Choose a batch from Batches first.")
        return
    try:
        batch = client.get_batch(batch_id)
        result = client.results(batch_id)
    except Exception as error:
        _show_error(error)
        return
    st.caption(f"Batch: {batch['name']} · {batch['workflow']} · {batch['state']}")
    if not result.get("items"):
        st.info("This batch has no logical item results.")
        return
    for item in result["items"]:
        with st.container(border=True):
            st.subheader(f"Item {item['position']} · {item['item_id']}")
            for attempt in item.get("attempts", []):
                with st.expander(
                    attempt_label(attempt, item.get("current_attempt_no")),
                    expanded=attempt["attempt_no"] == item.get("current_attempt_no"),
                ):
                    st.write(
                        f"Workflow: {attempt['workflow']} · Priority: {attempt['effective_priority']}"
                    )
                    st.caption(
                        f"Created {attempt['created_at']} · Updated {attempt['updated_at']} · "
                        f"retry_of_attempt={attempt.get('retry_of_attempt_no')} · "
                        f"retry_of_job={attempt.get('retry_of_job_id')}"
                    )
                    if attempt.get("cancel_requested") and attempt["status"] not in {
                        "succeeded",
                        "failed",
                        "timed_out",
                        "cancelled",
                    }:
                        st.warning(
                            "Cancellation requested; this is not a cancelled terminal state."
                        )
                    if attempt.get("error"):
                        st.error(f"{attempt['error']['code']}: {attempt['error']['message']}")
                    if not attempt.get("outputs"):
                        st.info("This attempt has no managed outputs.")
                    for output in attempt.get("outputs", []):
                        st.write(
                            f"{output['original_filename']} · {output['mime_type']} · "
                            f"{output['size_bytes']} bytes"
                        )
                        try:
                            content = client.get_content(
                                output.get("preview_href") or output["content_href"]
                            )
                            renderer = media_renderer(output["mime_type"])
                            if renderer == "image":
                                st.image(content)
                            elif renderer == "video":
                                st.video(content)
                            elif renderer == "audio":
                                st.audio(content)
                            st.download_button(
                                "Download managed output",
                                data=content,
                                file_name=safe_filename(output["original_filename"]),
                                mime=output["mime_type"],
                                key=f"download_{attempt['job_id']}_{output['asset_id']}",
                            )
                        except Exception as error:
                            st.warning("This output preview or download is currently unavailable.")
                            _show_error(error)


def render_page(name: str) -> None:
    renderers = {
        "projects": render_projects,
        "batches": render_batches,
        "editor": render_editor,
        "monitor": render_monitor,
        "results": render_results,
    }
    renderers[name]()


def last_read_caption() -> str:
    return datetime.now(timezone.utc).isoformat()
