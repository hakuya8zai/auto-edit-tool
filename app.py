"""Streamlit app: SRT → Premiere FCP7 XML — block-based assembly wizard.

Two-phase flow:

  Phase 1 (Setup): pre-fill all technical parameters once
                   (fps, anchor TC, B delay, paths, target duration, etc.)
                   + upload SRT, pick mode/purpose.

  Phase 2 (Discussion):
    - highlight: LLM proposes 8-12 content BLOCKS;
                 user adds blocks to edit, reorders, swaps camera per block,
                 with "↻ 全部重新生成" if unsatisfied.
    - sequential: LLM proposes 3 removal-heaviness presets;
                  user picks one, fine-tunes per-cue keep flags.

  Stage 3 (Review & generate): final cut list → XML download + verify anchors.

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
    st.header("🤖 LLM")
    api_key = st.text_input(
        "Anthropic API key", type="password", key="api_key_input",
        help="Used only this session, never stored.",
    )
    llm_model = st.selectbox(
        "Model",
        ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
        index=1, key="llm_model_input",
    )
    if api_key: st.caption("🔓 Key entered (session only)")
    else: st.caption("Enter key to enable LLM proposals.")

    st.divider()
    if st.button("🔄 Reset workflow", use_container_width=True):
        reset_workflow()
        st.rerun()

    if st.session_state.get("frozen_config"):
        st.divider()
        st.subheader("📋 Setup summary")
        c = st.session_state["frozen_config"]
        st.caption(
            f"**{c['fps']} {c['df']}** · {c['width']}×{c['height']}\n\n"
            f"**Mode**: `{c['mode']}` · target `{c['target_duration']}`\n\n"
            f"**Cameras**: {'A+B (multicam)' if c['multicam_enabled'] else 'A only'}"
        )


# ============================================================================
# Header
# ============================================================================

st.title("🎬 SRT → Premiere XML")
stage_label = {
    "setup":         "1️⃣ Setup",
    "block_builder": "2️⃣ Pick & arrange blocks",
    "preset_pick":   "2️⃣ Pick removal preset",
    "remove_review": "3️⃣ Fine-tune removals",
    "review":        "4️⃣ Review & generate",
}.get(st.session_state["stage"], "?")
st.caption(f"📍 {stage_label}")


# ============================================================================
# LLM helpers
# ============================================================================

def call_claude(api_key, model, system, user_msg, max_tokens=8192):
    try:
        import anthropic
    except ImportError:
        st.error("`anthropic` package not installed.")
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


SYS_BLOCKS = """You are an SRT-to-Premiere editing assistant.

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

Respond with JSON ONLY (no markdown):
{
  "blocks": [
    {
      "name": "<short evocative>",
      "hook_quote": "<verbatim SRT line>",
      "cue_range": "<consecutive cues, e.g. '82' or '256-261'>",
      "estimated_length_seconds": <int>,
      "role": "<cold-open|hook|setup|punchline|callout|close|demo|data|transition>",
      "suggested_cam": "A|B",
      "selling_point": "<short why-it-works>"
    }
  ]
}

Camera psychology (use to set suggested_cam):
- A (front wide) = objective context, scene-setting, callouts, conceptual
- B (side close) = emotion, punchlines, intimate moments, audience reactions
"""

SYS_PRESETS = """You are an SRT-to-Premiere editing assistant in sequential clean-cut mode.

Propose 3 removal heaviness presets:
1. Conservative — only obvious filler (嗯, 啊, 那個, mic check, false starts)
2. Moderate — conservative + obvious redundancy + dead air
3. Aggressive — moderate + off-topic tangents + Q&A interludes + repetition

For each, list ALL cues to remove with reason and preview text.

Respond with JSON ONLY:
{
  "presets": [
    {
      "name": "Conservative|Moderate|Aggressive",
      "description": "<one sentence>",
      "estimated_kept_seconds": <int>,
      "remove": [
        {"cues": "<cue or range>", "reason": "<short>", "preview_text": "<≤30 chars>"}
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
        "Fill in technical parameters and upload SRT. "
        "These get **frozen** when you start. Use sidebar **Reset** to change."
    )

    # ----- SRT + mode -----
    st.subheader("📄 SRT & mode")
    col1, col2 = st.columns([2, 1])
    with col1:
        uploaded = st.file_uploader("SRT file", type=["srt"])
    with col2:
        mode = st.radio(
            "Mode", ["highlight", "sequential"],
            help="highlight = pick & arrange content blocks · "
                 "sequential = clean cut keeping order",
        )
    purpose = st.text_input(
        "Purpose / audience (optional)",
        placeholder="e.g. 募款短片 / 社群短片 / podcast cleanup",
    )

    st.divider()

    # ----- Sequence -----
    st.subheader("⚙️ Sequence")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        fps = st.selectbox("fps",
            ["23.976", "24", "25", "29.97", "30", "50", "59.94", "60"], index=6)
    with c2:
        df = st.selectbox("displayformat", ["NDF", "DF"],
            help="DF only valid for 29.97/59.94")
    with c3:
        width = st.number_input("width", min_value=1, value=1920, step=2)
    with c4:
        height = st.number_input("height", min_value=1, value=1080, step=2)

    c5, c6, c7, c8 = st.columns(4)
    with c5:
        pixel_aspect = st.selectbox("pixel aspect",
            ["square", "NTSC-601", "PAL-601", "HD"])
    with c6:
        audio_sr = st.selectbox("audio rate", [44100, 48000, 96000], index=1)
    with c7:
        audio_depth = st.selectbox("audio depth", [16, 24])
    with c8:
        audio_channels = st.number_input("audio ch", 1, 8, 2)

    st.divider()

    # ----- Cameras -----
    st.subheader("🎥 Cameras")
    st.caption(
        "Source video files don't need paths here — re-link them in Premiere "
        "after importing the XML."
    )

    multicam_enabled = st.checkbox(
        "➕ Add Camera B (multicam / dual-camera setup)",
        help="Required for highlight mode A/B switching. Sequential mode is single-cam.",
    )

    b_delay = "0s0f"
    if multicam_enabled:
        b_delay = st.text_input(
            "⏱️ B delay from A — how much later B camera started recording",
            "0s0f",
            help="Format: '54s29f' = 54 sec 29 frames · '54.5' = decimal sec · "
                 "'00:00:54:29' = TC. Direction: B started LATER than A.",
        )

    with st.expander(
        "⚙️ Advanced: SRT-to-source alignment "
        "(only if your SRT timestamps don't match source video timing)"
    ):
        st.markdown(
            "**If your SRT was auto-transcribed from this source video** "
            "(e.g. via Whisper), the timestamps already align — leave the defaults.\n\n"
            "**Otherwise** set the source TC reading at the moment SRT cue 1 starts. "
            "Example: source video records 13 min of pre-roll before speech begins, "
            "and SRT cue 1 starts at SRT 1.0s; you'd set "
            "`source_tc = 00:13:48:47`, `srt_at = 1.0`."
        )
        ca1, ca2 = st.columns(2)
        with ca1:
            a_tc = st.text_input("A anchor source TC", "00:00:00:00")
        with ca2:
            a_srt_at = st.number_input(
                "A anchor srt_at (seconds)", value=0.0, step=0.1
            )

    # Hardcoded placeholder paths — user will re-link in Premiere.
    a_path = "<<RELINK>>"
    b_path = "<<RELINK>>"

    st.divider()

    # ----- Output -----
    st.subheader("⏱️ Output")
    co1, co2 = st.columns(2)
    with co1:
        target_duration = st.text_input("Target duration", "60s",
            help="60 / '60s' / '1min' / '1.5min' / '1h30min' / '1:30'")
    with co2:
        padding = st.number_input("Padding (seconds)",
            value=0.5, step=0.1, min_value=0.0)

    st.divider()

    # ----- Start button (regular, not form-submit, so conditional UI works) -----
    if st.button("▶️ Start discussion", type="primary", use_container_width=True):
        errors = []
        if not uploaded:
            errors.append("Upload an SRT first.")
        if not api_key:
            errors.append("Enter Anthropic API key in sidebar.")
        if df == "DF" and fps not in ("29.97", "59.94"):
            errors.append(f"DF only valid for 29.97/59.94 fps (got {fps}).")
        if errors:
            for e in errors:
                st.error(e)
            return

        try:
            srt_text = uploaded.read().decode("utf-8")
            cues = srt2xml.parse_srt(srt_text)
        except Exception as e:
            st.error(f"SRT parse failed: {e}")
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
            "a_path": a_path, "a_tc": a_tc, "a_srt_at": float(a_srt_at),
            "multicam_enabled": multicam_enabled and mode == "highlight",
            "b_path": b_path, "b_delay": b_delay,
            "target_duration": target_duration,
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
        f"Target length: {c['target_duration']}\n"
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

    # Generate available blocks if not yet
    if st.session_state["available_blocks"] is None:
        with st.spinner("🤖 Generating content blocks..."):
            try:
                st.session_state["available_blocks"] = fetch_blocks()
            except Exception as e:
                st.error(f"LLM error: {e}")
                if st.button("↻ Retry"):
                    st.session_state["available_blocks"] = None; st.rerun()
                return

    available = st.session_state["available_blocks"]
    selected = st.session_state["selected_blocks"]
    selected_keys = {block_key(b) for b in selected}

    # Two-column layout: available + selected
    left, right = st.columns([1, 1])

    # --- Available blocks ---
    with left:
        st.markdown("### 📚 Available blocks")
        st.caption(f"{len(available)} proposed by LLM. Click ➕ to add to your edit.")
        for i, b in enumerate(available):
            already = block_key(b) in selected_keys
            with st.container(border=True):
                row = st.columns([4, 1])
                with row[0]:
                    st.markdown(
                        f"**{b.get('name', f'Block {i+1}')}** "
                        f"`[{b.get('role', '')}]` "
                        f"~{b.get('estimated_length_seconds', '?')}s"
                    )
                    st.markdown(f"💬 *“{b.get('hook_quote', '')}”*")
                    st.caption(
                        f"cues `{b.get('cue_range')}` · "
                        f"suggested cam `{b.get('suggested_cam', 'A')}` · "
                        f"{b.get('selling_point', '')}"
                    )
                with row[1]:
                    if already:
                        st.button("✓ Added", key=f"add_{i}",
                            disabled=True, use_container_width=True)
                    else:
                        if st.button("➕ Add", key=f"add_{i}",
                            type="primary", use_container_width=True):
                            new_block = dict(b)
                            new_block["cam"] = (
                                b.get("suggested_cam", "A")
                                if c["multicam_enabled"] else "A"
                            )
                            selected.append(new_block)
                            st.rerun()

        st.divider()
        if st.button("↻ 全部重新生成 blocks（不滿意這些）",
                     type="secondary", use_container_width=True):
            with st.spinner("Regenerating with different content..."):
                try:
                    st.session_state["available_blocks"] = fetch_blocks(prev=available)
                    st.session_state["regen_blocks"] += 1
                except Exception as e:
                    st.error(f"Regen error: {e}")
            st.rerun()

    # --- Selected blocks (ordered) ---
    with right:
        total_secs = sum(b.get("estimated_length_seconds", 0) for b in selected)
        st.markdown(
            f"### 🎬 Your edit · {len(selected)} blocks · ~{total_secs}s"
        )
        target_sec = (
            srt2xml.parse_duration(c["target_duration"])
            if c["target_duration"] else None
        )
        if target_sec:
            diff = total_secs - target_sec
            tag = "🎯" if abs(diff) < 5 else ("⚠️" if diff > 0 else "ℹ️")
            st.caption(f"{tag} target {target_sec:.0f}s · "
                       f"diff {diff:+.1f}s")

        if not selected:
            st.info("Empty — add blocks from the left.")
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
                                       help="Move up"):
                        selected[i], selected[i-1] = selected[i-1], selected[i]
                        st.rerun()
                    if head[4].button("⬇️", key=f"dn_{i}",
                                       disabled=(i == len(selected) - 1),
                                       help="Move down"):
                        selected[i], selected[i+1] = selected[i+1], selected[i]
                        st.rerun()
                    if head[5].button("🗑️", key=f"del_{i}", help="Remove"):
                        selected.pop(i)
                        st.rerun()

            st.caption("Tip: drag isn't supported, use ⬆️⬇️ to reorder.")

        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            if st.button("← Setup", use_container_width=True):
                reset_workflow(); st.rerun()
        with col2:
            ok = len(selected) > 0
            if st.button("→ Continue to review", type="primary",
                         use_container_width=True, disabled=not ok):
                st.session_state["stage"] = "review"
                st.rerun()


# ============================================================================
# Stage: preset pick (sequential)
# ============================================================================

def fetch_presets(prev=None):
    c = st.session_state["frozen_config"]
    user_msg = (
        f"Target length: {c['target_duration']}\n"
        f"Purpose: {c['purpose'] or '(unspecified)'}\n\n"
        f"SRT:\n{c['srt_text']}"
    )
    if prev: user_msg += "\n\nPropose DIFFERENT removal sets than before."
    return extract_json(call_claude(api_key, llm_model, SYS_PRESETS, user_msg)).get("presets", [])


def render_preset_pick():
    st.markdown("### Pick removal heaviness")
    st.caption("Sequential mode keeps SRT order; only removes specified cues.")

    if st.session_state["presets"] is None:
        with st.spinner("🤖 Analyzing SRT for removal candidates..."):
            try:
                st.session_state["presets"] = fetch_presets()
            except Exception as e:
                st.error(f"LLM error: {e}")
                if st.button("↻ Retry"):
                    st.session_state["presets"] = None; st.rerun()
                return

    presets = st.session_state["presets"]
    if not presets:
        st.warning("No presets returned.")
        if st.button("↻ Try again"):
            st.session_state["presets"] = None; st.rerun()
        return

    for i, p in enumerate(presets):
        with st.container(border=True):
            head_l, head_r = st.columns([3, 1])
            with head_l:
                st.markdown(
                    f"**{p.get('name', f'Preset {i+1}')}** · "
                    f"removes {len(p.get('remove', []))} ranges · "
                    f"~{p.get('estimated_kept_seconds', '?')}s kept"
                )
                st.caption(p.get("description", ""))
                rows = [{"cues": r.get("cues"), "reason": r.get("reason"),
                         "preview": r.get("preview_text", "")[:40]}
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
        if st.button("← Setup", use_container_width=True):
            reset_workflow(); st.rerun()
    with col2:
        if st.button("↻ 全部重新生成 presets",
                     type="secondary", use_container_width=True):
            with st.spinner("Regenerating..."):
                try:
                    st.session_state["presets"] = fetch_presets(prev=presets)
                    st.session_state["regen_presets"] += 1
                except Exception as e:
                    st.error(f"Regen error: {e}")
            st.rerun()


def render_remove_review():
    p = st.session_state["chosen_preset"]
    st.markdown(f"### Fine-tune removals  ·  *{p.get('name')}*")
    st.caption("Uncheck any removal you DON'T want.")

    flags = st.session_state["remove_keep_flags"]
    new_flags = []
    for i, r in enumerate(p.get("remove", [])):
        label = (f"`{r.get('cues')}` · {r.get('reason', '')} "
                 f"— *{r.get('preview_text', '')[:40]}*")
        new_flags.append(st.checkbox(label, value=flags[i], key=f"rmflag_{i}"))
    st.session_state["remove_keep_flags"] = new_flags
    st.caption(f"Active removals: **{sum(new_flags)} / {len(new_flags)}**")

    st.divider()
    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("← 換 preset", use_container_width=True):
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

    st.markdown("### Final review")

    if mode == "highlight":
        selected = st.session_state["selected_blocks"]
        if not selected:
            st.error("No blocks selected. Go back and add some.")
            if st.button("← Back"):
                st.session_state["stage"] = "block_builder"; st.rerun()
            return
        rows = [{
            "#": i,
            "cam": b.get("cam", "A"),
            "cues": b.get("cue_range"),
            "role": b.get("role", ""),
            "label": b.get("name", ""),
            "~length(s)": b.get("estimated_length_seconds", "?"),
        } for i, b in enumerate(selected)]
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        p = st.session_state["chosen_preset"]
        flags = st.session_state["remove_keep_flags"]
        kept = [r for r, k in zip(p.get("remove", []), flags) if k]
        st.markdown(f"**Preset**: {p.get('name')}  ·  "
                    f"**Active removals**: {len(kept)} / {len(p.get('remove', []))}")
        rows = [{"cues": r.get("cues"), "reason": r.get("reason"),
                 "preview": r.get("preview_text", "")[:40]}
                for r in kept]
        st.dataframe(rows, use_container_width=True, hide_index=True)

    col1, col2 = st.columns([1, 2])
    with col1:
        if st.button("← Back", use_container_width=True):
            st.session_state["stage"] = (
                "block_builder" if mode == "highlight" else "remove_review"
            )
            st.rerun()
    with col2:
        if st.button("⚙️ Generate XML", type="primary", use_container_width=True):
            generate_xml()


def build_spec():
    c = st.session_state["frozen_config"]
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
                "file": Path(c["a_path"]).name if c["a_path"] != "<<RELINK>>" else "A_camera",
                "path": c["a_path"],
                "anchor": {"srt_at": c["a_srt_at"], "source_tc": c["a_tc"]},
            },
        },
        "settings": {
            "padding": c["padding"],
            "multicam": c["multicam_enabled"],
            "target_duration": c["target_duration"] or None,
        },
        "mode": c["mode"],
    }
    if c["multicam_enabled"]:
        spec["cameras"]["B"] = {
            "file": Path(c["b_path"]).name if c["b_path"] != "<<RELINK>>" else "B_camera",
            "path": c["b_path"], "delay_from_a": c["b_delay"],
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
            st.error("No cuts after applying spec."); return
        cuts = srt2xml.expand_cuts(cut_specs, cues, c["padding"])
        total_frames = srt2xml.compute_frames(cuts, cam_offsets, timebase)
        xml_text = srt2xml.emit_xml(spec, cuts, total_frames, cam_offsets)
    except SystemExit as e:
        st.error(f"Generation failed: {e}"); return
    except Exception as e:
        st.error(f"Unexpected error: {e}"); return

    duration_sec = total_frames / timebase
    target_sec = (
        srt2xml.parse_duration(c["target_duration"])
        if c["target_duration"] else None
    )

    st.success(f"✅ Generated — {len(cuts)} cuts · {duration_sec:.2f}s")

    cols = st.columns(3)
    cols[0].metric("Cuts", len(cuts))
    cols[1].metric("Duration", f"{duration_sec:.2f}s",
        delta=(f"{duration_sec - target_sec:+.1f}s vs target"
               if target_sec else None))
    cols[2].metric("Frames", total_frames)

    out_name = f"{Path(c['uploaded_name']).stem}.xml"
    st.download_button("⬇️ Download XML", xml_text, file_name=out_name,
        mime="application/xml", use_container_width=True)

    with st.expander("🎯 Verification anchors", expanded=True):
        analysis = srt2xml.make_analysis(
            cuts, cues, cam_offsets, anchor_cam, fps_preset, total_frames
        )
        for cut in analysis["cuts"]:
            cams_at_mid = cut["mid"]["cameras"]
            st.markdown(
                f"**Cut {cut['index']} [{cut['cam']}]** {cut.get('label') or ''} · "
                f"TL {cut['timeline_in']}-{cut['timeline_out']} "
                f"({cut['duration_seconds']}s)"
            )
            st.caption(
                f"SRT mid: {cut['mid']['srt_seconds']}s "
                f"(cue {cut['mid']['cue_at_mid']}) · "
                + " · ".join(f"{cid}: frame {info['mid']}"
                             for cid, info in cams_at_mid.items())
            )
            st.code(f'Expected: "{cut["mid"]["expected_text"]}"')
        if spec["settings"]["multicam"]:
            st.info("🔄 Toggle V1/V2 enable at one cut to verify A/B sync.")

    with st.expander("🔍 Full spec used"):
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
