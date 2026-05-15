"""Streamlit app: SRT → Premiere FCP7 XML — block-based assembly wizard.

Flow:
  1. mode_select       — pick highlight or sequential (big cards)
  2a. setup_highlight  → theme_pick → block_builder → review (highlight)
  2b. setup_sequential → preset_pick → remove_review → review (sequential)

Highlight block_builder:
- LLM produces categorized blocks (by narrative role in the OUTPUT clip,
  not source position) + a recommended initial assembly.
- User drags to reorder & cross-pool. Regenerate updates only the unselected
  pool, keeping the user's picks intact.

BYOK Anthropic API key, session-only, never persisted.
"""

import json
import re
import uuid
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

import srt2xml

# Custom drag-drop component (frontend/index.html)
_FRONTEND_DIR = Path(__file__).parent / "frontend"
_block_sorter_component = components.declare_component(
    "block_sorter", path=str(_FRONTEND_DIR)
)


def block_sorter(containers, key=None):
    """Custom drag-drop component.

    containers: list of dicts:
      {
        "header": "...html...",
        "meta": "...html..." (optional),
        "items": [{"id": str, "name": str, "quote": str, "role": str, "secs": int}, ...]
      }
    First container is rendered as the "assembly" (full-width, horizontal items).
    Subsequent containers are laid out as a responsive grid of category columns.

    Returns: list[list[str]] — for each container, the ordered list of item ids
    after user dragging. None on first render before user interaction.
    """
    return _block_sorter_component(containers=containers, key=key, default=None)

st.set_page_config(page_title="SRT → Premiere XML", page_icon="🎬", layout="wide")


# ============================================================================
# Constants
# ============================================================================

NARRATIVE_ROLES = {
    "opener":  {"emoji": "🎯", "label_zh": "吸睛開頭",
                "desc": "金句、反差、衝擊數據——適合放在新影片最前面抓住注意力"},
    "teaser":  {"emoji": "🪝", "label_zh": "勾人 hook",
                "desc": "懸念、提問、tease——讓觀眾想繼續看下去"},
    "context": {"emoji": "💭", "label_zh": "鋪陳脈絡",
                "desc": "情境、背景、設定——交代必要的前提"},
    "climax":  {"emoji": "🔥", "label_zh": "高潮金句",
                "desc": "轉折、洞見、共鳴——影片的情緒/觀點高峰"},
    "story":   {"emoji": "💝", "label_zh": "動人故事",
                "desc": "個人經驗、案例、情感——讓觀點落地"},
    "data":    {"emoji": "📊", "label_zh": "數據佐證",
                "desc": "事實、數字、研究——強化說服力"},
    "closer":  {"emoji": "🎬", "label_zh": "收尾呼籲",
                "desc": "行動、總結、希望——影片結尾留印象"},
}
ROLE_ORDER = ["opener", "teaser", "context", "climax", "story", "data", "closer"]


# ============================================================================
# State init
# ============================================================================

def reset_workflow():
    for k in list(st.session_state.keys()):
        if k.startswith("api_key_input") or k.startswith("llm_model_input") \
                or k == "llm_provider_input":
            continue
        del st.session_state[k]
    st.session_state["stage"] = "mode_select"


for k, v in [
    ("stage", "mode_select"),
    ("mode", None),
    ("frozen_config", None),
    ("themes", None),
    ("chosen_theme", None),
    ("block_pool", None),     # all proposed blocks {id: block}
    ("selected_ids", []),     # ordered list of ids in 你的剪輯
    ("presets", None),
    ("chosen_preset", None),
    ("remove_keep_flags", None),
    ("regen_themes", 0),
    ("regen_blocks", 0),
    ("regen_presets", 0),
]:
    st.session_state.setdefault(k, v)


# ============================================================================
# Sidebar
# ============================================================================

LLM_PROVIDERS = {
    "Anthropic": {
        "key_label": "Anthropic API key",
        "models": ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
        "default_model_idx": 1,
    },
    "OpenAI": {
        "key_label": "OpenAI API key",
        "models": ["gpt-5.5", "gpt-5", "gpt-4.1", "gpt-4o"],
        "default_model_idx": 0,
    },
}

with st.sidebar:
    st.header("🤖 LLM 設定")
    provider = st.selectbox("Provider", list(LLM_PROVIDERS.keys()),
        index=0, key="llm_provider_input")
    _p = LLM_PROVIDERS[provider]
    api_key = st.text_input(_p["key_label"], type="password",
        key=f"api_key_input_{provider}",
        help="僅在此瀏覽器 session 使用,不會儲存。關閉分頁即清除。")
    llm_model = st.selectbox("Model",
        _p["models"], index=_p["default_model_idx"],
        key=f"llm_model_input_{provider}")
    if api_key: st.caption(f"🔓 已輸入 key(僅本次 session,使用 {provider})")
    else: st.caption("輸入 key 才能使用 LLM 提案功能")

    st.divider()
    if st.button("🔄 重設流程", use_container_width=True):
        reset_workflow()
        st.rerun()

    if st.session_state.get("frozen_config"):
        c = st.session_state["frozen_config"]
        st.divider()
        st.subheader("📋 前置設定摘要")
        st.caption(
            f"**模式**:`{c['mode']}`\n\n"
            f"**規格**:{c['fps']} {c['df']} · {c['width']}×{c['height']}\n\n"
            f"**目標**:{c['target_duration_seconds']:.0f} 秒\n\n"
            f"**鏡位**:{'A+B 雙機' if c['multicam_enabled'] else '僅 A 機'}"
        )


# ============================================================================
# Header
# ============================================================================

st.title("🎬 SRT → Premiere XML 自動剪輯工具")
stage_label = {
    "mode_select":      "1️⃣ 選擇剪輯模式",
    "setup_highlight":  "2️⃣ 前置設定 (短片精華)",
    "setup_sequential": "2️⃣ 前置設定 (順剪)",
    "theme_pick":       "3️⃣ 選擇主題",
    "block_builder":    "4️⃣ 組合片段",
    "preset_pick":      "3️⃣ 選擇移除程度",
    "remove_review":    "4️⃣ 微調移除清單",
    "cam_review_seq":   "5️⃣ 分配每段鏡位",
    "review":           "6️⃣ 確認並產出",
}.get(st.session_state["stage"], "?")
st.caption(f"📍 {stage_label}")


# ============================================================================
# LLM helpers
# ============================================================================

def call_llm(api_key, model, system, user_msg, max_tokens=8192,
             provider="Anthropic"):
    if provider == "OpenAI":
        try:
            import openai
        except ImportError:
            st.error("尚未安裝 `openai` 套件,請執行 `pip install openai`")
            st.stop()
        client = openai.OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=model, max_completion_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
        )
        return resp.choices[0].message.content
    try:
        import anthropic
    except ImportError:
        st.error("尚未安裝 `anthropic` 套件")
        st.stop()
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(model=model, max_tokens=max_tokens,
        system=system, messages=[{"role": "user", "content": user_msg}])
    return msg.content[0].text


# Backwards-compatible alias used in earlier code paths.
call_claude = call_llm


def extract_json(text):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    return json.loads(text)


SYS_THEMES = """You are an SRT-to-Premiere editing assistant for Taiwanese users (highlight mode).

Propose 5-7 DISTINCT theme options for a short narrative clip from this SRT.
Each theme is a narrative angle / story focus.

Vary the angles: emotional / data / humorous / interactive / contrast / hook+arc.

**IMPORTANT: Respond in Traditional Chinese (zh-TW)** for `name`, `angle`,
`selling_point`, `audience_fit`. `hook_quote` is verbatim from SRT.

Respond with JSON ONLY:
{
  "themes": [
    {
      "name": "<簡短主題名稱>",
      "angle": "<一句話描述敘事角度>",
      "hook_quote": "<verbatim SRT line>",
      "cue_range_hint": "<rough cue range, just for user preview>",
      "estimated_length_seconds": <int>,
      "selling_point": "<為什麼這主題有效>",
      "audience_fit": "<適合什麼觀眾>"
    }
  ]
}
"""

SYS_BLOCKS = """You are an SRT-to-Premiere editing assistant for Taiwanese users (highlight mode).

Given an SRT, a chosen theme, target length, and camera availability,
PARTITION THE SRT TIMELINE INTO NON-OVERLAPPING SEGMENTS.

⚠️ CRITICAL CONSTRAINTS:
1. cue_range MUST contain ONLY cue numbers that EXIST in the SRT.
2. **NO TWO BLOCKS MAY SHARE ANY CUE NUMBER.** Every cue belongs to at most
   one block. This is a hard partition of the timeline.
3. Filler / garbage / off-topic / repeated-themselves cues are DROPPED —
   they simply don't appear in any block. (You don't need to cover every
   cue; you need to cover only the keepable ones, disjointly.)
4. Each block's cue_range MUST be a CONSECUTIVE range (e.g. "256-261",
   not "256,259,261").

Step-by-step process:
A. Read the entire SRT and identify topic-coherent chunks.
B. Drop the parts that are mic checks, false starts, asides, fumbles,
   Q&A interruptions, irrelevant tangents, or content that doesn't fit
   the chosen theme.
C. The remaining keep-worthy cues form your partition — split them into
   12-20 SEGMENTS at natural SENTENCE / TOPIC boundaries. Each segment
   must:
   - Start at a sentence beginning (not mid-clause)
   - End at a sentence ending (period, full pause)
   - Be SELF-CONTAINED — the segment makes sense on its own without
     needing the previous or next segment to resolve references
   This is what makes adjacent segments cuttable in any order without
   sounding choppy.
D. For each segment, assign ONE narrative_role.

narrative_role enum:
- opener  (吸睛開頭): hook quotes, contrast, shock data — for clip OPENING
- teaser  (勾人 hook): suspense, tease, question — make viewer want to continue
- context (鋪陳脈絡): scenario, background, premise
- climax  (高潮金句): reveal, insight, twist — clip's emotional/intellectual peak
- story   (動人故事): personal anecdote, case, emotion — grounds the message
- data    (數據佐證): facts, numbers, research — strengthens persuasion
- closer  (收尾呼籲): call to action, summary, hope — clip ENDING

Also produce a `recommended_assembly` — an ordered list of 5-8 segment IDs
that forms a complete narrative arc:
    opener → (teaser?) → context → story/data/climax → closer
The recommended_assembly may pick a SUBSET of segments (segments not on the
list still appear in the user's pool — they can pick differently).

⚠️ SEMANTIC FLOW IS CRITICAL — the recommended_assembly will be cut
together back-to-back with no transitions. Read the chosen segments AS A
SINGLE PARAGRAPH and make sure:
- Adjacent segments connect smoothly: no mid-sentence cuts, no pronouns
  referring to things only in earlier dropped segments, no jarring
  topic jumps.
- Each segment ends at a natural sentence boundary so the next segment's
  opening sentence stands on its own.
- The overall arc reads like the speaker said it in one breath, not
  like a clip show.
- Prefer slightly longer segments over shorter ones if it preserves
  semantic continuity.
If the only segments that fit the theme don't connect smoothly, it's
better to return fewer segments than to assemble a choppy clip.

**IMPORTANT: Respond in Traditional Chinese (zh-TW)** for `name`,
`selling_point`. `hook_quote` is verbatim from SRT.

Respond with JSON ONLY:
{
  "blocks": [
    {
      "id": "<short unique id, e.g. 'b1', 'b2'...>",
      "name": "<簡短中文名稱>",
      "hook_quote": "<verbatim from SRT>",
      "cue_range": "<consecutive cues, e.g. '82' or '256-261'>",
      "estimated_length_seconds": <int>,
      "narrative_role": "opener|teaser|context|climax|story|data|closer",
      "suggested_cam": "A|B",
      "selling_point": "<中文 why-it-works>"
    }
  ],
  "recommended_assembly": ["b1", "b3", "b7", ...]
}

Camera psychology (set suggested_cam):
- A (front wide) = objective context, scene-setting, callouts
- B (side close) = emotion, punchlines, intimate moments
"""

SYS_PRESETS = """You are an SRT-to-Premiere editing assistant in sequential clean-cut mode for Taiwanese users.

Propose 3 removal heaviness presets:
1. Conservative — only obvious filler (嗯, 啊, 那個, mic check, false starts)
2. Moderate — conservative + obvious redundancy + dead air
3. Aggressive — moderate + off-topic tangents + Q&A interludes + repetition

For each, list ALL cues to remove with reason and preview text.

**IMPORTANT: Respond in Traditional Chinese (zh-TW)** for `description`
and `reason`. Keep `name` as English label. `preview_text` is verbatim.

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
# Helpers
# ============================================================================

def time_3unit(label, key_prefix, fps_int,
               default_m=0, default_s=0, default_f=0,
               help_text=None, allow_minutes=True):
    """Render a 3-unit time picker (minutes / seconds / frames). Returns (m, s, f)."""
    if label:
        st.markdown(f"**{label}**")
    if help_text:
        st.caption(help_text)
    if allow_minutes:
        cols = st.columns([2, 0.7, 2, 0.7, 2, 1.6])
        with cols[0]:
            m = st.number_input("min", min_value=0, value=default_m, step=1,
                label_visibility="collapsed", key=f"{key_prefix}_min")
        with cols[1]:
            st.markdown("<div style='padding-top:0.5em'><b>分</b></div>",
                unsafe_allow_html=True)
        with cols[2]:
            s = st.number_input("sec", min_value=0, max_value=59,
                value=default_s, step=1,
                label_visibility="collapsed", key=f"{key_prefix}_sec")
        with cols[3]:
            st.markdown("<div style='padding-top:0.5em'><b>秒</b></div>",
                unsafe_allow_html=True)
        with cols[4]:
            f = st.number_input("frame", min_value=0, max_value=fps_int - 1,
                value=default_f, step=1,
                label_visibility="collapsed", key=f"{key_prefix}_frame")
        with cols[5]:
            st.markdown(f"<div style='padding-top:0.5em'><b>frame</b> "
                        f"(0–{fps_int - 1})</div>", unsafe_allow_html=True)
    else:
        m = 0
        cols = st.columns([3, 0.7, 3, 1.6])
        with cols[0]:
            s = st.number_input("sec", min_value=0,
                value=default_s, step=1,
                label_visibility="collapsed", key=f"{key_prefix}_sec")
        with cols[1]:
            st.markdown("<div style='padding-top:0.5em'><b>秒</b></div>",
                unsafe_allow_html=True)
        with cols[2]:
            f = st.number_input("frame", min_value=0, max_value=fps_int - 1,
                value=default_f, step=1,
                label_visibility="collapsed", key=f"{key_prefix}_frame")
        with cols[3]:
            st.markdown(f"<div style='padding-top:0.5em'><b>frame</b> "
                        f"(0–{fps_int - 1})</div>", unsafe_allow_html=True)
    return m, s, f


def block_to_label(b):
    """Compact 1-line label for sortable display."""
    role = b.get("narrative_role", "")
    role_emoji = NARRATIVE_ROLES.get(role, {}).get("emoji", "📝")
    name = b.get("name", "?")
    quote = b.get("hook_quote", "")[:30]
    secs = b.get("estimated_length_seconds", "?")
    return f"{role_emoji} [{secs}s] {name} — 「{quote}」"


def parse_label_to_id(label, blocks_by_id):
    """Reverse: given the label string in sortable, find its block id."""
    for bid, b in blocks_by_id.items():
        if block_to_label(b) == label:
            return bid
    return None


# ============================================================================
# Stage: mode_select
# ============================================================================

def render_mode_select():
    st.markdown("## 你想做什麼樣的剪輯?")

    col1, col2 = st.columns(2)
    with col1:
        with st.container(border=True):
            st.markdown("### 🎬 短片精華")
            st.markdown("**`highlight`** mode")
            st.markdown(
                "從原片挑出有故事弧的精華片段組合成新片。\n\n"
                "**適合**:募款短片 / 社群短片 / 預告 / 摘要金句\n\n"
                "**長度**:15 秒 ~ 5 分鐘\n\n"
                "**多機**:支援 A+B 雙機切換\n\n"
                "**流程**:\n"
                "1. 設定規格 →\n"
                "2. 選一個敘事主題 →\n"
                "3. AI 提案推薦版本 + 分類好的素材庫 →\n"
                "4. 你拖拉組合 → 產出"
            )
            if st.button("選擇短片精華", type="primary",
                         key="pick_highlight", use_container_width=True):
                st.session_state["mode"] = "highlight"
                st.session_state["stage"] = "setup_highlight"
                st.rerun()

    with col2:
        with st.container(border=True):
            st.markdown("### ✂️ 順剪去廢話")
            st.markdown("**`sequential`** mode")
            st.markdown(
                "保留原 SRT 順序,AI 直接掃出冗詞贅句、離題段落,你勾選確認後移除。\n\n"
                "**適合**:演講順剪 / podcast 清理 / 內部存檔\n\n"
                "**長度**:5 分鐘 ~ 30 分鐘\n\n"
                "**多機**:支援 A+B(每段可分配鏡位)\n\n"
                "**流程**:\n"
                "1. 設定規格 →\n"
                "2. AI 直接掃描提移除清單(無需主題討論) →\n"
                "3. 你勾選/取消個別 cue →\n"
                "4. (多機)為每段分配 A/B 鏡位 → 產出"
            )
            if st.button("選擇順剪去廢話", type="primary",
                         key="pick_sequential", use_container_width=True):
                st.session_state["mode"] = "sequential"
                st.session_state["stage"] = "setup_sequential"
                st.rerun()


# ============================================================================
# Stage: Setup (mode-aware)
# ============================================================================

def render_setup():
    mode = st.session_state["mode"]
    is_seq = (mode == "sequential")
    st.markdown(
        f"### 前置設定 — {'順剪' if is_seq else '短片精華'}模式\n"
        "填寫所有技術參數並上傳 SRT。按下「開始」後這些設定會**鎖定**,"
        "需要修改請使用左側的「🔄 重設流程」。"
    )

    # ----- SRT -----
    st.subheader("📄 SRT")
    uploaded = st.file_uploader("上傳 SRT 檔案", type=["srt"])
    purpose = st.text_input(
        "用途 / 目標觀眾(選填)",
        placeholder="例如:募款短片 / 社群短片 / podcast 順剪",
    )

    # Inline-parse uploaded SRT to fetch cue 1's SRT start time (used
    # below as default for the "first speech in source" alignment input).
    cue_1_srt_start = None
    if uploaded:
        try:
            srt_preview = uploaded.getvalue().decode("utf-8")
            preview_cues = srt2xml.parse_srt(srt_preview)
            if preview_cues:
                cue_1_srt_start = preview_cues[min(preview_cues.keys())][0]
        except Exception:
            pass

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
        audio_sr = st.selectbox("音訊取樣率 (Hz)", [44100, 48000, 96000], index=1)
    with c7:
        audio_depth = st.selectbox("位元深度 (bit)", [16, 24])
    with c8:
        audio_channels = st.number_input("聲道數", 1, 8, 2)

    st.divider()

    # ----- Cameras -----
    st.subheader("🎥 攝影機")
    st.caption("不需要在這裡指定來源檔路徑——匯入 Premiere 後 re-link 即可。")
    fps_int = srt2xml.FPS_PRESETS[fps]["fps_int"]

    # Default 0,0,0 (per user request). Math: "0,0,0" means SRT cue 1
    # maps to source TC 0:00:00 — i.e. no pre-roll, speaker starts at
    # source's very first frame. For sources with pre-roll the user
    # scrubs Premiere to find where the speaker starts and types in
    # that TC.

    a_m, a_s, a_f = time_3unit(
        "📍 SRT cue 1(第一段話)在 A 機源檔的第幾分幾秒幾 frame 開始?",
        key_prefix="a_srt_starts", fps_int=fps_int,
        default_m=0, default_s=0, default_f=0,
        help_text=(
            "預設 0,0,0 = 沒有 pre-roll(SRT 跟源檔對齊)。\n\n"
            "• **Whisper 從完整 A 機原檔轉錄、源檔沒 pre-roll**:留 0,0,0。\n\n"
            "• **A 機有 pre-roll**(錄影機提早開機 13 分鐘才開始講話):"
            "在 Premiere 中 scrub 找到 cue 1 第一個字實際出現在 A 機源檔的時間點,輸入該值。"
        ),
    )
    if cue_1_srt_start is not None:
        st.caption(
            f"💡 你的 SRT 第一個 cue 從 SRT 第 **{cue_1_srt_start:.2f} 秒** 開始(供參考)。"
        )

    multicam_enabled = False
    b_m = b_s = b_f = 0
    multicam_enabled = st.checkbox(
        "➕ 加入 B 機(雙機 multicam)",
        help=("highlight 模式:你可以在每段選 A 或 B。\n\n"
              "sequential 模式:AI 移除廢話之後,你可以為每個保留段落選 A 或 B。"),
    )
    if multicam_enabled:
        b_m, b_s, b_f = time_3unit(
            "⏱️ B 機比 A 機晚多久開機?",
            key_prefix="b_delay", fps_int=fps_int,
            help_text="方向:B 比 A **晚**開機(B 的 pre-roll 較短)。"
                      "可從兩機都看得到的拍手 / 揮手動作目測。",
        )

    # Distinct placeholders so Premiere's relink dialog treats A and B as
    # separate missing files. If both are "<<RELINK>>" Premiere sees them
    # as identical paths and auto-applies the user's first relink choice
    # to both clips — pointing A and B at the same physical file.
    a_path = "<<RELINK_A_CAMERA>>"
    b_path = "<<RELINK_B_CAMERA>>"

    st.divider()

    # ----- Output -----
    st.subheader("⏱️ 輸出設定")
    st.markdown("**目標長度**")
    td_n, td_u = st.columns([3, 1])
    with td_n:
        target_value = st.number_input("target value",
            value=1.0, min_value=0.0, step=0.5,
            label_visibility="collapsed")
    with td_u:
        target_unit_label = st.selectbox("target unit",
            ["秒 (seconds)", "分鐘 (minutes)", "小時 (hours)"], index=1,
            label_visibility="collapsed")
    target_unit = target_unit_label.split(" ")[0]
    target_seconds = target_value * {"秒": 1, "分鐘": 60, "小時": 3600}[target_unit]
    st.caption(f"= {target_seconds:g} 秒")

    st.markdown("**Padding**(每個 cut 邊界外擴的緩衝秒數)")
    pad_n, pad_u = st.columns([3, 1])
    with pad_n:
        padding = st.number_input("padding value",
            value=0.5, step=0.1, min_value=0.0,
            label_visibility="collapsed")
    with pad_u:
        st.markdown("<div style='padding-top:0.5em'><b>秒</b></div>",
            unsafe_allow_html=True)

    st.divider()

    # ----- Start -----
    btn_label = "▶️ 開始" + ("(AI 直接掃描)" if is_seq else "(進入主題選擇)")
    loading_flag = "setup_loading"
    is_loading = st.session_state.get(loading_flag, False)
    start_clicked = st.button(
        ("⏳ 載入中…" if is_loading else btn_label),
        type="primary",
        use_container_width=True,
        disabled=is_loading,
    )
    status_slot = st.empty()

    # Two-phase: first click sets the flag and reruns so the button
    # re-renders as disabled. Second pass (with flag set) actually runs
    # the LLM call.
    if start_clicked and not is_loading:
        errors = []
        if not uploaded: errors.append("請先上傳 SRT 檔案")
        if not api_key: errors.append(f"請在左側輸入 {provider} API key")
        if df == "DF" and fps not in ("29.97", "59.94"):
            errors.append(f"DF 僅支援 29.97 / 59.94 fps,目前選的是 {fps}")
        if errors:
            for e in errors: st.error(e)
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
            "a_srt_starts_at": (a_m * 60) + a_s + (a_f / fps_int),
            "multicam_enabled": multicam_enabled,
            "b_path": b_path,
            "b_delay_seconds": int(b_m * 60 + b_s),
            "b_delay_frames": int(b_f),
            "target_duration_seconds": float(target_seconds),
            "padding": float(padding),
        }
        st.session_state[loading_flag] = True
        st.rerun()

    if is_loading:
        # Second-phase: button is already showing disabled, now do the work.
        spinner_msg = (
            "🤖 正在掃描 SRT 找移除候選..." if is_seq
            else "🤖 正在從 SRT 產生主題提案..."
        )
        with status_slot.container():
            with st.spinner(spinner_msg):
                try:
                    if is_seq:
                        st.session_state["presets"] = fetch_presets()
                    else:
                        st.session_state["themes"] = fetch_themes()
                except Exception as e:
                    st.session_state[loading_flag] = False
                    st.error(f"LLM 呼叫失敗:{e}")
                    return
        st.session_state[loading_flag] = False
        st.session_state["stage"] = (
            "preset_pick" if is_seq else "theme_pick"
        )
        st.rerun()


# ============================================================================
# Stage: theme_pick (highlight)
# ============================================================================

def fetch_themes(prev=None):
    c = st.session_state["frozen_config"]
    user_msg = (
        f"Target length: {c['target_duration_seconds']:.0f} seconds\n"
        f"Purpose: {c['purpose'] or '(unspecified)'}\n"
        f"Camera: {'A+B (multicam)' if c['multicam_enabled'] else 'A only'}\n\n"
        f"SRT:\n{c['srt_text']}"
    )
    if prev:
        names = ", ".join(t.get("name", "?") for t in prev)
        user_msg += f"\n\nPreviously proposed themes: {names}. Propose DIFFERENT angles."
    return extract_json(call_llm(
        api_key, llm_model, SYS_THEMES, user_msg, provider=provider
    )).get("themes", [])


def render_theme_pick():
    c = st.session_state["frozen_config"]
    st.markdown(f"### 選擇敘事主題 · *目標 {c['target_duration_seconds']:.0f}s*")

    if st.session_state["themes"] is None:
        with st.spinner("🤖 正在從 SRT 產生主題提案..."):
            try:
                st.session_state["themes"] = fetch_themes()
            except Exception as e:
                st.error(f"LLM 呼叫失敗:{e}")
                if st.button("↻ 重試"):
                    st.session_state["themes"] = None; st.rerun()
                return

    themes = st.session_state["themes"]
    if not themes:
        st.warning("沒有回傳任何主題")
        if st.button("↻ 重試"):
            st.session_state["themes"] = None; st.rerun()
        return

    for i, t in enumerate(themes):
        with st.container(border=True):
            head_l, head_r = st.columns([4, 1])
            with head_l:
                st.markdown(
                    f"**{t.get('name', f'主題 {i+1}')}** · "
                    f"~{t.get('estimated_length_seconds', '?')}s"
                )
                st.markdown(f"💬 *“{t.get('hook_quote', '')}”*")
                st.caption(f"📐 {t.get('angle', '')}")
                st.caption(f"🎯 {t.get('selling_point', '')}")
                if t.get("audience_fit"):
                    st.caption(f"👥 {t['audience_fit']}")
            with head_r:
                if st.button("✓ 用這個", type="primary",
                             key=f"theme_{i}", use_container_width=True):
                    st.session_state["chosen_theme"] = t
                    st.session_state["block_pool"] = None
                    st.session_state["selected_ids"] = []
                    st.session_state["regen_blocks"] = 0
                    st.session_state["stage"] = "block_builder"
                    st.rerun()

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        if st.button("← 回到前置設定", use_container_width=True):
            st.session_state["stage"] = "setup_highlight"
            st.session_state["themes"] = None
            st.rerun()
    with col2:
        if st.button("↻ 全部重新生成主題",
                     type="secondary", use_container_width=True):
            with st.spinner("重新產生不同角度的主題..."):
                try:
                    st.session_state["themes"] = fetch_themes(prev=themes)
                    st.session_state["regen_themes"] += 1
                except Exception as e:
                    st.error(f"重新生成失敗:{e}")
            st.rerun()


# ============================================================================
# Stage: block_builder (highlight, drag-drop)
# ============================================================================

def fetch_blocks_for_theme(theme, prev=None):
    c = st.session_state["frozen_config"]
    cues_dict = c["cues"]
    max_cue = max(cues_dict.keys()) if cues_dict else 0
    cam_desc = "A+B (multicam)" if c["multicam_enabled"] else "A only"
    user_msg = (
        f"Chosen theme:\n{json.dumps(theme, ensure_ascii=False, indent=2)}\n\n"
        f"Camera: {cam_desc}\n"
        f"Target length: {c['target_duration_seconds']:.0f} seconds\n"
        f"⚠️ THE SRT HAS CUES NUMBERED 1 TO {max_cue} — DO NOT propose cues > {max_cue}.\n\n"
        f"SRT:\n{c['srt_text']}"
    )
    if prev:
        names = ", ".join(b.get("name", "?") for b in prev.values())
        user_msg += (f"\n\nPreviously proposed blocks: {names}. "
                     f"Propose DIFFERENT blocks (different cue ranges and angles).")
    resp = extract_json(call_llm(
        api_key, llm_model, SYS_BLOCKS, user_msg, provider=provider
    ))
    blocks = resp.get("blocks", [])
    recommended = resp.get("recommended_assembly", [])

    # Filter / clip blocks: drop blocks whose cue_range has NO valid cues,
    # clip blocks where some cues exceed SRT range.
    valid_blocks = []
    dropped = []
    clipped = []
    for b in blocks:
        if "id" not in b or "cue_range" not in b:
            continue
        try:
            cue_nums = srt2xml.parse_cues(b["cue_range"])
        except Exception:
            dropped.append((b.get("name"), b.get("cue_range")))
            continue
        valid = sorted([n for n in cue_nums if n in cues_dict])
        if not valid:
            dropped.append((b.get("name"), b.get("cue_range")))
            continue
        if len(valid) < len(cue_nums):
            new_range = f"{valid[0]}-{valid[-1]}" if len(valid) > 1 else str(valid[0])
            clipped.append((b.get("name"), b["cue_range"], new_range))
            b["cue_range"] = new_range
        valid_blocks.append(b)

    if dropped:
        st.warning(
            f"⚠️ LLM 提了 {len(dropped)} 個 SRT 範圍外的 block,已濾掉:\n"
            + "\n".join(f"  - 「{n}」 cues `{r}`" for n, r in dropped)
        )
    if clipped:
        st.info(
            f"ℹ️ LLM 提了 {len(clipped)} 個 block 部分超出 SRT 範圍(cue 上限 "
            f"{max_cue}),自動 clip 到有效 cue:\n"
            + "\n".join(f"  - 「{n}」 `{old}` → `{new}`" for n, old, new in clipped)
        )

    # Hard-enforce disjointness: walk blocks in SRT order and drop any block
    # whose cue_range shares a cue with an earlier kept block. This makes
    # the block pool a true partition (no cue appears in two blocks), so
    # the user cannot accidentally pick overlapping content.
    valid_blocks.sort(
        key=lambda b: sorted(srt2xml.parse_cues(b["cue_range"]))[0]
    )
    seen_cues = set()
    disjoint_blocks = []
    overlap_dropped = []
    for b in valid_blocks:
        nums = set(srt2xml.parse_cues(b["cue_range"]))
        clash = nums & seen_cues
        if clash:
            overlap_dropped.append(
                (b.get("name"), b["cue_range"], sorted(clash))
            )
            continue
        disjoint_blocks.append(b)
        seen_cues |= nums
    if overlap_dropped:
        st.info(
            f"ℹ️ LLM 提了 {len(overlap_dropped)} 個 block 跟其他 block 共用 cue,"
            "為了避免內容重複,已丟掉後出現的:\n"
            + "\n".join(
                f"  - 「{n}」 cues `{r}` 跟既有 block 重疊在 `{sorted(c)}`"
                for n, r, c in overlap_dropped
            )
        )
    valid_blocks = disjoint_blocks

    blocks_by_id = {b["id"]: b for b in valid_blocks}
    valid_recommended = [rid for rid in recommended if rid in blocks_by_id]
    return blocks_by_id, valid_recommended


def render_block_builder():
    c = st.session_state["frozen_config"]
    theme = st.session_state["chosen_theme"]

    # First-time fetch: blocks + initial recommended assembly
    if st.session_state["block_pool"] is None:
        with st.spinner("🤖 正在依主題產生分類好的素材庫和推薦版本..."):
            try:
                pool, recommended = fetch_blocks_for_theme(theme)
                st.session_state["block_pool"] = pool
                st.session_state["selected_ids"] = [
                    rid for rid in recommended if rid in pool
                ]
            except Exception as e:
                st.error(f"LLM 呼叫失敗:{e}")
                if st.button("↻ 重試"):
                    st.session_state["block_pool"] = None; st.rerun()
                return

    pool = st.session_state["block_pool"]
    selected_ids = st.session_state["selected_ids"]

    # Header
    st.markdown(f"### 組合片段 · *{theme.get('name')}*")
    st.caption(f"💬 *“{theme.get('hook_quote', '')}”*  ·  "
               f"📐 {theme.get('angle', '')}")

    # Total length tracker
    total_secs = sum(
        pool[bid].get("estimated_length_seconds", 0) for bid in selected_ids if bid in pool
    )
    target_sec = c.get("target_duration_seconds") or None
    diff_text = ""
    if target_sec:
        diff = total_secs - target_sec
        tag = "🎯" if abs(diff) < 5 else ("⚠️ 超過" if diff > 0 else "ℹ️ 不足")
        diff_text = f" · {tag} 目標 {target_sec:.0f}s,差距 {diff:+.1f}s"
    st.markdown(
        f"##### {len(selected_ids)} 段 · ~{total_secs}s{diff_text}"
    )

    st.caption(
        "拖曳卡片重新排序、跨欄移動。點卡片右側 ▸ 展開細節。"
        "下方各欄是「在輸出影片中扮演的角色」,不是 SRT 原順序。"
    )

    # Build per-role categorization (only blocks NOT in selected_ids)
    selected_set = set(selected_ids)
    blocks_by_role = {role: [] for role in ROLE_ORDER}
    for bid, b in pool.items():
        if bid in selected_set:
            continue
        role = b.get("narrative_role", "context")
        blocks_by_role.setdefault(role, []).append(bid)

    def fmt_srt_range(start_sec, end_sec):
        def fmt_t(s):
            m = int(s // 60)
            sec = s - m * 60
            return f"{m}:{sec:05.2f}"
        return f"{fmt_t(start_sec)}–{fmt_t(end_sec)}"

    def make_item(b):
        role_key = b.get("narrative_role", "")
        info = NARRATIVE_ROLES.get(role_key, {})
        cue_range = b.get("cue_range", "")
        srt_range = ""
        actual_text = ""
        try:
            cue_nums = srt2xml.parse_cues(cue_range)
            srt_cues = c["cues"]
            present = [n for n in cue_nums if n in srt_cues]
            if present:
                start = min(srt_cues[n][0] for n in present)
                end = max(srt_cues[n][1] for n in present)
                srt_range = fmt_srt_range(start, end)
                # Concatenate the actual SRT text for this block's cues
                # so the user can verify the LLM label matches reality.
                actual_text = " ".join(srt_cues[n][2] for n in present)
        except Exception:
            pass
        # Prefer the real SRT text over the LLM's possibly-hallucinated
        # hook_quote. Falls back to hook_quote if the cue range was bad.
        quote = actual_text or b.get("hook_quote", "")
        return {
            "id": b["id"],
            "name": b.get("name", ""),
            "quote": quote,
            "role": info.get("emoji", "") + " " + info.get("label_zh", ""),
            "role_key": role_key,  # for auto-route lookup in frontend
            "secs": b.get("estimated_length_seconds", "?"),
            "cue_range": cue_range,
            "srt_range": srt_range,
        }

    containers = [{
        "header": "你的剪輯",
        "meta": f"{len(selected_ids)} 段 · ~{total_secs}s",
        "items": [make_item(pool[bid]) for bid in selected_ids if bid in pool],
        "empty_msg": "(從下方分類拖曳卡片到這裡開始組合)",
        "role": "__assembly__",
    }]
    for role in ROLE_ORDER:
        info = NARRATIVE_ROLES[role]
        ids = blocks_by_role.get(role, [])
        containers.append({
            "header": f"{info['emoji']} {info['label_zh']}",
            "meta": f"{len(ids)}",
            "items": [make_item(pool[bid]) for bid in ids],
            "empty_msg": "(空)",
            "role": role,
        })

    # Render custom component
    result = block_sorter(
        containers,
        key=f"sorter_{st.session_state['regen_blocks']}",
    )

    # Parse result: list of lists of ids, one per container.
    # Reorder the pool dict so its iteration matches frontend's order
    # (assembly first, then per-category in ROLE_ORDER). This makes the
    # next render produce containers IDENTICAL to what the frontend
    # already shows → fingerprint matches → no rebuild → no flicker.
    if result and isinstance(result, list) and len(result) > 0:
        new_selected_ids = [bid for bid in result[0] if bid in pool]
        flat_order = list(new_selected_ids)
        # Persist the user's category choice: when a card is dropped into a
        # category column whose role doesn't match its narrative_role, we
        # rewrite the block's narrative_role so the next re-render keeps it
        # in the destination column. Without this, the per-role grouping
        # snaps the card back, looking like a failed drag.
        for cat_idx, cat_list in enumerate(result[1:]):
            target_role = (
                ROLE_ORDER[cat_idx] if cat_idx < len(ROLE_ORDER) else None
            )
            for bid in cat_list:
                if bid in pool:
                    if target_role and pool[bid].get("narrative_role") != target_role:
                        pool[bid]["narrative_role"] = target_role
                    if bid not in flat_order:
                        flat_order.append(bid)
        # Append any orphaned items (shouldn't happen normally)
        for bid in pool:
            if bid not in flat_order:
                flat_order.append(bid)
        st.session_state["block_pool"] = {bid: pool[bid] for bid in flat_order}
        st.session_state["selected_ids"] = new_selected_ids

    new_selected_ids = st.session_state["selected_ids"]

    # Show selected detail summary
    if new_selected_ids:
        with st.expander(f"📖 你的剪輯 — 詳細內容 ({len(new_selected_ids)} 段)", expanded=False):
            for i, bid in enumerate(new_selected_ids):
                if bid not in pool: continue
                b = pool[bid]
                role_info = NARRATIVE_ROLES.get(b.get("narrative_role", ""), {})
                st.markdown(
                    f"**{i+1}. {role_info.get('emoji', '')} {b.get('name')}** · "
                    f"`{b.get('narrative_role', '?')}` · "
                    f"~{b.get('estimated_length_seconds', '?')}s"
                )
                st.caption(f"💬 「{b.get('hook_quote', '')}」")
                st.caption(f"🎯 {b.get('selling_point', '')} · "
                           f"cues `{b.get('cue_range')}` · "
                           f"建議鏡位 `{b.get('suggested_cam', 'A')}`")

    st.divider()

    # Per-cut camera override (multicam only)
    if c["multicam_enabled"] and new_selected_ids:
        with st.expander("🎥 調整每段鏡位 (A / B)"):
            for i, bid in enumerate(new_selected_ids):
                if bid not in pool: continue
                b = pool[bid]
                cur_cam = b.get("cam") or b.get("suggested_cam", "A")
                new_cam = st.radio(
                    f"{i+1}. {b.get('name')} (建議 `{b.get('suggested_cam', 'A')}`)",
                    ["A", "B"], index=(0 if cur_cam == "A" else 1),
                    horizontal=True, key=f"cam_{bid}",
                )
                pool[bid]["cam"] = new_cam

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("← 換主題", use_container_width=True):
            st.session_state["stage"] = "theme_pick"
            st.session_state["block_pool"] = None
            st.session_state["selected_ids"] = []
            st.rerun()
    with col2:
        if st.button("↻ 重新生成素材庫(保留你已選的)",
                     type="secondary", use_container_width=True):
            with st.spinner("重新產生未選擇的素材..."):
                try:
                    new_pool, _ = fetch_blocks_for_theme(theme, prev=pool)
                    # Merge: keep user-selected blocks (overwrite pool entries
                    # for those ids if absent from new pool)
                    for bid in selected_ids:
                        if bid in pool and bid not in new_pool:
                            new_pool[bid] = pool[bid]
                    st.session_state["block_pool"] = new_pool
                    st.session_state["regen_blocks"] += 1
                except Exception as e:
                    st.error(f"重新生成失敗:{e}")
            st.rerun()
    with col3:
        ok = len(new_selected_ids) > 0
        if st.button("→ 進入確認步驟", type="primary",
                     use_container_width=True, disabled=not ok):
            st.session_state["stage"] = "review"
            st.rerun()


# ============================================================================
# Stage: preset_pick (sequential)
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
    return extract_json(call_llm(
        api_key, llm_model, SYS_PRESETS, user_msg, provider=provider
    )).get("presets", [])


def render_preset_pick():
    st.markdown("### 選擇移除程度")
    st.caption("Sequential 模式保持 SRT 原順序,只移除指定的 cue。"
               "AI 已自動分析 SRT,以下三個方案保守度遞增。")

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
            st.session_state["stage"] = "setup_sequential"
            st.session_state["presets"] = None
            st.rerun()
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
        c = st.session_state["frozen_config"]
        next_stage = "cam_review_seq" if c["multicam_enabled"] else "review"
        next_label = "✓ 進入鏡位分配" if c["multicam_enabled"] else "✓ 確認"
        if st.button(next_label, type="primary", use_container_width=True):
            st.session_state["stage"] = next_stage
            st.rerun()


# ============================================================================
# Stage: cam_review_seq (sequential + multicam — assign A/B per kept segment)
# ============================================================================

def render_cam_review_seq():
    c = st.session_state["frozen_config"]
    p = st.session_state["chosen_preset"]
    flags = st.session_state["remove_keep_flags"]
    active_removes = [r for r, k in zip(p.get("remove", []), flags) if k]

    # Compute kept segments based on current removals
    cut_specs = srt2xml.compute_sequential_cuts(
        c["cues"], active_removes, default_cam="A"
    )

    st.markdown(f"### 🎥 為每個保留段落分配鏡位 ({len(cut_specs)} 段)")
    st.caption(
        "套用所選移除規則後分成這幾段。預設都用 A 機,你可以個別切到 B 機"
        "(例如情緒重的段落用 B 機特寫)。"
    )

    cam_overrides = st.session_state.setdefault("seq_cam_overrides", {})

    for i, cs in enumerate(cut_specs):
        cue_str = cs["cues"]
        cur_cam = cam_overrides.get(cue_str, "A")
        with st.container(border=True):
            head = st.columns([0.5, 5, 2])
            head[0].markdown(f"**{i+1}.**")
            # Preview text from the first 2 cues of this segment
            cues_list = srt2xml.parse_cues(cue_str)
            preview_parts = []
            for cn in cues_list[:2]:
                if cn in c["cues"]:
                    preview_parts.append(c["cues"][cn][2][:30])
            preview = " · ".join(preview_parts)
            head[1].markdown(
                f"cues `{cue_str}`\n\n"
                f"<span style='color:#666;font-size:13px'>"
                f"「{preview}{'...' if len(cues_list) > 2 else ''}」</span>",
                unsafe_allow_html=True,
            )
            new_cam = head[2].radio(
                "cam", ["A", "B"],
                index=(0 if cur_cam == "A" else 1),
                horizontal=True, key=f"seq_cam_{cue_str}_{i}",
                label_visibility="collapsed",
            )
            cam_overrides[cue_str] = new_cam

    # Summary
    cnt_a = sum(1 for v in cam_overrides.values() if v == "A")
    cnt_b = sum(1 for v in cam_overrides.values() if v == "B")
    st.caption(f"分配:A 機 {cnt_a} 段 · B 機 {cnt_b} 段")

    st.divider()
    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("← 回到移除清單", use_container_width=True):
            st.session_state["stage"] = "remove_review"
            st.rerun()
    with col2:
        if st.button("🔄 全部重設為 A 機",
                     type="secondary", use_container_width=True):
            st.session_state["seq_cam_overrides"] = {}
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
        pool = st.session_state["block_pool"] or {}
        sel_ids = st.session_state["selected_ids"]
        if not sel_ids:
            st.error("還沒選任何片段,請回上一步。")
            if st.button("← 返回"):
                st.session_state["stage"] = "block_builder"; st.rerun()
            return
        rows = []
        cues_dict = c["cues"]
        cut_cue_sets = []  # for overlap detection
        for i, bid in enumerate(sel_ids):
            if bid not in pool: continue
            b = pool[bid]
            cue_range = b.get("cue_range", "")
            actual_text = ""
            srt_window = ""
            cue_set = set()
            try:
                cue_nums = srt2xml.parse_cues(cue_range)
                present = [n for n in cue_nums if n in cues_dict]
                cue_set = set(present)
                if present:
                    actual_text = " ".join(cues_dict[n][2] for n in present)
                    s = min(cues_dict[n][0] for n in present)
                    e = max(cues_dict[n][1] for n in present)
                    srt_window = (
                        f"{int(s//60)}:{s-int(s//60)*60:05.2f}"
                        f"–{int(e//60)}:{e-int(e//60)*60:05.2f}"
                    )
            except Exception:
                pass
            cut_cue_sets.append((i, cue_set, b.get("name", ""), cue_range))
            rows.append({
                "#": i,
                "鏡位": b.get("cam") or b.get("suggested_cam", "A"),
                "cues": cue_range,
                "SRT 時間": srt_window,
                "narrative_role": b.get("narrative_role", ""),
                "實際內容(從 SRT 取出)": actual_text[:80] + ("…" if len(actual_text) > 80 else ""),
            })

        # Overlap detection — backstop in case the partition enforcement
        # in fetch_blocks_for_theme misses an edge case. With the new
        # disjoint-pool design (PR #4) this warning should rarely fire.
        cue_to_cuts = {}
        for idx, cue_set, _, _ in cut_cue_sets:
            for n in cue_set:
                cue_to_cuts.setdefault(n, []).append(idx)
        duplicated_cues = {n: cuts for n, cuts in cue_to_cuts.items() if len(cuts) > 1}
        if duplicated_cues:
            pair_to_cues = {}
            for n, idxs in duplicated_cues.items():
                key = tuple(sorted(idxs))
                pair_to_cues.setdefault(key, []).append(n)
            lines = []
            for key, ns in sorted(pair_to_cues.items()):
                ns_sorted = sorted(ns)
                if len(ns_sorted) > 1 and max(ns_sorted) - min(ns_sorted) == len(ns_sorted) - 1:
                    n_str = f"{ns_sorted[0]}-{ns_sorted[-1]}"
                else:
                    n_str = ",".join(str(n) for n in ns_sorted)
                lines.append(f"  • cuts **#{'、'.join(f'#{i}' for i in key)}** 都包含 cue `{n_str}`")
            st.warning(
                "⚠️ **內容重疊警告**——以下 cut 共用了相同的 cue,"
                "這幾段內容會在輸出影片中重複播放:\n\n" + "\n".join(lines)
                + "\n\n如果這不是你要的,回上一步調整 cue 範圍或拿掉重複的 cut。"
            )

        st.caption("⚠️ **產出 XML 之前請對照「實際內容」這一欄**——這是 SRT 在這些 cue 範圍裡真正的逐字稿,不是 AI 給的標籤。")
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

        # If multicam, also show the cam-per-segment assignment
        if c["multicam_enabled"]:
            cam_overrides = st.session_state.get("seq_cam_overrides", {})
            cut_specs = srt2xml.compute_sequential_cuts(
                c["cues"], kept, default_cam="A"
            )
            cam_rows = [{
                "段#": i,
                "cues": cs["cues"],
                "鏡位": cam_overrides.get(cs["cues"], "A"),
            } for i, cs in enumerate(cut_specs)]
            st.markdown("**鏡位分配**")
            st.dataframe(cam_rows, use_container_width=True, hide_index=True)

    col1, col2 = st.columns([1, 2])
    with col1:
        if st.button("← 返回", use_container_width=True):
            if mode == "highlight":
                st.session_state["stage"] = "block_builder"
            else:
                st.session_state["stage"] = (
                    "cam_review_seq" if c["multicam_enabled"] else "remove_review"
                )
            st.rerun()
    with col2:
        if st.button("⚙️ 產出 XML", type="primary", use_container_width=True):
            generate_xml()


def build_spec():
    c = st.session_state["frozen_config"]

    # User input semantics: "first speech (SRT cue 1) is at A-source TC X"
    # — i.e. the user reads m:s:f off Premiere, which is nominal frames.
    # SRT timestamps are real-world seconds (Whisper output). For NTSC
    # framerates these differ by 0.1%, so we must convert TC→RW seconds
    # via actual_fps, NOT via the nominal timebase. Otherwise every cut
    # drifts ~0.1% later than intended (≈1 second over 16 min of SRT).
    cues = c["cues"]
    cue_1_srt_start = (
        cues[min(cues.keys())][0] if cues else 0.0
    )
    fps_preset = srt2xml.get_fps_preset(c["fps"])
    timebase = fps_preset["timebase"]
    fps_real = srt2xml.actual_fps(fps_preset)
    # `a_srt_starts_at` = the source TC where SRT cue 1's first frame
    # appears (in nominal seconds). Default is 0,0,0 = "speaker starts
    # at source 0:00:00 / no pre-roll". Convert nominal → RW seconds via
    # timebase/actual_fps, then subtract cue 1's SRT offset to get the
    # source second value that corresponds to SRT 0.
    a_anchor_rw_seconds = c["a_srt_starts_at"] * timebase / fps_real
    srt_zero_at_source = a_anchor_rw_seconds - cue_1_srt_start

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
            "A": {"file": "A_camera", "path": c["a_path"],
                  "srt_starts_at_source_seconds": srt_zero_at_source},
        },
        "settings": {
            "padding": c["padding"],
            "multicam": c["multicam_enabled"],
            "target_duration": c["target_duration_seconds"] or None,
        },
        "mode": c["mode"],
    }
    if c["multicam_enabled"]:
        spec["cameras"]["B"] = {"file": "B_camera",
            "path": c["b_path"], "delay_from_a": b_delay_str}

    if c["mode"] == "highlight":
        pool = st.session_state["block_pool"] or {}
        spec["cuts"] = []
        for bid in st.session_state["selected_ids"]:
            if bid not in pool: continue
            b = pool[bid]
            spec["cuts"].append({
                "cam": b.get("cam") or b.get("suggested_cam", "A"),
                "cues": b.get("cue_range"),
                "role": b.get("narrative_role", ""),
                "label": b.get("name", ""),
            })
    else:
        flags = st.session_state["remove_keep_flags"]
        p = st.session_state["chosen_preset"]
        active_removes = [r for r, k in zip(p.get("remove", []), flags) if k]
        if c["multicam_enabled"]:
            # Build explicit cuts with per-segment cam assignment
            cam_overrides = st.session_state.get("seq_cam_overrides", {})
            cut_specs = srt2xml.compute_sequential_cuts(
                c["cues"], active_removes, default_cam="A"
            )
            spec["mode"] = "highlight"  # explicit cuts path
            spec["cuts"] = [{
                "cam": cam_overrides.get(cs["cues"], "A"),
                "cues": cs["cues"],
                "label": cs.get("label", ""),
            } for cs in cut_specs]
        else:
            spec["default_cam"] = "A"
            spec["remove"] = active_removes
    return spec


def generate_xml():
    c = st.session_state["frozen_config"]
    spec = build_spec()
    cues = c["cues"]
    try:
        srt2xml.validate_spec(spec)
        fps_preset = srt2xml.get_fps_preset(spec["sequence"]["fps"])
        fps_real = srt2xml.actual_fps(fps_preset)
        cam_offsets, anchor_cam = srt2xml.compute_offsets(spec["cameras"], fps_preset, c["df"])
        if spec["mode"] == "sequential":
            cut_specs = srt2xml.compute_sequential_cuts(cues, spec.get("remove", []), "A")
        else:
            cut_specs = spec["cuts"]
        if not cut_specs:
            st.error("套用設定後沒有產生任何 cut")
            return
        cuts = srt2xml.expand_cuts(cut_specs, cues, c["padding"])
        total_frames = srt2xml.compute_frames(cuts, cam_offsets, fps_preset)
        xml_text = srt2xml.emit_xml(spec, cuts, total_frames, cam_offsets)
    except SystemExit as e:
        st.error(f"產出失敗:{e}"); return
    except Exception as e:
        st.error(f"未預期錯誤:{e}"); return

    duration_sec = total_frames / fps_real
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
if stage == "mode_select":
    render_mode_select()
elif stage in ("setup_highlight", "setup_sequential"):
    render_setup()
elif stage == "theme_pick":
    render_theme_pick()
elif stage == "block_builder":
    render_block_builder()
elif stage == "preset_pick":
    render_preset_pick()
elif stage == "remove_review":
    render_remove_review()
elif stage == "cam_review_seq":
    render_cam_review_seq()
elif stage == "review":
    render_review()
