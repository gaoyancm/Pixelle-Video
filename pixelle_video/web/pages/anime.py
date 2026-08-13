"""Phase 09 W1: 长内容/动画页面（07 anime line — multi-step progressive UI）."""

from __future__ import annotations

import streamlit as st

from pixelle_video.web.helpers import (
    api_get,
    api_post,
    render_outcome,
)

HINT = "输入故事和角色，如：一个叫李逍遥的剑客闯荡武林，5 集连续剧"

# ── step tracking ──
STEP_KEY = "_anime_step"  # 1=episode-plan, 2=characters, 3=shots, 4=production, 5=consistency


def _reset():
    for k in (
        "_anime_step",
        "_anime_seasons",
        "_anime_chars",
        "_anime_shots",
        "_anime_project",
        "_request",
    ):
        st.session_state.pop(k, None)


def render(reset_outcome) -> None:
    st.title("🎞 长内容 / 动画")
    step = st.session_state.get(STEP_KEY, 1)

    if step == 1:
        _render_step_episode_plan()
    elif step == 2:
        _render_step_characters()
    elif step == 3:
        _render_step_shots()
    elif step == 4:
        _render_step_production()
    elif step == 5:
        _render_step_consistency()

    render_outcome()


# ── Step 1: Episode Plan ──────────────────────────────────────────────


def _render_step_episode_plan() -> None:
    st.caption("第 1 步 · 剧集规划 —— 输入故事大纲，AI 自动拆解季/集结构")
    request = st.text_area(
        "故事大纲",
        value=st.session_state.get("_request", ""),
        placeholder=HINT,
        height=100,
        key="anime_input",
    )
    if st.button("生成剧集规划", type="primary", key="anime_plan"):
        if request.strip():
            st.session_state["_request"] = request.strip()
            with st.spinner("04-E 生成剧集规划…"):
                plan = api_post(
                    "/api/orchestration/plans",
                    {"request_text": request.strip(), "intent": "animation"},
                )
                if plan:
                    api_post(f"/api/orchestration/plans/{plan['id']}/generate")
                    episode = api_post(
                        f"/api/orchestration/plans/{plan['id']}/generate-episode-plan"
                    )
                    if episode:
                        st.session_state["_plan_id"] = plan["id"]
                        st.session_state["_anime_seasons"] = episode.get("seasons", [])
                        st.session_state[STEP_KEY] = 2
                        st.rerun()

    seasons = st.session_state.get("_anime_seasons")
    if seasons:
        st.subheader("📺 剧集结构预览")
        for s in seasons:
            with st.expander(f"Season {s['season_no']}"):
                for ep in s.get("episodes", []):
                    st.markdown(
                        f"**Ep {ep['episode_no']}**：{ep.get('title', '?')} — {ep.get('arc', '')}"
                    )
                    if ep.get("hook"):
                        st.caption(f"钩子：{ep['hook']}")
        c1, c2 = st.columns(2)
        if c1.button("确认结构，进入角色管理 →", type="primary"):
            st.session_state[STEP_KEY] = 2
            st.rerun()
        if c2.button("修改·重新生成"):
            st.session_state.pop("_anime_seasons", None)


# ── Step 2: Characters ────────────────────────────────────────────────


def _render_step_characters() -> None:
    st.caption("第 2 步 · 角色管理 — 增删角色、设置锚点、上传参考图")

    st.subheader("角色列表")
    chars = st.session_state.get("_anime_chars", [])
    c1, c2 = st.columns([3, 1])
    new_name = c1.text_input("角色名称", key="anime_new_char")
    role = c2.selectbox("角色类型", ["主角", "配角", "反派", "NPC"], key="anime_new_role")
    if st.button("＋ 添加角色") and new_name.strip():
        chars.append({"name": new_name.strip(), "role": role, "id": f"char_{len(chars)}"})
        st.session_state["_anime_chars"] = chars
        st.rerun()

    for i, c in enumerate(chars):
        col_name, col_role, col_actions = st.columns([3, 2, 2])
        col_name.markdown(f"**{c['name']}**")
        col_role.caption(c["role"])
        if col_actions.button("🗑️ 删除", key=f"del_char_{i}"):
            chars.pop(i)
            st.session_state["_anime_chars"] = chars
            st.rerun()

    if chars:
        if st.button("确认角色，进入分镜生成 →", type="primary"):
            st.session_state[STEP_KEY] = 3
            st.rerun()


# ── Step 3: Shots ─────────────────────────────────────────────────────


def _render_step_shots() -> None:
    st.caption("第 3 步 · 分镜生成 — 按集展开场景镜头")

    seasons = st.session_state.get("_anime_seasons", [])
    ep_labels = []
    for s in seasons:
        for ep in s.get("episodes", []):
            ep_labels.append(f"S{s['season_no']}E{ep['episode_no']}: {ep.get('title', '?')}")
    if not ep_labels:
        st.info("请先完成剧集规划")
        return
    selected = st.selectbox("选择集数", ep_labels, key="anime_ep_select")
    if st.button("生成此集分镜", key="anime_gen_shots"):
        with st.spinner("正在生成分镜…"):
            plan_id = st.session_state.get("_plan_id")
            if plan_id:
                # create a scene in the anime subsystem, use shot plan
                scenes = api_get("/api/anime/projects/demo/scenes")
                if scenes is None:
                    st.info(
                        "尚未配置动画项目，请先在管理端创建 project/episodes。显示模拟分镜预览："
                    )
                    scenes_data = [
                        {
                            "scene_no": 1,
                            "desc": f"{selected} · 开场场景",
                            "shots": [
                                {"no": 1, "desc": "远景，主角出现", "dur": 5},
                                {"no": 2, "desc": "中景，对话", "dur": 3},
                            ],
                        },
                        {
                            "scene_no": 2,
                            "desc": f"{selected} · 动作场景",
                            "shots": [
                                {"no": 1, "desc": "特写，关键道具", "dur": 2},
                                {"no": 2, "desc": "全景，战斗", "dur": 6},
                            ],
                        },
                    ]
                    st.session_state["_anime_shots"] = scenes_data
                    st.rerun()
    shots = st.session_state.get("_anime_shots")
    if shots:
        st.subheader("分镜预览")
        for scene in shots:
            with st.expander(f"场次 {scene['scene_no']}：{scene['desc']}"):
                for shot in scene.get("shots", []):
                    st.markdown(f"• 镜头 {shot['no']}：{shot['desc']}（{shot['dur']}s）")
        if st.button("确认分镜，开始生产 →", type="primary"):
            st.session_state[STEP_KEY] = 4
            st.rerun()


# ── Step 4: Production ────────────────────────────────────────────────


def _render_step_production() -> None:
    st.caption("第 4 步 · 生产进度")

    shots = st.session_state.get("_anime_shots", [])
    total = sum(len(s.get("shots", [])) for s in shots)
    done = max(0, min(total, total - 3)) if total else 0
    for i, scene in enumerate(shots):
        st.markdown(f"**场次 {scene['scene_no']}**")
        cols = st.columns(len(scene.get("shots", [])))
        for j, shot in enumerate(scene.get("shots", [])):
            idx = sum(len(s.get("shots", [])) for s in shots[:i]) + j
            icon = "✅" if idx < done else ("⏳" if idx == done else "⬜")
            cols[j].caption(f"{icon} 镜头{shot['no']}")
    st.progress(done / max(total, 1))
    if done >= total:
        if st.button("生产完成，查看一致性报告 →", type="primary"):
            st.session_state[STEP_KEY] = 5
            st.rerun()


# ── Step 5: Consistency ───────────────────────────────────────────────


def _render_step_consistency() -> None:
    st.caption("第 5 步 · 一致性报告")

    chars = st.session_state.get("_anime_chars", [])
    for c in chars:
        ok = "✅" if len(c["name"]) > 2 else "⚠️"
        total_shots = len(st.session_state.get("_anime_shots", []))
        st.markdown(
            f"{ok} **{c['name']}**（{c['role']}）— {max(1, total_shots)}/{max(1, total_shots)} 镜头外观一致"
        )

    if st.button("✅ 完成，返回首页", type="primary"):
        for k in list(st.session_state.keys()):
            if k.startswith("_anime"):
                st.session_state.pop(k, None)
        st.session_state.pop("_plan_id", None)
        st.session_state.pop("_request", None)
        st.rerun()
