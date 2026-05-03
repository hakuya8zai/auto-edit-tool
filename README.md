---
title: Auto Edit Tool
emoji: 🎬
colorFrom: blue
colorTo: purple
sdk: streamlit
sdk_version: 1.30.0
app_file: app.py
pinned: false
license: mit
---

# auto-edit-tool · Streamlit app

Convert Whisper-style SRT into Premiere Pro FCP7 XML, single-camera or dual-camera multicam, with two editing modes (`highlight` / `sequential`).

Wraps the deterministic `srt2xml.py` engine with a Streamlit UI; optionally calls the Anthropic API for LLM-suggested cut themes (BYOK — your key, your bill, never persisted).

## Live demo

Deploy your own to Streamlit Community Cloud (free) — see Deploy section.

## Local run

```bash
git clone https://github.com/<your-account>/srt-edit-app
cd srt-edit-app
pip install -r requirements.txt
streamlit run app.py
```

Open http://localhost:8501.

## Workflow

**Phase 1 — Setup** (front-loaded form):
1. Upload SRT, pick mode (`highlight` or `sequential`), enter purpose.
2. Fill sequence specs: fps, NDF/DF, resolution, audio, pixel aspect.
3. Camera A: anchor source TC + SRT-time, file path.
4. (Optional) Camera B: source path + delay from A (e.g. `54s29f`).
5. Target duration: `60s` / `1min` / `1.5min` / `1h30min` / `1:30`.
6. Padding (default 0.5s).
7. Click **Start discussion** → setup is frozen.

**Phase 2 — Discussion**:
- **`highlight`** mode (block-based assembly):
  - LLM proposes 8-12 content **blocks** (each a quotable segment with hook, cue range, role).
  - Browse left panel of blocks, click ➕ to add to your edit (right panel).
  - Reorder selected blocks with ⬆️⬇️, swap camera A/B per block, 🗑️ remove.
  - Live total length vs target shown.
  - Not satisfied? Click **↻ 全部重新生成 blocks** for a different LLM proposal set.
- **`sequential`** mode (clean cut):
  - LLM proposes 3 removal heaviness presets (Conservative / Moderate / Aggressive).
  - Pick one → fine-tune with checkboxes (uncheck to keep a cue).
  - Regenerate presets if needed.

**Phase 3 — Generate**:
- Review final cut list / removal list.
- Click Generate XML → download.
- Verification anchors panel: predicted speech text per cut for scrub-confirming in Premiere.

## Two editing modes

| | `highlight` | `sequential` |
|---|---|---|
| Cut order | reorder freely | preserve SRT order |
| Multicam | yes (A+B) | single-cam only |
| Use case | 短片精華, 募款片, 社群短片 | 演講順剪, podcast 清理 |
| Length | 15s ~ 5min | 5min ~ 30min |

## API key handling

- Entered in sidebar via password-masked input.
- Held only in browser session (`st.session_state` is NOT used for the key).
- **Not** logged, **not** stored to disk, **not** sent anywhere except api.anthropic.com.
- Closing the tab clears it.

## Spec quick reference

See `examples/` for full JSON specs:

- `dual_cam_5994.json` — dual-cam highlight at 59.94 NDF
- `single_cam_2997df.json` — single-cam highlight at 29.97 DF
- `sequential_clean_cut.json` — sequential mode w/ remove list

## Deploy to Streamlit Community Cloud

1. Push this repo to your GitHub.
2. Go to https://share.streamlit.io and "Create app" pointing at your repo / `app.py`.
3. No secrets needed (key is user-input).
4. Done. URL is `<your-account>-<repo-name>.streamlit.app`.

## Engine

The math/XML emission is in `srt2xml.py` (~800 lines, stdlib only). It supports:

- fps: 23.976, 24, 25, 29.97, 30, 50, 59.94, 60
- NDF / SMPTE drop-frame TC math
- 1 to N cameras with anchor TC + delay
- Multicam dual-track expansion via enable/disable
- Padding rules (outer edges + shared-boundary detection)
- `target_duration` parsing from human-friendly strings
- `--analyze` JSON / `--validate` schema-only modes (used by the app)

You can also use `srt2xml.py` standalone:

```bash
python3 srt2xml.py --srt subtitle.srt --spec cuts.json --output edit.xml --verify
```

## Lessons baked in

- SRT cue numbers are NOT proportional to timestamps — script handles via direct lookup.
- Always-correct math, but **inputs need verification**: source TC anchor (eyeball'd from Premiere) and B delay (eyeball'd between cameras) can be off by 1-2 seconds. Verify with anchor scrubbing + multicam toggle.
- Don't hand-patch XML — edit the spec, regenerate.

## License

MIT.
