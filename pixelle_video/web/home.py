"""Phase 09 W1: Streamlit sidebar navigation home page."""

from __future__ import annotations

import streamlit as st

from pixelle_video.web.helpers import (
    API_BASE_URL,
    create_and_preview,
    render_confirm_and_route,
    render_outcome,
    render_plan_preview,
    run_products,
    run_videos,
)

st.set_page_config(
    page_title="AI 媒体平台 · 统一入口",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── session-state init ────────────────────────────────────────────────
if "_request" not in st.session_state:
    st.session_state["_request"] = ""

HOMEPAGE_HINT = "试试说：手工皮具钱包，意大利头层牛皮，需要 Etsy 主图和 TikTok 视频广告"


def _reset_outcome():
    st.session_state.pop("_outcome", None)
    st.session_state.pop("_outcome_intent", None)


# ── sidebar ───────────────────────────────────────────────────────────
st.sidebar.title("🎬 AI 媒体平台")
st.sidebar.caption(f"API：{API_BASE_URL}")

page = st.sidebar.radio(
    "导航",
    ["🏠 首页", "🛍 商品广告", "🎬 短视频", "🎞 长内容/动画"],
    index=0,
)

# ── 首页 ──────────────────────────────────────────────────────────────
if page == "🏠 首页":
    st.title("🎬 AI 媒体平台 · 统一内容入口")
    st.caption(f"后端 API：{API_BASE_URL}")

    request_text = st.text_area(
        "输入一句话需求",
        value=st.session_state["_request"],
        placeholder=HOMEPAGE_HINT,
        height=100,
        key="home_input",
    )
    if st.button("生成方案（04-E）", type="primary", key="home_submit"):
        _reset_outcome()
        if request_text.strip():
            st.session_state["_request"] = request_text.strip()
            with st.spinner("04-E 编排生成方案中…"):
                payload = create_and_preview()
            if payload is not None:
                render_plan_preview(payload)

    plan_id = st.session_state.get("_plan_id")
    intent = st.session_state.get("_intent")
    if plan_id and intent:
        route_fn = run_products if intent == "product_ad" else run_videos
        render_confirm_and_route(route_fn, intent)

    render_outcome()

# ── 商品广告 ───────────────────────────────────────────────────────────
elif page == "🛍 商品广告":
    from pixelle_video.web.pages.products import render

    st.session_state["_request"] = st.session_state.get("_request", "")
    render(_reset_outcome)

# ── 短视频 ─────────────────────────────────────────────────────────────
elif page == "🎬 短视频":
    from pixelle_video.web.pages.videos import render

    st.session_state["_request"] = st.session_state.get("_request", "")
    render(_reset_outcome)

# ── 长内容/动画 ────────────────────────────────────────────────────────
elif page == "🎞 长内容/动画":
    from pixelle_video.web.pages.anime import render

    st.session_state["_request"] = st.session_state.get("_request", "")
    render(_reset_outcome)
