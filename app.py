"""Streamlit app: SRT → Premiere FCP7 XML — block-based assembly wizard.

Two-phase flow:
  Phase 1 (Setup): pre-fill all technical parameters once
  Phase 2 (Discussion):
    - highlight: LLM proposes content blocks; user picks/reorders
    - sequential: LLM proposes removal presets; user picks/fine-tunes
  Phase 3 (Review & Generate): XML download + verify anchors

Setup is FROZEN after Start. Reset workflow to change.
BYOK Anthropic API key, session-only, never persisted.
"""

import json
import re
from pathlib import Path

import streamlit as st

import srt2xml

st.set_page_config(page_title="SRT → Premiere XML", page_icon="🎬", layout="wide")

# ============================================================================
# State init
# ============================================================================

def reset_workflow():
    keep = {"api_key_input", "llm_model_input"}
    for k in list(st.session_state.keys()):
        if k not in keep:
            del st.session_state[k]
    st.session_state["stage"] = "setup"


for k, v in [
    ("stage", "setup"),
    ("frozen_config", None),
    ("available_blocks", None),
    ("selected_blocks", []),
    ("presets", None),
    ("chosen_preset", None),
    ("remove_keep_flags", None),
    ("regen_blocks", 0),
    ("regen_presets", 0),
]:
    st.session_state.setdefault(k, v)


# ============================================================================
# Sidebar: API key + Reset (always visible)
# ============================================================================

with st.sidebar:
    st.header("🤖 LLM 設定")
    api_key = st.text_input(
        "Anthropic API key",
        type="password",
        key="api_key_input",
        help="僅在此瀏覽器 session 使用,不會儲存。關閉分頁即清除。",
    )
    llm_model = st.selectbox(
        "Model",
        ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
        index=1, key="llm_model_input",
    )
    if api_key:
        st.caption("🔓 已輸入 key(僅本次 session)")
    else:
        st.caption("輸入 key 才能使用 LLM 提案功能")

    st.divider()
    if st.button("🔄 重設流程", use_container_width=True):
        reset_workflow()
        st.rerun()

    if st.session_state.get("frozen_config"):
        st.divider()
        st.subheader("📋 前置設定摘要")
        c = st.session_state["frozen_config"]
        st.caption(
            f"**{c['fps']} {c['df']}** · {c['width']}×{c['height']}\n\n"
            f"**模式**: `{c['mode']}` · 目標長度 `{c['target_duration_seconds']:.0f}s`\n\n"
            f"**鏡位**: {'A+B 雙機' if c['multicam_enabled'] else '僅 A 機'}"
        )


# ============================================================================
# Header
# ============================================================================

st.title("🎬 SRT → Premiere XML 自動剪輯工具")
stage_label = {
    "setup":         "1️⃣ 前置設定",
    "block_builder": "2️⃣ 選擇並排序片段",
    "preset_pick":   "2️⃣ 選擇移除程度",
    "remove_review": "3️⃣ 微調移除清單",
    "review":        "4️⃣ 確認並產出",
}.get(st.session_state["stage"], "?")
st.caption(f"📍 {stage_label}")


# ============================================================================
# LLM helpers
# ============================================================================

def call_claude(api_key, model, system, user_msg, max_tokens=8192):
    try:
        import anthropic
    except ImportError:
        st.error("尚未安裝 `anthropic` 套件")
        st.stop()
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model, max_tokens=max_tokens,
        system=system, messages=[{"role": "user", "content": user_msg}],
    )
    return msg.content[0].text


def extract_json(text):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    return json.loads(text)


SYS_BLOCKS = """You are an SRT-to-Premiere editing assistant for Taiwanese users.

Propose 8-12 content BLOCKS from this SRT. Each block is a self-contained
quotable segment that the user can mix-and-match to assemble a clip.

Vary by:
- Role: cold-open, hook, setup, punchline, callout, transition, close, demo, data
- Length: short (2-5s) AND medium (5-15s)
- Tone: emotional / data / humorous / interactive / contrast

The user will pick which blocks to include AND reorder them. So:
- Include opening-suitable blocks (cold-open, hook).
- Include body-suitable blocks (setup, data, punchline).
- Include closing-suitable blocks (callout, close).
- Distinct blocks should NOT heavily overlap in cue range.

**IMPORTANT: Respond in Traditional Chinese (zh-TW)** for all text fields
(`name`, `selling_point`). Keep `hook_quote` verbatim from the SRT (whatever
language it's in). Keep `role` in English (it's an enum).

Respond with JSON ONLY (no markdown):
{
  "blocks": [
    {
      "name": "<簡短中文名稱>",
      "hook_quote": "<verbatim from SRT>",
      "cue_range": "<consecutive cues, e.g. '82' or '256-261'>",
      "estimated_length_seconds": <int>,
      "role": "<cold-open|hook|setup|punchline|callout|close|demo|data|transition>",
      "suggested_cam": "A|B",
      "selling_point": "<為什麼這段有效,中文說明>"
    }
  ]
}

Camera psychology (use to set suggested_cam):
- A (front wide) = objective context, scene-setting, callouts, conceptual
- B (side close) = emotion, punchlines, intimate moments, audience reactions
"""

SYS_PRESETS = """You are an SRT-to-Premiere editing assistant in sequential clean-cut mode for Taiwanese users.

Propose 3 removal heaviness presets:
1. Conservative — only obvious filler (嗯, 啊, 那個, mic check, false starts)
2. Moderate — conservative + obvious redundancy + dead air
3. Aggressive — moderate + off-topic tangents + Q&A interludes + repetition

For each, list ALL cues to remove with reason and preview text.

**IMPORTANT: Respond in Traditional Chinese (zh-TW)** for `description` and
`reason`. Keep `name` as the English label (Conservative/Moderate/Aggressive).
`preview_text` is verbatim from SRT.

Respond with JSON ONLY:
{
  "presets": [
    {
      "name": "Conservative|Moderate|Aggressive",
      "description": "<中文說明>",
      "estimated_kept_seconds": <int>,
      "remove": [
        {"cues": "<cue or range>", "reason": "<中文移除原因>", "preview_text": "<≤30 chars>"}
      ]
    }
  ]
}

Rules:
- cues must be consecutive and exist in SRT.
- Conservative removes ≤10% of cues, Aggressive ≤30%.
- When in doubt, KEEP.
"""


# ============================================================================
# Stage: Setup (Phase 1)
# ============================================================================

def render_setup():
    st.markdown(
        "填寫所有技術參數並上傳 SRT。按下「開始討論」後這些設定會**鎖定**,"
        "需要修改請使用左側的「🔄 重設流程」。"
    )

    # ----- SRT + mode -----
    st.subheader("📄 SRT 與剪輯模式")
    col1, col2 = st.columns([2, 1])
    with col1:
        uploaded = st.file_uploader("上傳 SRT 檔案", type=["srt"])
    with col2:
        mode = st.radio(
            "剪輯模式", ["highlight", "sequential"],
            help="**highlight** = 短片精華,自由選擇片段並排列順序。\n\n"
                 "**sequential** = 順剪,保留原 SRT 順序、只移除指定片段。",
        )
    purpose = st.text_input(
        "用途 / 目標觀眾(選填)",
        placeholder="例如:募款短片 / 社群短片 / podcast 順剪",
    )

    st.divider()

    # ----- Sequence -----
    st.subheader("⚙️ Sequence 規格")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        fps = st.selectbox("fps",
            ["23.976", "24", "25", "29.97", "30", "50", "59.94", "60"], index=6)
    with c2:
        df = st.selectbox("displayformat", ["NDF", "DF"],
            help="DF(Drop-Frame)只適用於 29.97 / 59.94 fps")
    with c3:
        width = st.number_input("寬度 (px)", min_value=1, value=1920, step=2)
    with c4:
        height = st.number_input("高度 (px)", min_value=1, value=1080, step=2)

    c5, c6, c7, c8 = st.columns(4)
    with c5:
        pixel_aspect = st.selectbox("pixel aspect",
            ["square", "NTSC-601", "PAL-601", "HD"])
    with c6:
        audio_sr = st.selectbox("音訊取樣率 (Hz)",
            [44100, 48000, 96000], index=1)
    with c7:
        audio_depth = st.selectbox("位元深度 (bit)", [16, 24])
    with c8:
        audio_channels = st.number_input("聲道數", 1, 8, 2)

    st.divider()

    # ----- Cameras -----
    st.subheader("🎥 攝影機")
    st.caption(
        "不需要在這裡指定來源檔路徑——匯入 Premiere 後 re-link 即可。"
    )

    fps_int = srt2xml.FPS_PRESETS[fps]["fps_int"]

    # ----- A camera SRT alignment -----
    st.markdown("**📍 逐字稿從 A 機影片的第幾秒開始?**")
    st.caption("若 SRT 是用 Whisper 從這份 A 機原檔自動轉錄(時間戳已對齊),"
               "保持 0 秒 0 frame。\n\n"
               "若 A 機有 pre-roll(例如錄影機提早開機 13 分鐘才開始講話),"
               "輸入講話開始的位置(例如 `828` 秒 + `47` frames)。")
    a1, a2, a3, a4 = st.columns([3, 1, 3, 1])
    with a1:
        a_srt_starts_seconds = st.number_input(
            "A srt starts seconds", min_value=0, value=0, step=1,
            label_visibility="collapsed",
        )
    with a2:
        st.markdown("&nbsp;")
        st.markdown("**秒 (s)**")
    with a3:
        a_srt_starts_frames = st.number_input(
            "A srt starts frames", min_value=0, max_value=fps_int - 1,
            value=0, step=1,
            label_visibility="collapsed",
        )
    with a4:
        st.markdown("&nbsp;")
        st.markdown(f"**frames** (0–{fps_int - 1})")

    multicam_enabled = st.checkbox(
        "➕ 加入 B 機(雙機 multicam)",
        help="highlight 模式 A/B 切換需要。sequential 模式僅支援單機。",
    )

    b_delay_seconds = 0
    b_delay_frames = 0
    if multicam_enabled:
        st.markdown("**⏱️ B 機比 A 機晚多久開機?**")
        bd_s_col, bd_s_unit, bd_f_col, bd_f_unit = st.columns([3, 1, 3, 1])
        with bd_s_col:
            b_delay_seconds = st.number_input(
                "B delay seconds", min_value=0, value=0, step=1,
                label_visibility="collapsed",
            )
        with bd_s_unit:
            st.markdown("&nbsp;")
            st.markdown("**秒 (s)**")
        with bd_f_col:
            b_delay_frames = st.number_input(
                "B delay frames", min_value=0, max_value=fps_int - 1,
                value=0, step=1,
                label_visibility="collapsed",
            )
        with bd_f_unit:
            st.markdown("&nbsp;")
            st.markdown(f"**frames** (0–{fps_int - 1})")
        st.caption("方向:B 比 A **晚**開機(B 的 pre-roll 較短)。"
                   "可從兩機都看得到的拍手 / 揮手動作目測。")

    # Hardcoded placeholder paths — user will re-link in Premiere.
    a_path = "<<RELINK>>"
    b_path = "<<RELINK>>"

    st.divider()

    # ----- Output -----
    st.subheader("⏱️ 輸出設定")

    st.markdown("**目標長度**")
    td_n_col, td_u_col = st.columns([3, 1])
    with td_n_col:
        target_value = st.number_input(
            "target value", value=1.0, min_value=0.0, step=0.5,
            label_visibility="collapsed",
        )
    with td_u_col:
        target_unit_label = st.selectbox(
            "target unit", ["秒 (seconds)", "分鐘 (minutes)", "小時 (hours)"],
            index=1,
            label_visibility="collapsed",
        )
    target_unit = target_unit_label.split(" ")[0]
    target_seconds = target_value * {"秒": 1, "分鐘": 60, "小時": 3600}[target_unit]
    st.caption(f"= {target_seconds:g} 秒")

    st.markdown("**Padding**(每個 cut 邊界外擴的緩衝秒數,避免聽感被切掉)")
    pad_n_col, pad_u_col = st.columns([3, 1])
    with pad_n_col:
        padding = st.number_input(
            "padding value", value=0.5, step=0.1, min_value=0.0,
            label_visibility="collapsed",
        )
    with pad_u_col:
        st.markdown("&nbsp;")
        st.markdown("**秒 (s)**")

    st.divider()

    # ----- Start button -----
    if st.button("▶️ 開始討論", type="primary", use_container_width=True):
        errors = []
        if not uploaded:
            errors.append("請先上傳 SRT 檔案")
        if not api_key:
            errors.append("請在左側輸入 Anthropic API key")
        if df == "DF" and fps not in ("29.97", "59.94"):
            errors.append(f"DF 僅支援 29.97 / 59.94 fps,目前選的是 {fps}")
        if errors:
            for e in errors:
                st.error(e)
            return

        try:
            srt_text = uploaded.read().decode("utf-8")
            cues = srt2xml.parse_srt(srt_text)
        except Exception as e:
            st.error(f"SRT 解析失敗:{e}")
            return

        st.session_state["frozen_config"] = {
            "uploaded_name": uploaded.name,
            "srt_text": srt_text,
            "cues": cues,
            "mode": mode,
            "purpose": purpose,
            "fps": fps, "df": df,
            "width": int(width), "height": int(height),
            "pixel_aspect": pixel_aspect,
            "audio_sr": int(audio_sr),
            "audio_depth": int(audio_depth),
            "audio_channels": int(audio_channels),
            "a_path": a_path,
            "a_srt_starts_at": (
                float(a_srt_starts_seconds) + float(a_srt_starts_frames) / fps_int
            ),
            "multicam_enabled": multicam_enabled and mode == "highlight",
            "b_path": b_path,
            "b_delay_seconds": int(b_delay_seconds),
            "b_delay_frames": int(b_delay_frames),
            "target_duration_seconds": float(target_seconds),
            "padding": float(padding),
        }
        st.session_state["stage"] = (
            "block_builder" if mode == "highlight" else "preset_pick"
        )
        st.rerun()


# ============================================================================
# Stage: block builder (highlight)
# ============================================================================

def fetch_blocks(prev=None):
    c = st.session_state["frozen_config"]
    user_msg = (
        f"Target length: {c['target_duration_seconds']:.0f} seconds\n"
        f"Purpose: {c['purpose'] or '(unspecified)'}\n"
        f"Camera availability: {'A+B (multicam)' if c['multicam_enabled'] else 'A only'}\n\n"
        f"SRT:\n{c['srt_text']}"
    )
    if prev:
        names = ", ".join(b.get("name", "?") for b in prev)
        user_msg += (f"\n\nPreviously proposed blocks: {names}. "
                     f"Propose DIFFERENT blocks (different cue ranges and angles).")
    return extract_json(call_claude(api_key, llm_model, SYS_BLOCKS, user_msg)).get("blocks", [])


def block_key(b):
    return f"{b.get('cue_range', '?')}::{b.get('name', '?')}"


def render_block_builder():
    c = st.session_state["frozen_config"]

    if st.session_state["available_blocks"] is None:
        with st.spinner("🤖 正在從 SRT 產生內容片段..."):
            try:
                st.session_state["available_blocks"] = fetch_blocks()
            except Exception as e:
                st.error(f"LLM 呼叫失敗:{e}")
                if st.button("↻ 重試"):
                    st.session_state["available_blocks"] = None; st.rerun()
                return

    available = st.session_state["available_blocks"]
    selected = st.session_state["selected_blocks"]
    selected_keys = {block_key(b) for b in selected}

    left, right = st.columns([1, 1])

    # --- Available blocks ---
    with left:
        st.markdown("### 📚 可用片段")
        st.caption(f"LLM 提案了 {len(available)} 個。點 ➕ 加入右側剪輯。")
        for i, b in enumerate(available):
            already = block_key(b) in selected_keys
            with st.container(border=True):
                row = st.columns([4, 1])
                with row[0]:
                    st.markdown(
                        f"**{b.get('name', f'片段 {i+1}')}** "
                        f"`[{b.get('role', '')}]` "
                        f"~{b.get('estimated_length_seconds', '?')}s"
                    )
                    st.markdown(f"💬 *“{b.get('hook_quote', '')}”*")
                    st.caption(
                        f"cue 範圍 `{b.get('cue_range')}` · "
                        f"建議鏡位 `{b.get('suggested_cam', 'A')}` · "
                        f"{b.get('selling_point', '')}"
                    )
                with row[1]:
                    if already:
                        st.button("✓ 已加入", key=f"add_{i}",
                            disabled=True, use_container_width=True)
                    else:
                        if st.button("➕ 加入", key=f"add_{i}",
                            type="primary", use_container_width=True):
                            new_block = dict(b)
                            new_block["cam"] = (
                                b.get("suggested_cam", "A")
                                if c["multicam_enabled"] else "A"
                            )
                            selected.append(new_block)
                            st.rerun()

        st.divider()
        if st.button("↻ 全部重新生成片段(不滿意這些)",
                     type="secondary", use_container_width=True):
            with st.spinner("正在用不同角度重新生成..."):
                try:
                    st.session_state["available_blocks"] = fetch_blocks(prev=available)
                    st.session_state["regen_blocks"] += 1
                except Exception as e:
                    st.error(f"重新生成失敗:{e}")
            st.rerun()

    # --- Selected blocks (ordered) ---
    with right:
        total_secs = sum(b.get("estimated_length_seconds", 0) for b in selected)
        st.markdown(
            f"### 🎬 你的剪輯 · {len(selected)} 段 · ~{total_secs} 秒"
        )
        target_sec = c.get("target_duration_seconds") or None
        if target_sec:
            diff = total_secs - target_sec
            tag = "🎯" if abs(diff) < 5 else ("⚠️" if diff > 0 else "ℹ️")
            st.caption(f"{tag} 目標 {target_sec:.0f}s · "
                       f"差距 {diff:+.1f}s")

        if not selected:
            st.info("還沒選任何片段——從左側「📚 可用片段」按 ➕ 加入。")
        else:
            for i, b in enumerate(selected):
                with st.container(border=True):
                    head = st.columns([0.4, 3.5, 0.7, 0.4, 0.4, 0.4])
                    head[0].markdown(f"**{i+1}**")
                    head[1].markdown(
                        f"**{b.get('name')}** `[{b.get('role', '')}]` · "
                        f"~{b.get('estimated_length_seconds', '?')}s\n\n"
                        f"💬 *{b.get('hook_quote', '')[:50]}*"
                    )
                    if c["multicam_enabled"]:
                        new_cam = head[2].selectbox(
                            "cam", ["A", "B"],
                            index=(0 if b.get("cam", "A") == "A" else 1),
                            key=f"cam_{i}", label_visibility="collapsed",
                        )
                        b["cam"] = new_cam
                    else:
                        head[2].markdown("`A`")

                    if head[3].button("⬆️", key=f"up_{i}", disabled=(i == 0),
                                       help="上移"):
                        selected[i], selected[i-1] = selected[i-1], selected[i]
                        st.rerun()
                    if head[4].button("⬇️", key=f"dn_{i}",
                                       disabled=(i == len(selected) - 1),
                                       help="下移"):
                        selected[i], selected[i+1] = selected[i+1], selected[i]
                        st.rerun()
                    if head[5].button("🗑️", key=f"del_{i}", help="移除"):
                        selected.pop(i)
                        st.rerun()

            st.caption("提示:目前不支援拖拉,請用 ⬆️⬇️ 調整順序。")

        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            if st.button("← 回到前置設定", use_container_width=True):
                reset_workflow(); st.rerun()
        with col2:
            ok = len(selected) > 0
            if st.button("→ 進入確認步驟", type="primary",
                         use_container_width=True, disabled=not ok):
                st.session_state["stage"] = "review"
                st.rerun()


# ============================================================================
# Stage: preset pick (sequential)
# ============================================================================

def fetch_presets(prev=None):
    c = st.session_state["frozen_config"]
    user_msg = (
        f"Target length: {c['target_duration_seconds']:.0f} seconds\n"
        f"Purpose: {c['purpose'] or '(unspecified)'}\n\n"
        f"SRT:\n{c['srt_text']}"
    )
    if prev:
        user_msg += "\n\nPropose DIFFERENT removal sets than before."
    return extract_json(call_claude(api_key, llm_model, SYS_PRESETS, user_msg)).get("presets", [])


def render_preset_pick():
    st.markdown("### 選擇移除程度")
    st.caption("Sequential 模式保持 SRT 原順序,只移除指定的 cue。")

    if st.session_state["presets"] is None:
        with st.spinner("🤖 正在掃描 SRT 找移除候選..."):
            try:
                st.session_state["presets"] = fetch_presets()
            except Exception as e:
                st.error(f"LLM 呼叫失敗:{e}")
                if st.button("↻ 重試"):
                    st.session_state["presets"] = None; st.rerun()
                return

    presets = st.session_state["presets"]
    if not presets:
        st.warning("沒有回傳任何 preset")
        if st.button("↻ 重試"):
            st.session_state["presets"] = None; st.rerun()
        return

    for i, p in enumerate(presets):
        with st.container(border=True):
            head_l, head_r = st.columns([3, 1])
            with head_l:
                st.markdown(
                    f"**{p.get('name', f'Preset {i+1}')}** · "
                    f"移除 {len(p.get('remove', []))} 段 · "
                    f"預估保留 ~{p.get('estimated_kept_seconds', '?')}s"
                )
                st.caption(p.get("description", ""))
                rows = [{"cues": r.get("cues"),
                         "原因": r.get("reason"),
                         "預覽": r.get("preview_text", "")[:40]}
                        for r in p.get("remove", [])]
                st.dataframe(rows, use_container_width=True, hide_index=True,
                             height=min(35 * (len(rows) + 1) + 10, 280))
            with head_r:
                if st.button("✓ 用這個", type="primary",
                             key=f"pick_p_{i}", use_container_width=True):
                    st.session_state["chosen_preset"] = p
                    st.session_state["remove_keep_flags"] = [True] * len(p.get("remove", []))
                    st.session_state["stage"] = "remove_review"
                    st.rerun()

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        if st.button("← 回到前置設定", use_container_width=True):
            reset_workflow(); st.rerun()
    with col2:
        if st.button("↻ 全部重新生成 presets",
                     type="secondary", use_container_width=True):
            with st.spinner("重新產生..."):
                try:
                    st.session_state["presets"] = fetch_presets(prev=presets)
                    st.session_state["regen_presets"] += 1
                except Exception as e:
                    st.error(f"重新生成失敗:{e}")
            st.rerun()


def render_remove_review():
    p = st.session_state["chosen_preset"]
    st.markdown(f"### 微調移除清單 · *{p.get('name')}*")
    st.caption("取消勾選 = 不要移除這個 cue(保留它)。")

    flags = st.session_state["remove_keep_flags"]
    new_flags = []
    for i, r in enumerate(p.get("remove", [])):
        label = (f"`{r.get('cues')}` · {r.get('reason', '')} "
                 f"— *{r.get('preview_text', '')[:40]}*")
        new_flags.append(st.checkbox(label, value=flags[i], key=f"rmflag_{i}"))
    st.session_state["remove_keep_flags"] = new_flags
    st.caption(f"目前生效的移除:**{sum(new_flags)} / {len(new_flags)}**")

    st.divider()
    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("← 換一個 preset", use_container_width=True):
            st.session_state["stage"] = "preset_pick"
            st.rerun()
    with col2:
        if st.button("↻ 全部重新生成 presets",
                     type="secondary", use_container_width=True):
            st.session_state["presets"] = None
            st.session_state["chosen_preset"] = None
            st.session_state["stage"] = "preset_pick"
            st.rerun()
    with col3:
        if st.button("✓ 確認", type="primary", use_container_width=True):
            st.session_state["stage"] = "review"
            st.rerun()


# ============================================================================
# Stage: review + generate
# ============================================================================

def render_review():
    c = st.session_state["frozen_config"]
    mode = c["mode"]

    st.markdown("### 最終確認")

    if mode == "highlight":
        selected = st.session_state["selected_blocks"]
        if not selected:
            st.error("還沒選任何片段,請回上一步。")
            if st.button("← 返回"):
                st.session_state["stage"] = "block_builder"; st.rerun()
            return
        rows = [{
            "#": i,
            "鏡位": b.get("cam", "A"),
            "cues": b.get("cue_range"),
            "role": b.get("role", ""),
            "片段名稱": b.get("name", ""),
            "預估長度(秒)": b.get("estimated_length_seconds", "?"),
        } for i, b in enumerate(selected)]
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        p = st.session_state["chosen_preset"]
        flags = st.session_state["remove_keep_flags"]
        kept = [r for r, k in zip(p.get("remove", []), flags) if k]
        st.markdown(f"**移除程度**: {p.get('name')}  ·  "
                    f"**生效中的移除**: {len(kept)} / {len(p.get('remove', []))}")
        rows = [{"cues": r.get("cues"),
                 "原因": r.get("reason"),
                 "預覽": r.get("preview_text", "")[:40]}
                for r in kept]
        st.dataframe(rows, use_container_width=True, hide_index=True)

    col1, col2 = st.columns([1, 2])
    with col1:
        if st.button("← 返回", use_container_width=True):
            st.session_state["stage"] = (
                "block_builder" if mode == "highlight" else "remove_review"
            )
            st.rerun()
    with col2:
        if st.button("⚙️ 產出 XML", type="primary", use_container_width=True):
            generate_xml()


def build_spec():
    c = st.session_state["frozen_config"]
    b_delay_str = f"{c['b_delay_seconds']}s{c['b_delay_frames']}f"
    spec = {
        "sequence": {
            "name": Path(c["uploaded_name"]).stem or "Edit",
            "fps": c["fps"], "displayformat": c["df"],
            "width": c["width"], "height": c["height"],
            "pixel_aspect": c["pixel_aspect"],
            "audio": {"sample_rate": c["audio_sr"], "depth": c["audio_depth"],
                      "channels": c["audio_channels"]},
        },
        "cameras": {
            "A": {
                "file": "A_camera",
                "path": c["a_path"],
                "srt_starts_at_source_seconds": c["a_srt_starts_at"],
            },
        },
        "settings": {
            "padding": c["padding"],
            "multicam": c["multicam_enabled"],
            "target_duration": c["target_duration_seconds"] or None,
        },
        "mode": c["mode"],
    }
    if c["multicam_enabled"]:
        spec["cameras"]["B"] = {
            "file": "B_camera",
            "path": c["b_path"], "delay_from_a": b_delay_str,
        }
    if c["mode"] == "highlight":
        spec["cuts"] = [{
            "cam": b.get("cam", "A"),
            "cues": b.get("cue_range"),
            "role": b.get("role", ""),
            "label": b.get("name", ""),
        } for b in st.session_state["selected_blocks"]]
    else:
        flags = st.session_state["remove_keep_flags"]
        p = st.session_state["chosen_preset"]
        spec["default_cam"] = "A"
        spec["remove"] = [r for r, k in zip(p.get("remove", []), flags) if k]
    return spec


def generate_xml():
    c = st.session_state["frozen_config"]
    spec = build_spec()
    cues = c["cues"]
    try:
        srt2xml.validate_spec(spec)
        fps_preset = srt2xml.get_fps_preset(spec["sequence"]["fps"])
        timebase = fps_preset["timebase"]
        cam_offsets, anchor_cam = srt2xml.compute_offsets(spec["cameras"], fps_preset, c["df"])
        if spec["mode"] == "sequential":
            cut_specs = srt2xml.compute_sequential_cuts(cues, spec.get("remove", []), "A")
        else:
            cut_specs = spec["cuts"]
        if not cut_specs:
            st.error("套用設定後沒有產生任何 cut")
            return
        cuts = srt2xml.expand_cuts(cut_specs, cues, c["padding"])
        total_frames = srt2xml.compute_frames(cuts, cam_offsets, timebase)
        xml_text = srt2xml.emit_xml(spec, cuts, total_frames, cam_offsets)
    except SystemExit as e:
        st.error(f"產出失敗:{e}"); return
    except Exception as e:
        st.error(f"未預期錯誤:{e}"); return

    duration_sec = total_frames / timebase
    target_sec = c.get("target_duration_seconds") or None

    st.success(f"✅ 產出完成 — {len(cuts)} 個 cut · {duration_sec:.2f} 秒")

    cols = st.columns(3)
    cols[0].metric("Cuts", len(cuts))
    cols[1].metric("總長度", f"{duration_sec:.2f}s",
        delta=(f"{duration_sec - target_sec:+.1f}s vs 目標"
               if target_sec else None))
    cols[2].metric("Frames", total_frames)

    out_name = f"{Path(c['uploaded_name']).stem}.xml"
    st.download_button("⬇️ 下載 XML", xml_text, file_name=out_name,
        mime="application/xml", use_container_width=True)

    with st.expander("🎯 驗證錨點(請在 Premiere 中 scrub 確認)", expanded=True):
        analysis = srt2xml.make_analysis(
            cuts, cues, cam_offsets, anchor_cam, fps_preset, total_frames
        )
        for cut in analysis["cuts"]:
            cams_at_mid = cut["mid"]["cameras"]
            st.markdown(
                f"**Cut {cut['index']} [{cut['cam']}]** {cut.get('label') or ''} · "
                f"timeline {cut['timeline_in']}-{cut['timeline_out']} "
                f"({cut['duration_seconds']}s)"
            )
            st.caption(
                f"SRT 中段:{cut['mid']['srt_seconds']}s "
                f"(cue {cut['mid']['cue_at_mid']}) · "
                + " · ".join(f"{cid}: frame {info['mid']}"
                             for cid, info in cams_at_mid.items())
            )
            st.code(f'預期內容:"{cut["mid"]["expected_text"]}"')
        if spec["settings"]["multicam"]:
            st.info("🔄 Multicam 同步檢查:在某個 cut 的 timeline 中段,"
                    "把 V1/V2 enable 各 toggle 一次,兩軌應該都在播相同內容。")

    with st.expander("🔍 完整 spec(除錯用)"):
        st.code(json.dumps(spec, ensure_ascii=False, indent=2), language="json")


# ============================================================================
# Dispatch
# ============================================================================

stage = st.session_state["stage"]
if stage == "setup":
    render_setup()
elif stage == "block_builder":
    render_block_builder()
elif stage == "preset_pick":
    render_preset_pick()
elif stage == "remove_review":
    render_remove_review()
elif stage == "review":
    render_review()
