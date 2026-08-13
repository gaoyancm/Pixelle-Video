"""Phase 09 W1: 短视频页面（06 video line）."""

from __future__ import annotations

import streamlit as st

from pixelle_video.web.helpers import (
    create_and_preview,
    render_confirm_and_route,
    render_outcome,
    render_plan_preview,
    run_videos,
)

HINT = "输入选题和时长，如：人工智能改变日常生活的 5 种方式，60 秒"


def render(reset_outcome) -> None:
    st.title("🎬 短视频")
    st.caption("输入选题 → AI 生成脚本+分镜 → 自动合成视频")

    request = st.text_area(
        "视频选题",
        value=st.session_state["_request"],
        placeholder=HINT,
        height=100,
        key="videos_input",
    )
    if st.button("生成视频方案", type="primary", key="videos_submit"):
        reset_outcome()
        if request.strip():
            st.session_state["_request"] = request.strip()
            with st.spinner("04-E 编排生成视频方案…"):
                payload = create_and_preview(intent_hint="short_video")
            if payload is not None:
                render_plan_preview(payload)

    intent = st.session_state.get("_intent")
    if intent == "short_video":
        render_confirm_and_route(run_videos, intent)

    render_outcome()
