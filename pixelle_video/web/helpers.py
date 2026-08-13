"""Phase 09 W1: full sidebar-navigated Streamlit UI (shared helpers)."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
import streamlit as st


def _default_api_base_url() -> str:
    try:
        return str(st.secrets.get("api_base_url", "http://localhost:8000"))
    except Exception:
        return "http://localhost:8000"


API_BASE_URL = os.environ.get("API_BASE_URL", _default_api_base_url()).rstrip("/")
CLIENT_TIMEOUT = 300.0  # real DeepSeek + full 04-E pipeline can exceed 120s


def _safe_json(response: httpx.Response) -> Any:
    """Parse a response body as JSON, falling back to None on bad payloads."""
    try:
        return response.json()
    except ValueError:
        return None


def api_post(path: str, json: dict[str, Any] | None = None) -> dict[str, Any] | None:
    try:
        with httpx.Client(base_url=API_BASE_URL, timeout=CLIENT_TIMEOUT) as client:
            response = client.post(path, json=json or {})
        payload = _safe_json(response)
        if response.status_code >= 400:
            err = (payload or {}).get("error", {})
            msg = (
                err.get("message", f"HTTP {response.status_code}")
                if isinstance(err, dict)
                else str(err)
            )
            st.error(f"POST {path}: {msg}")
            return None
        return payload if isinstance(payload, dict) else None
    except httpx.HTTPError as exc:
        st.error(f"无法连接 API 服务（{API_BASE_URL}）：{exc}")
        return None


def api_get(path: str) -> dict[str, Any] | None:
    try:
        with httpx.Client(base_url=API_BASE_URL, timeout=CLIENT_TIMEOUT) as client:
            response = client.get(path)
        payload = _safe_json(response)
        if response.status_code >= 400:
            err = (payload or {}).get("error", {})
            msg = (
                err.get("message", f"HTTP {response.status_code}")
                if isinstance(err, dict)
                else str(err)
            )
            st.error(f"GET {path}: {msg}")
            return None
        return payload if isinstance(payload, dict) else None
    except httpx.HTTPError as exc:
        st.error(f"无法连接 API 服务（{API_BASE_URL}）：{exc}")
        return None


def upload_image_file(uploaded) -> str | None:
    """Upload an image to the media-assets API and return its asset id."""
    try:
        with httpx.Client(base_url=API_BASE_URL, timeout=CLIENT_TIMEOUT) as client:
            response = client.post(
                "/api/assets",
                files={
                    "file": (
                        uploaded.name or "upload.png",
                        uploaded.getvalue(),
                        uploaded.type or "application/octet-stream",
                    )
                },
            )
        payload = _safe_json(response)
        if response.status_code >= 400:
            err = (payload or {}).get("error", {})
            msg = (
                err.get("message", f"HTTP {response.status_code}")
                if isinstance(err, dict)
                else str(err)
            )
            st.error(f"图片上传失败：{msg}")
            return None
        return payload.get("asset_id") if isinstance(payload, dict) else None
    except httpx.HTTPError as exc:
        st.error(f"无法连接 API 服务（{API_BASE_URL}）：{exc}")
        return None


def render_reference_image_uploader(key: str) -> None:
    """Render a reference-image uploader; stores the asset id on success."""
    uploaded = st.file_uploader(
        "参考图（可选 · 图生图 / 图生视频）",
        type=["png", "jpg", "jpeg", "webp"],
        key=key,
    )
    if uploaded is not None:
        st.image(uploaded, caption="参考图预览", width=240)
        if st.session_state.get("_reference_image_id") is None:
            with st.spinner("上传参考图…"):
                asset_id = upload_image_file(uploaded)
            if asset_id:
                st.session_state["_reference_image_id"] = asset_id
        st.caption(f"参考图已上传：{st.session_state.get('_reference_image_id', '')[:8]}…")


def poll_progress(
    path: str,
    done_keys: tuple[str, ...],
    label: str,
    step_start: float,
    step_end: float,
    timeout_s: float = 300.0,
) -> dict[str, Any] | None:
    """Poll a progress endpoint with st.progress bar updates."""
    deadline = time.time() + timeout_s
    progress_bar = None
    while time.time() < deadline:
        payload = api_get(path)
        if payload is not None:
            done = any(payload.get(key) for key in done_keys)
            percent = payload.get("percent")
            if isinstance(percent, (int, float)):
                if progress_bar is None:
                    progress_bar = st.progress(step_start, label)
                progress_bar.progress(
                    step_start + (step_end - step_start) * min(float(percent) / 100.0, 1.0)
                )
            st.caption(f"{label}：{payload}")
            if done:
                if progress_bar:
                    progress_bar.progress(step_end)
                return payload
        time.sleep(2.0)
    st.error(f"{label} 超时（{timeout_s}s）")
    return None


def create_and_preview(intent_hint: str | None = None) -> dict[str, Any] | None:
    """Create a plan through 04-E, generate it, and return the approval summary.

    ``intent_hint`` forces the product line (product_ad/short_video/animation)
    for sub-pages that already know their intent; the homepage passes None to
    let 04-E classify from natural language.
    """
    body = {"request_text": st.session_state["_request"]}
    if intent_hint:
        body["intent"] = intent_hint
    created = api_post("/api/orchestration/plans", body)
    if created is None:
        return None
    plan_id = created.get("id")
    generated = api_post(f"/api/orchestration/plans/{plan_id}/generate")
    if generated is None:
        return None
    summary = api_get(f"/api/orchestration/plans/{plan_id}/approval-summary")
    return {
        "plan_id": plan_id,
        "intent": created.get("intent", intent_hint or "unknown"),
        "status": created.get("status", "draft"),
        "summary": summary or {},
    }


def render_plan_preview(payload: dict[str, Any]) -> None:
    """Common plan preview card: metrics + summary + issues."""
    plan_id = payload["plan_id"]
    summary = payload["summary"]
    st.subheader(f"📋 方案预览（plan {plan_id[:8]}）")
    cols = st.columns(4)
    cols[0].metric("意图", payload["intent"])
    cols[1].metric("状态", payload["status"])
    cols[2].metric("成本估计", f"¥{summary.get('cost_estimate') or 0:.4f}")
    cols[3].metric("评分", summary.get("grade") or "—")
    st.markdown(f"**摘要**：{summary.get('summary') or '（无）'}")
    if summary.get("issues"):
        st.markdown("**问题建议**：")
        for issue in summary["issues"]:
            st.markdown(f"- {issue}")
    st.session_state["_plan_id"] = plan_id
    st.session_state["_intent"] = payload["intent"]


def render_confirm_and_route(route_fn, intent: str) -> None:
    """Render the confirm button and run the product-line route."""
    plan_id = st.session_state.get("_plan_id")
    if not plan_id:
        return
    st.divider()
    st.subheader("🚀 确认并执行")
    st.caption(f"意图：{intent} · plan：{plan_id}")
    if st.button("确认方案，启动产品线引擎", type="primary", key="confirm_btn_" + intent):
        progress_bar = st.progress(0.0, "准备中…")
        outcome = route_fn(plan_id, intent, progress_bar)
        if outcome is not None:
            st.session_state["_outcome"] = outcome
            st.session_state["_outcome_intent"] = intent


def render_outcome() -> None:
    """Show the last execution result + download link."""
    outcome = st.session_state.get("_outcome")
    if not outcome:
        return
    st.divider()
    st.subheader("✅ 执行结果")
    intent = st.session_state.get("_outcome_intent", "")
    if intent == "product_ad":
        st.write(f"**Brief**：{outcome.get('brief_id', '?')}")
        st.write(f"**结果**：{outcome.get('results')}")
        if outcome.get("download"):
            st.markdown(f"[⬇️ 下载交付包]({outcome['download']})")
    elif intent == "short_video":
        st.write(f"**脚本**：{outcome.get('script_id', '?')}")
        st.write(f"**结果**：{outcome.get('result')}")
        if outcome.get("download"):
            st.markdown(f"[⬇️ 下载视频]({outcome['download']})")
    elif intent == "animation":
        st.write(f"**剧集规划**：{outcome.get('episode_plan', {}).get('seasons', [])}")


# ── product / video line runners ──────────────────────────────────────


def run_products(plan_id: str, progress_bar) -> dict[str, Any] | None:
    progress_bar.progress(0.05, "创建 Brief（05）")
    ref_id = st.session_state.get("_reference_image_id")
    body = {"reference_images": [ref_id]} if ref_id else None
    brief = api_post(f"/api/products/briefs/from-plan/{plan_id}", body)
    if brief is None:
        return None
    brief_id = brief["brief_id"]
    progress_bar.progress(0.3, "确认 Brief + 启动引擎")
    confirmed = api_post(f"/api/products/briefs/{brief_id}/confirm-from-plan")
    if confirmed is None:
        return None
    poll_progress(
        f"/api/products/briefs/{brief_id}/progress", ("status",), "广告生产进度", 0.3, 0.9
    )
    final = api_get(f"/api/products/briefs/{brief_id}/results")
    progress_bar.progress(1.0, "完成")
    return {
        "brief_id": brief_id,
        "confirmed": confirmed,
        "results": final,
        "download": f"{API_BASE_URL}/api/products/briefs/{brief_id}/download",
    }


def run_videos(plan_id: str, progress_bar) -> dict[str, Any] | None:
    progress_bar.progress(0.05, "创建脚本（06）")
    ref_id = st.session_state.get("_reference_image_id")
    body = {"reference_image_id": ref_id} if ref_id else None
    script = api_post(f"/api/videos/scripts/from-plan/{plan_id}", body)
    if script is None:
        return None
    script_id = script["script_id"]
    progress_bar.progress(0.2, "确认脚本 + 分镜")
    confirmed = api_post(f"/api/videos/scripts/{script_id}/confirm-from-plan")
    if confirmed is None:
        return None
    progress_bar.progress(0.35, "生成素材")
    api_post(f"/api/videos/scripts/{script_id}/generate-assets")
    poll_progress(
        f"/api/videos/scripts/{script_id}/assets/progress", ("percent",), "素材生成进度", 0.35, 0.75
    )
    progress_bar.progress(0.78, "合成视频")
    api_post(f"/api/videos/scripts/{script_id}/compose")
    poll_progress(
        f"/api/videos/scripts/{script_id}/compose/status",
        ("percent", "status"),
        "合成进度",
        0.78,
        0.95,
    )
    result = api_get(f"/api/videos/scripts/{script_id}/result")
    progress_bar.progress(1.0, "完成")
    return {
        "script_id": script_id,
        "confirmed": confirmed,
        "result": result,
        "download": f"{API_BASE_URL}/api/videos/scripts/{script_id}/download",
    }
