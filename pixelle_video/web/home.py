"""Phase 08 D1: unified user entry page (Streamlit).

One page that chains the existing product-line APIs end to end:

    natural language -> 04-E plan -> preview -> confirm -> engine -> progress -> result

No new API endpoints, no new tables, no new dependencies (httpx is already
part of the project). The FastAPI backend must be running; its base URL is
taken from the API_BASE_URL env var, falling back to http://localhost:8000.
"""

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

CLIENT_TIMEOUT = 120.0  # real DeepSeek takes 30-60 s

st.set_page_config(
    page_title="AI 媒体平台 · 统一入口",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# API helpers (synchronous httpx against the existing FastAPI backend)
# ---------------------------------------------------------------------------


def _api_error(status: int, payload: Any) -> str:
    error = (payload or {}).get("error", {}) if isinstance(payload, dict) else {}
    return error.get("message") or f"HTTP {status}"


def api_post(path: str, json: dict[str, Any] | None = None) -> dict[str, Any] | None:
    try:
        with httpx.Client(base_url=API_BASE_URL, timeout=CLIENT_TIMEOUT) as client:
            response = client.post(path, json=json or {})
        if response.status_code >= 400:
            st.error(f"POST {path}: {_api_error(response.status_code, response.json())}")
            return None
        return response.json()
    except httpx.HTTPError as exc:
        st.error(f"无法连接 API 服务（{API_BASE_URL}）：{exc}")
        return None


def api_get(path: str) -> dict[str, Any] | None:
    try:
        with httpx.Client(base_url=API_BASE_URL, timeout=CLIENT_TIMEOUT) as client:
            response = client.get(path)
        if response.status_code >= 400:
            st.error(f"GET {path}: {_api_error(response.status_code, response.json())}")
            return None
        return response.json()
    except httpx.HTTPError as exc:
        st.error(f"无法连接 API 服务（{API_BASE_URL}）：{exc}")
        return None


# ---------------------------------------------------------------------------
# 04-E: plan creation + preview
# ---------------------------------------------------------------------------


def create_and_generate_plan(request_text: str) -> dict[str, Any] | None:
    created = api_post("/api/orchestration/plans", {"request_text": request_text})
    if created is None:
        return None
    plan_id = created.get("id")
    generated = api_post(f"/api/orchestration/plans/{plan_id}/generate")
    if generated is None:
        return None
    summary = api_get(f"/api/orchestration/plans/{plan_id}/approval-summary")
    return {
        "plan_id": plan_id,
        "intent": created.get("intent", "unknown"),
        "status": created.get("status", "draft"),
        "summary": summary or {},
    }


def render_plan_preview(payload: dict[str, Any]) -> None:
    plan_id = payload["plan_id"]
    summary = payload["summary"]
    st.subheader(f"📋 方案预览（plan {plan_id[:8]}）")
    columns = st.columns(4)
    columns[0].metric("意图", payload["intent"])
    columns[1].metric("状态", payload["status"])
    columns[2].metric("成本估计", f"¥{summary.get('cost_estimate') or 0:.4f}")
    columns[3].metric("评分", summary.get("grade") or "—")
    st.markdown(f"**摘要**：{summary.get('summary') or '（无）'}")
    if summary.get("issues"):
        st.markdown("**问题建议**：")
        for issue in summary["issues"]:
            st.markdown(f"- {issue}")
    st.session_state["plan_id"] = plan_id
    st.session_state["intent"] = payload["intent"]


# ---------------------------------------------------------------------------
# product-line drivers (05 products / 06 videos / 07 anime)
# ---------------------------------------------------------------------------


def run_product_line(plan_id: str, intent: str, progress_bar) -> dict[str, Any] | None:
    if intent == "product_ad":
        return _run_products(plan_id, progress_bar)
    if intent == "short_video":
        return _run_videos(plan_id, progress_bar)
    if intent == "animation":
        return _run_anime(plan_id, progress_bar)
    st.error(f"不支持的意图：{intent}")
    return None


def _poll(
    progress_bar,
    path: str,
    done_keys: tuple[str, ...],
    label: str,
    step_start: float,
    step_end: float,
    timeout_s: float = 300.0,
) -> dict[str, Any] | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        payload = api_get(path)
        if payload is not None:
            done = any(payload.get(key) for key in done_keys)
            percent = payload.get("percent")
            if isinstance(percent, (int, float)):
                shown = step_start + (step_end - step_start) * min(float(percent) / 100.0, 1.0)
                progress_bar.progress(shown)
            st.caption(f"{label}：{payload}")
            if done:
                progress_bar.progress(step_end)
                return payload
        time.sleep(2.0)
    st.error(f"{label} 超时（{timeout_s}s）")
    return None


def _run_products(plan_id: str, progress_bar) -> dict[str, Any] | None:
    progress_bar.progress(0.05, "创建 Brief（05）")
    brief = api_post(f"/api/products/briefs/from-plan/{plan_id}")
    if brief is None:
        return None
    brief_id = brief["brief_id"]
    progress_bar.progress(0.3, "确认 Brief + 启动引擎（05）")
    confirmed = api_post(f"/api/products/briefs/{brief_id}/confirm-from-plan")
    if confirmed is None:
        return None
    results = _poll(
        progress_bar,
        f"/api/products/briefs/{brief_id}/progress",
        ("status",),
        "广告生产进度（05）",
        0.3,
        0.9,
    )
    final = api_get(f"/api/products/briefs/{brief_id}/results")
    if final is not None:
        progress_bar.progress(1.0, "完成")
    return {
        "brief_id": brief_id,
        "confirmed": confirmed,
        "progress": results,
        "results": final,
        "download": f"{API_BASE_URL}/api/products/briefs/{brief_id}/download",
    }


def _run_videos(plan_id: str, progress_bar) -> dict[str, Any] | None:
    progress_bar.progress(0.05, "创建脚本（06）")
    script = api_post(f"/api/videos/scripts/from-plan/{plan_id}")
    if script is None:
        return None
    script_id = script["script_id"]
    progress_bar.progress(0.2, "确认脚本 + 分镜（06）")
    confirmed = api_post(f"/api/videos/scripts/{script_id}/confirm-from-plan")
    if confirmed is None:
        return None
    progress_bar.progress(0.35, "生成素材（06）")
    assets = api_post(f"/api/videos/scripts/{script_id}/generate-assets")
    if assets is None:
        return None
    _poll(
        progress_bar,
        f"/api/videos/scripts/{script_id}/assets/progress",
        ("percent",),
        "素材生成进度（06）",
        0.35,
        0.75,
    )
    progress_bar.progress(0.78, "合成视频（06）")
    composed = api_post(f"/api/videos/scripts/{script_id}/compose")
    if composed is None:
        return None
    _poll(
        progress_bar,
        f"/api/videos/scripts/{script_id}/compose/status",
        ("percent", "status"),
        "合成进度（06）",
        0.78,
        0.95,
    )
    result = api_get(f"/api/videos/scripts/{script_id}/result")
    if result is not None:
        progress_bar.progress(1.0, "完成")
    return {
        "script_id": script_id,
        "confirmed": confirmed,
        "assets": assets,
        "composed": composed,
        "result": result,
        "download": f"{API_BASE_URL}/api/videos/scripts/{script_id}/download",
    }


def _run_anime(plan_id: str, progress_bar) -> dict[str, Any] | None:
    progress_bar.progress(0.1, "生成剧集规划（04-E D2）")
    episode_plan = api_post(f"/api/orchestration/plans/{plan_id}/generate-episode-plan")
    if episode_plan is None:
        return None
    seasons = episode_plan.get("seasons", [])
    progress_bar.progress(0.5, "剧集规划完成")
    st.success(
        f"已生成 {len(seasons)} 季剧集规划（{sum(len(s.get('episodes', [])) for s in seasons)} 集）。"
        "动画镜头生产需在管理端配置项目/剧集/场景后执行。"
    )
    progress_bar.progress(1.0, "规划完成")
    return {
        "plan_id": plan_id,
        "episode_plan": episode_plan,
    }


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------


def main() -> None:
    st.title("🎬 AI 媒体平台 · 统一内容入口")
    st.caption(f"后端 API：{API_BASE_URL}")

    with st.form("request_form"):
        request_text = st.text_area(
            "输入一句话需求",
            placeholder="例：为手工皮具钱包生成 Etsy 主图和 TikTok 广告；"
            "或：做一个 AI 科普短视频；或：做一部仙侠长篇动画剧集",
            height=100,
        )
        submitted = st.form_submit_button("生成方案（04-E）", type="primary")

    if submitted and request_text.strip():
        with st.spinner("04-E 编排生成方案中…"):
            payload = create_and_generate_plan(request_text.strip())
        if payload is not None:
            render_plan_preview(payload)

    plan_id = st.session_state.get("plan_id")
    intent = st.session_state.get("intent")
    if plan_id and intent:
        st.divider()
        st.subheader("🚀 确认并执行")
        st.caption(f"意图：{intent} · plan：{plan_id}")
        if st.button("确认方案，启动产品线引擎", type="primary"):
            progress_bar = st.progress(0.0, "准备中…")
            outcome = run_product_line(plan_id, intent, progress_bar)
            if outcome is not None:
                st.session_state["last_outcome"] = outcome
                st.session_state["last_intent"] = intent

    outcome = st.session_state.get("last_outcome")
    if outcome:
        st.divider()
        st.subheader("✅ 执行结果")
        intent = st.session_state.get("last_intent")
        if intent == "product_ad":
            st.write(f"**Brief**：{outcome['brief_id']}")
            st.write(f"**结果**：{outcome.get('results')}")
            if outcome.get("download"):
                st.markdown(f"[⬇️ 下载交付包]({outcome['download']})")
        elif intent == "short_video":
            st.write(f"**脚本**：{outcome['script_id']}")
            st.write(f"**合成**：{outcome.get('composed')}")
            st.write(f"**结果**：{outcome.get('result')}")
            if outcome.get("download"):
                st.markdown(f"[⬇️ 下载视频]({outcome['download']})")
        elif intent == "animation":
            st.write(f"**剧集规划**：{outcome.get('episode_plan', {}).get('seasons', [])}")


if __name__ == "__main__":
    main()
