"""Phase 07 anime line — real production pipeline UI.

Binds the 07 backend endpoints (projects/episodes/scenes/shots/plan/generate/
progress/consistency) directly. No demo project, no fabricated storyboards, no
computed progress, no name-length "consistency" — every step reads real API
state and every failure surfaces explicitly.
"""

from __future__ import annotations

import streamlit as st

from pixelle_video.web.helpers import (
    api_get,
    api_post,
    render_outcome,
    upload_image_file,
)

HINT = "输入故事和角色，如：一个叫李逍遥的剑客闯荡武林，5 集连续剧"

# step tracking
STEP_KEY = "_anime_step"  # 1=episode-plan, 2=project+characters, 3=episodes+shots, 4=production, 5=consistency


def _reset():
    for k in list(st.session_state.keys()):
        if k.startswith("_anime"):
            st.session_state.pop(k, None)
    st.session_state.pop("_plan_id", None)
    st.session_state.pop("_request", None)


def render(reset_outcome) -> None:
    st.title("🎞 长内容 / 动画")
    step = st.session_state.get(STEP_KEY, 1)

    if step == 1:
        _render_step_episode_plan()
    elif step == 2:
        _render_step_project_characters()
    elif step == 3:
        _render_step_episodes_shots()
    elif step == 4:
        _render_step_production()
    elif step == 5:
        _render_step_consistency()

    render_outcome()


# ── Step 1: Episode Plan (04-E orchestration, real) ─────────────────────────


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
                else:
                    st.error("剧集规划失败：请检查编排服务是否可用。")

    seasons = st.session_state.get("_anime_seasons")
    if seasons:
        st.subheader("📺 剧集结构预览")
        for s in seasons:
            with st.expander(f"Season {s['season_no']}"):
                for ep in s.get("episodes", []):
                    st.markdown(
                        f"**Ep {ep['episode_no']}**：{ep.get('title', '?')} — {ep.get('arc', '')}"
                    )
        c1, c2 = st.columns(2)
        if c1.button("确认结构，进入项目与角色 →", type="primary"):
            st.session_state[STEP_KEY] = 2
            st.rerun()
        if c2.button("修改·重新生成"):
            st.session_state.pop("_anime_seasons", None)


# ── Step 2: Real anime project + characters ─────────────────────────────────


def _ensure_anime_project() -> str | None:
    project_id = st.session_state.get("_anime_project_id")
    if project_id:
        return project_id
    created = api_post(
        "/api/anime/projects",
        {
            "project_id": st.session_state.get("_plan_id") or "anime-project",
            "world_setting": st.session_state.get("_request", "")[:200],
            "style_profile": "动画",
        },
    )
    if not created:
        st.error("创建动画项目失败。")
        return None
    st.session_state["_anime_project_id"] = created["id"]
    return created["id"]


def _render_step_project_characters() -> None:
    st.caption("第 2 步 · 项目与角色 —— 创建真实 AnimeProject 与角色")
    anime_project_id = _ensure_anime_project()
    if not anime_project_id:
        return
    st.caption(f"动画项目：{anime_project_id}")

    st.subheader("角色列表")
    chars = st.session_state.get("_anime_chars", [])
    c1, c2 = st.columns([3, 1])
    new_name = c1.text_input("角色名称", key="anime_new_char")
    role = c2.selectbox("角色类型", ["主角", "配角", "反派", "NPC"], key="anime_new_role")
    ref_file = st.file_uploader(
        "角色参考图（可选）", type=["png", "jpg", "jpeg", "webp"], key="anime_new_ref"
    )
    if st.button("＋ 添加角色") and new_name.strip():
        asset_id = upload_image_file(ref_file) if ref_file is not None else None
        created = api_post(
            "/api/anime/characters",
            {
                "project_id": anime_project_id,
                "name": new_name.strip(),
                "role_type": role,
                "description": new_name.strip(),
                "reference_images": [asset_id] if asset_id else None,
            },
        )
        if created:
            chars.append({"name": created["name"], "role": created["role_type"], "id": created["id"]})
            st.session_state["_anime_chars"] = chars
            st.rerun()
        else:
            st.error("创建角色失败。")

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


# ── Step 3: Real episodes + scenes + shots ───────────────────────────────────


def _render_step_episodes_shots() -> None:
    st.caption("第 3 步 · 分镜生成 —— 创建真实集/场景/镜头并规划")
    anime_project_id = st.session_state.get("_anime_project_id")
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
        with st.spinner("正在创建集/场景并规划分镜…"):
            # parse season/episode numbers from the label
            season_no = int(selected.split("S")[1].split("E")[0])
            episode_no = int(selected.split("E")[1].split(":")[0])
            episode = api_post(
                "/api/anime/episodes",
                {
                    "anime_project_id": anime_project_id,
                    "season_no": season_no,
                    "episode_no": episode_no,
                    "title": selected.split(": ", 1)[-1],
                    "script_summary": selected,
                },
            )
            if not episode:
                st.error("创建剧集失败。")
                st.stop()
            st.session_state["_anime_episode_id"] = episode["id"]
            # create 2 scenes
            scene_ids = []
            for scene_no in (1, 2):
                scene = api_post(
                    f"/api/anime/episodes/{episode['id']}/scenes",
                    {"scene_no": scene_no, "description": f"第 {scene_no} 场"},
                )
                if scene:
                    scene_ids.append(scene["id"])
            st.session_state["_anime_scene_ids"] = scene_ids
            # plan shots per scene
            all_shots = []
            for scene_id in scene_ids:
                planned = api_post(f"/api/anime/scenes/{scene_id}/plan")
                if planned:
                    all_shots.append(
                        {"scene_id": scene_id, "shots": planned.get("shots", [])}
                    )
            st.session_state["_anime_shots"] = all_shots
            st.rerun()

    shots = st.session_state.get("_anime_shots")
    if shots:
        st.subheader("分镜预览（真实后端规划）")
        for entry in shots:
            with st.expander(f"场景 {entry['scene_id']}"):
                for shot in entry.get("shots", []):
                    shot_no = shot.get("shot_no") or shot.get("id", "?")
                    st.markdown(f"• 镜头 {shot_no}：{shot.get('visual_description', '')}")
        if st.button("确认分镜，开始生产 →", type="primary"):
            st.session_state[STEP_KEY] = 4
            st.rerun()


# ── Step 4: Real production + progress polling ───────────────────────────────


def _render_step_production() -> None:
    st.caption("第 4 步 · 生产进度（轮询真实任务状态）")
    scene_ids = st.session_state.get("_anime_scene_ids", [])
    if not scene_ids:
        st.info("请先生成分镜")
        return

    if st.button("开始生产", key="anime_start_prod", type="primary"):
        for scene_id in scene_ids:
            api_post(f"/api/anime/scenes/{scene_id}/generate")
        st.session_state["_anime_prod_started"] = True

    if not st.session_state.get("_anime_prod_started"):
        return

    total = 0
    completed = 0
    failed = 0
    for scene_id in scene_ids:
        progress = api_get(f"/api/anime/scenes/{scene_id}/progress")
        if progress:
            states = progress.get("states", {})
            total += progress.get("total", 0)
            completed += states.get("succeeded", 0)
            failed += states.get("failed", 0)
    st.progress(completed / max(total, 1))
    st.markdown(f"完成 {completed}/{total}，失败 {failed}")

    if total and completed + failed >= total:
        if st.button("生产完成，查看一致性报告 →", type="primary"):
            st.session_state[STEP_KEY] = 5
            st.rerun()


# ── Step 5: Real consistency report ──────────────────────────────────────────


def _render_step_consistency() -> None:
    st.caption("第 5 步 · 一致性报告（真实后端 QC）")
    episode_id = st.session_state.get("_anime_episode_id")
    if not episode_id:
        st.info("请先生成剧集")
        return

    report = api_get(f"/api/anime/episodes/{episode_id}/consistency-report")
    if report is None:
        st.error("一致性报告读取失败。")
        return
    counts = report.get("shot_counts", {})
    st.json(counts)
    if report.get("consistent"):
        st.success("全部镜头已成功，剧集具备交付条件。")
    else:
        st.warning("仍有未完成或失败镜头，暂不能交付。")

    if report.get("ready") and st.button("生成动漫交付包", type="primary"):
        packaged = api_post(f"/api/anime/episodes/{episode_id}/package")
        if packaged:
            st.session_state["_anime_packaged"] = True
            st.success("交付包已生成。")
    if st.session_state.get("_anime_packaged"):
        st.link_button(
            "下载动漫交付包",
            f"http://127.0.0.1:8000/api/anime/episodes/{episode_id}/download",
        )

    if st.button("✅ 完成，返回首页", type="primary"):
        _reset()
        st.rerun()
