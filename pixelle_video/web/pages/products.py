"""Phase 09 W1: 商品广告页面（05 product line）."""

from __future__ import annotations

import streamlit as st

from pixelle_video.web.helpers import (
    create_and_preview,
    render_confirm_and_route,
    render_outcome,
    render_plan_preview,
    run_products,
)

HINT = "输入产品名和平台，如：手工皮具钱包，Etsy + TikTok"


def render(reset_outcome) -> None:
    st.title("🛍 商品广告")
    st.caption("输入产品信息 → AI 生成广告方案 → 自动出图")

    request = st.text_area(
        "产品描述",
        value=st.session_state["_request"],
        placeholder=HINT,
        height=100,
        key="products_input",
    )
    if st.button("生成广告方案", type="primary", key="products_submit"):
        reset_outcome()
        if request.strip():
            st.session_state["_request"] = request.strip()
            with st.spinner("04-E 编排生成广告方案…"):
                payload = create_and_preview()
            if payload is not None:
                render_plan_preview(payload)

    intent = st.session_state.get("_intent")
    if intent == "product_ad":
        render_confirm_and_route(run_products, intent)

    render_outcome()
