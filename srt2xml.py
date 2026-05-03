#!/usr/bin/env python3
"""srt2xml — SRT + cut spec to FCP7 XML for Premiere Pro.

Supports:
  - 1 to N cameras (single-cam or multi-cam)
  - Frame rates: 23.976 / 24 / 25 / 29.97 / 30 / 50 / 59.94 / 60
  - NDF / DF (DF only valid for 29.97 and 59.94)
  - Configurable resolution, pixel aspect, audio (sample rate / depth / channels)
  - Multicam dual-track expansion via enable/disable flags
  - --analyze (JSON instead of XML), --validate (spec check only), stdin/stdout via "-"

Usage:
  python3 srt2xml.py --srt subtitle.srt --spec cuts.json --output edit.xml --verify
  python3 srt2xml.py --spec cuts.json --validate
  python3 srt2xml.py --srt s.srt --spec c.json --analyze --output -

See cuts.json schema in the README or docs/cuts.schema.md.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape


# =============================================================================
# Frame rate registry
# =============================================================================

FPS_PRESETS = {
    "23.976": {"timebase": 24, "ntsc": True,  "fps_int": 24},
    "24":     {"timebase": 24, "ntsc": False, "fps_int": 24},
    "25":     {"timebase": 25, "ntsc": False, "fps_int": 25},
    "29.97":  {"timebase": 30, "ntsc": True,  "fps_int": 30, "df_drop": 2},
    "30":     {"timebase": 30, "ntsc": False, "fps_int": 30},
    "50":     {"timebase": 50, "ntsc": False, "fps_int": 50},
    "59.94":  {"timebase": 60, "ntsc": True,  "fps_int": 60, "df_drop": 4},
    "60":     {"timebase": 60, "ntsc": False, "fps_int": 60},
}


def get_fps_preset(fps_str):
    s = str(fps_str).strip()
    if s not in FPS_PRESETS:
        raise SystemExit(
            f"unsupported fps: {fps_str!r}. supported: {sorted(FPS_PRESETS)}"
        )
    return FPS_PRESETS[s]


# =============================================================================
# Timecode parsing (NDF + SMPTE drop-frame)
# =============================================================================

def parse_tc_to_frame(tc_str, fps_preset, displayformat):
    parts = re.split(r"[:;]", tc_str.strip())
    if len(parts) != 4:
        raise ValueError(
            f"bad TC format: {tc_str!r} (expected HH:MM:SS:FF or HH:MM:SS;FF)"
        )
    h, m, s, f = map(int, parts)
    fps_int = fps_preset["fps_int"]
    if displayformat == "DF":
        if "df_drop" not in fps_preset:
            raise ValueError(
                f"DF not supported for {fps_preset['fps_int']}fps "
                "(only 29.97 and 59.94 use drop-frame)"
            )
        drop = fps_preset["df_drop"]
        total_minutes = h * 60 + m
        drops = drop * (total_minutes - total_minutes // 10)
        nominal = ((h * 60 + m) * 60 + s) * fps_int + f
        return nominal - drops
    return ((h * 60 + m) * 60 + s) * fps_int + f


# =============================================================================
# Spec field parsers
# =============================================================================

def parse_cues(s):
    """Parse cue spec. Accepts:
      - int: 5
      - list: [5, 7, 9]
      - str: "5", "5-10", "5,7,9", "5-7,12,20-22"
    Returns sorted unique list of ints.
    """
    if isinstance(s, int):
        return [s]
    if isinstance(s, list):
        return sorted({int(n) for n in s})
    out = set()
    for part in str(s).strip().split(","):
        p = part.strip()
        if "-" in p:
            a, b = p.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        elif p:
            out.add(int(p))
    return sorted(out)


def parse_delay(value, fps_int):
    """Parse delay → frames. Accepts:
      - numeric seconds: 54.5
      - 'Ns Mf':         '54s29f'
      - TC:              '00:00:54:29' (treated as offset, not anchor)
    """
    if isinstance(value, (int, float)):
        return int(round(value * fps_int))
    s = str(value).strip()
    m = re.match(r"^(\d+)s(\d+)f$", s)
    if m:
        return int(m.group(1)) * fps_int + int(m.group(2))
    m = re.match(r"^(\d+):(\d+):(\d+)[:;](\d+)$", s)
    if m:
        h, mi, sec, fr = map(int, m.groups())
        return ((h * 60 + mi) * 60 + sec) * fps_int + fr
    try:
        return int(round(float(s) * fps_int))
    except ValueError:
        raise ValueError(
            f"bad delay format: {value!r}. accepted: '54s29f' / '00:00:54:29' / 54.5"
        )


def parse_duration(value):
    """Parse target duration → seconds (float). Accepts:
      - numeric: 60, 1.5  (treated as seconds)
      - '60s', '90s'
      - '1min', '1.5min', '10min'
      - '1h', '1.5h', '2h30min'
      - 'mm:ss' (e.g., '1:30' = 90s)
      - 'hh:mm:ss'
    """
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower().replace(" ", "")
    if not s:
        return None

    # hh:mm:ss or mm:ss
    if ":" in s:
        parts = s.split(":")
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        raise ValueError(f"bad duration: {value!r}")

    # 1h30min, 90min, 60s, 1.5h, etc.
    total = 0.0
    matched = False
    pattern = re.compile(r"(\d+(?:\.\d+)?)(h|hr|hour|hours|m|min|mins|minute|minutes|s|sec|secs|second|seconds)")
    for num_str, unit in pattern.findall(s):
        matched = True
        n = float(num_str)
        if unit in ("h", "hr", "hour", "hours"):
            total += n * 3600
        elif unit in ("m", "min", "mins", "minute", "minutes"):
            total += n * 60
        else:
            total += n
    if matched:
        return total

    # Plain number string = seconds
    try:
        return float(s)
    except ValueError:
        raise ValueError(
            f"bad duration: {value!r}. accepted: 60 / '60s' / '1min' / '1h30min' / '1:30' / '1:30:00'"
        )


# =============================================================================
# SRT parsing
# =============================================================================

def parse_srt(text):
    cues = {}
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue
        try:
            num = int(lines[0].strip())
        except ValueError:
            continue
        m = re.match(
            r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
            r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})",
            lines[1].strip(),
        )
        if not m:
            continue
        g = list(map(int, m.groups()))
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        cues[num] = (start, end, "\n".join(lines[2:]).strip())
    return cues


# =============================================================================
# Cut expansion + frame computation
# =============================================================================

def compute_sequential_cuts(srt, remove_specs, default_cam):
    """Sequential mode: keep all SRT cues except those listed in `remove`.
    Group consecutive kept cues (boundary = a removed cue) into cuts.
    """
    removed = set()
    for r in remove_specs or []:
        for n in parse_cues(r["cues"]):
            removed.add(n)

    sorted_cues = sorted(srt.keys())
    cuts = []
    group = []

    def flush():
        if not group:
            return
        cue_str = f"{group[0]}-{group[-1]}" if len(group) > 1 else str(group[0])
        cuts.append({
            "cam": default_cam,
            "cues": cue_str,
            "label": f"seg {cue_str}",
        })

    for n in sorted_cues:
        if n in removed:
            flush()
            group = []
        else:
            group.append(n)
    flush()

    return cuts


def expand_cuts(specs, srt, default_padding):
    cuts = []
    for spec in specs:
        nums = parse_cues(spec["cues"])
        missing = [n for n in nums if n not in srt]
        if missing:
            raise SystemExit(f"SRT missing cues: {missing} for cut {spec}")
        # Use min start / max end for the cue range (handles non-contiguous)
        srt_in_raw = min(srt[n][0] for n in nums)
        srt_out_raw = max(srt[n][1] for n in nums)
        cuts.append({
            **spec,
            "cue_nums": nums,
            "srt_in_raw": srt_in_raw,
            "srt_out_raw": srt_out_raw,
        })

    for i, cut in enumerate(cuts):
        prev = cuts[i - 1] if i > 0 else None
        nxt = cuts[i + 1] if i < len(cuts) - 1 else None
        pad_in = cut.get("pad_in", cut.get("padding", default_padding))
        pad_out = cut.get("pad_out", cut.get("padding", default_padding))
        share_in = prev is not None and abs(cut["srt_in_raw"] - prev["srt_out_raw"]) < 0.01
        share_out = nxt is not None and abs(cut["srt_out_raw"] - nxt["srt_in_raw"]) < 0.01
        cut["srt_in"] = cut["srt_in_raw"] - (0 if share_in else pad_in)
        cut["srt_out"] = cut["srt_out_raw"] + (0 if share_out else pad_out)
    return cuts


def compute_offsets(cams, fps_preset, displayformat):
    """Compute frame-at-SRT-0 per camera. Returns (offsets dict, anchor_cam_id)."""
    timebase = fps_preset["timebase"]
    fps_int = fps_preset["fps_int"]
    offsets = {}

    anchor_cam = next((cid for cid, c in cams.items() if "anchor" in c), None)
    if anchor_cam is None:
        raise SystemExit("at least one camera must have 'anchor' (srt_at + source_tc)")

    for cid, cam in cams.items():
        if "anchor" in cam:
            anchor_frame = parse_tc_to_frame(
                cam["anchor"]["source_tc"], fps_preset, displayformat
            )
            srt_at = float(cam["anchor"]["srt_at"])
            offsets[cid] = anchor_frame - int(round(srt_at * timebase))
        else:
            delay_key = f"delay_from_{anchor_cam.lower()}"
            if delay_key not in cam:
                raise SystemExit(
                    f"camera {cid!r} needs 'anchor' or {delay_key!r}"
                )
            delay = parse_delay(cam[delay_key], fps_int)
            offsets[cid] = offsets[anchor_cam] - delay

    return offsets, anchor_cam


def compute_frames(cuts, cam_offsets, timebase):
    timeline_pos = 0
    cam_ids = list(cam_offsets.keys())
    for cut in cuts:
        in_f = cut["srt_in"] * timebase
        out_f = cut["srt_out"] * timebase
        for cid, offset in cam_offsets.items():
            cut[f"{cid.lower()}_in"] = int(round(offset + in_f))
            cut[f"{cid.lower()}_out"] = int(round(offset + out_f))
        first = cam_ids[0].lower()
        dur = cut[f"{first}_out"] - cut[f"{first}_in"]
        cut["timeline_in"] = timeline_pos
        cut["timeline_out"] = timeline_pos + dur
        timeline_pos += dur
    return timeline_pos


# =============================================================================
# XML emission
# =============================================================================

def emit_xml(spec, cuts, total_duration, cam_offsets):
    seq = spec["sequence"]
    cams = spec["cameras"]
    settings = spec.get("settings", {})
    multicam = settings.get("multicam", False) and len(cams) > 1

    fps_preset = get_fps_preset(seq["fps"])
    timebase = fps_preset["timebase"]
    ntsc = "TRUE" if fps_preset["ntsc"] else "FALSE"
    df = seq.get("displayformat", "NDF")
    width = seq.get("width", 1920)
    height = seq.get("height", 1080)
    pixel_aspect = seq.get("pixel_aspect", "square")
    name = seq.get("name", "Sequence")
    audio = seq.get("audio", {})
    audio_rate = audio.get("sample_rate", 48000)
    audio_depth = audio.get("depth", 16)
    audio_channels = audio.get("channels", 2)

    rate = f"<rate><timebase>{timebase}</timebase><ntsc>{ntsc}</ntsc></rate>"
    cam_ids = list(cams.keys())

    # File definitions per camera (full first time, ref afterwards).
    file_state = {}
    for cid in cam_ids:
        c = cams[cid]
        fid = f"file-{cid}"
        duration = c.get("duration", 200000)
        path = c.get("path", "<<RELINK>>")
        file_name = c.get("file", f"{cid}_camera")
        full = (
            f'<file id="{fid}">\n'
            f'              <name>{xml_escape(file_name)}</name>\n'
            f'              <pathurl>file://{xml_escape(path)}</pathurl>\n'
            f'              {rate}\n'
            f'              <duration>{duration}</duration>\n'
            f'              <timecode>{rate}<string>00:00:00:00</string><frame>0</frame><displayformat>{df}</displayformat></timecode>\n'
            f'              <media>\n'
            f'                <video><duration>{duration}</duration><samplecharacteristics>{rate}<width>{width}</width><height>{height}</height><pixelaspectratio>{pixel_aspect}</pixelaspectratio></samplecharacteristics></video>\n'
            f'                <audio><samplecharacteristics><depth>{audio_depth}</depth><samplerate>{audio_rate}</samplerate></samplecharacteristics><channelcount>{audio_channels}</channelcount></audio>\n'
            f'              </media>\n'
            f'            </file>'
        )
        file_state[cid] = {
            "full": full,
            "ref": f'<file id="{fid}"/>',
            "used": False,
            "duration": duration,
        }

    def file_xml(cid):
        s = file_state[cid]
        if not s["used"]:
            s["used"] = True
            return s["full"]
        return s["ref"]

    def v_track(cid):
        return cam_ids.index(cid) + 1

    def a_tracks(cid):
        i = cam_ids.index(cid)
        return [i * audio_channels + ch for ch in range(1, audio_channels + 1)]

    def group_idx(cid):
        return cam_ids.index(cid) + 1

    def link_block(cut_idx, cid, clipidx):
        v = v_track(cid)
        a_list = a_tracks(cid)
        g = group_idx(cid)
        v_id = f"clip-v{v}-c{cut_idx}"
        parts = [
            f'<link><linkclipref>{v_id}</linkclipref><mediatype>video</mediatype>'
            f'<trackindex>{v}</trackindex><clipindex>{clipidx}</clipindex></link>'
        ]
        for a in a_list:
            aid = f"clip-a{a}-c{cut_idx}"
            parts.append(
                f'<link><linkclipref>{aid}</linkclipref><mediatype>audio</mediatype>'
                f'<trackindex>{a}</trackindex><clipindex>{clipidx}</clipindex>'
                f'<groupindex>{g}</groupindex></link>'
            )
        return "\n            ".join(parts)

    def video_clip(cut, cut_idx, clipidx, cid, enabled):
        in_f = cut[f"{cid.lower()}_in"]
        out_f = cut[f"{cid.lower()}_out"]
        en = "TRUE" if enabled else "FALSE"
        v = v_track(cid)
        return (
            f'          <clipitem id="clip-v{v}-c{cut_idx}">\n'
            f'            <name>{cid}_camera</name><enabled>{en}</enabled>'
            f'<duration>{file_state[cid]["duration"]}</duration>\n'
            f'            {rate}\n'
            f'            <start>{cut["timeline_in"]}</start><end>{cut["timeline_out"]}</end>'
            f'<in>{in_f}</in><out>{out_f}</out>\n'
            f'            {file_xml(cid)}\n'
            f'            {link_block(cut_idx, cid, clipidx)}\n'
            f'          </clipitem>'
        )

    def audio_clip(cut, cut_idx, clipidx, cid, ch, enabled):
        in_f = cut[f"{cid.lower()}_in"]
        out_f = cut[f"{cid.lower()}_out"]
        en = "TRUE" if enabled else "FALSE"
        a_idx = a_tracks(cid)[ch - 1]
        return (
            f'          <clipitem id="clip-a{a_idx}-c{cut_idx}">\n'
            f'            <name>{cid}_camera</name><enabled>{en}</enabled>'
            f'<duration>{file_state[cid]["duration"]}</duration>\n'
            f'            {rate}\n'
            f'            <start>{cut["timeline_in"]}</start><end>{cut["timeline_out"]}</end>'
            f'<in>{in_f}</in><out>{out_f}</out>\n'
            f'            {file_xml(cid)}\n'
            f'            <sourcetrack><mediatype>audio</mediatype><trackindex>{ch}</trackindex></sourcetrack>\n'
            f'            {link_block(cut_idx, cid, clipidx)}\n'
            f'          </clipitem>'
        )

    # Track contents per camera.
    track_clips = {}
    for cid in cam_ids:
        if multicam:
            track_clips[cid] = [(i, c, c["cam"] == cid) for i, c in enumerate(cuts)]
        else:
            track_clips[cid] = [(i, c, True) for i, c in enumerate(cuts) if c["cam"] == cid]

    # Build video tracks.
    video_tracks = []
    for cid in cam_ids:
        clips = track_clips[cid]
        clips_xml = "\n".join(
            video_clip(c, ci, idx + 1, cid, en)
            for idx, (ci, c, en) in enumerate(clips)
        )
        video_tracks.append(
            f'        <track>\n'
            f'          <enabled>TRUE</enabled><locked>FALSE</locked>\n'
            f'{clips_xml}\n'
            f'        </track>'
        )

    # Build audio tracks.
    audio_tracks = []
    for cid in cam_ids:
        clips = track_clips[cid]
        for ch in range(1, audio_channels + 1):
            clips_xml = "\n".join(
                audio_clip(c, ci, idx + 1, cid, ch, en)
                for idx, (ci, c, en) in enumerate(clips)
            )
            output_ch = ((ch - 1) % 2) + 1
            audio_tracks.append(
                f'        <track>\n'
                f'          <enabled>TRUE</enabled><locked>FALSE</locked>\n'
                f'          <outputchannelindex>{output_ch}</outputchannelindex>\n'
                f'{clips_xml}\n'
                f'        </track>'
            )

    video_blocks = "\n".join(video_tracks)
    audio_blocks = "\n".join(audio_tracks)

    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<!DOCTYPE xmeml>\n'
        f'<xmeml version="5">\n'
        f'  <sequence id="seq-{xml_escape(name)}">\n'
        f'    <name>{xml_escape(name)}</name>\n'
        f'    <duration>{total_duration}</duration>\n'
        f'    {rate}\n'
        f'    <in>-1</in>\n'
        f'    <out>-1</out>\n'
        f'    <timecode>{rate}<string>00:00:00:00</string><frame>0</frame><displayformat>{df}</displayformat></timecode>\n'
        f'    <media>\n'
        f'      <video>\n'
        f'        <format>\n'
        f'          <samplecharacteristics>{rate}<width>{width}</width><height>{height}</height><pixelaspectratio>{pixel_aspect}</pixelaspectratio><fielddominance>none</fielddominance><colordepth>24</colordepth></samplecharacteristics>\n'
        f'        </format>\n'
        f'{video_blocks}\n'
        f'      </video>\n'
        f'      <audio>\n'
        f'        <numOutputChannels>2</numOutputChannels>\n'
        f'        <format><samplecharacteristics><depth>{audio_depth}</depth><samplerate>{audio_rate}</samplerate></samplecharacteristics></format>\n'
        f'{audio_blocks}\n'
        f'      </audio>\n'
        f'    </media>\n'
        f'  </sequence>\n'
        f'</xmeml>\n'
    )


# =============================================================================
# Verify / analyze
# =============================================================================

def make_analysis(cuts, srt, cam_offsets, anchor_cam, fps_preset, total_duration):
    timebase = fps_preset["timebase"]
    out = []
    for idx, cut in enumerate(cuts):
        mid_srt = (cut["srt_in"] + cut["srt_out"]) / 2
        cue_at = next(
            (n for n, (s, e, _) in srt.items() if s <= mid_srt < e), None
        )
        text = srt[cue_at][2] if cue_at else None
        cams_at_mid = {
            cid: {
                "in": cut[f"{cid.lower()}_in"],
                "out": cut[f"{cid.lower()}_out"],
                "mid": int(round(offset + mid_srt * timebase)),
            }
            for cid, offset in cam_offsets.items()
        }
        out.append({
            "index": idx,
            "cam": cut["cam"],
            "cue_range": cut["cue_nums"],
            "label": cut.get("label"),
            "role": cut.get("role"),
            "srt_in": round(cut["srt_in"], 3),
            "srt_out": round(cut["srt_out"], 3),
            "timeline_in": cut["timeline_in"],
            "timeline_out": cut["timeline_out"],
            "duration_frames": cut["timeline_out"] - cut["timeline_in"],
            "duration_seconds": round((cut["timeline_out"] - cut["timeline_in"]) / timebase, 3),
            "mid": {
                "srt_seconds": round(mid_srt, 3),
                "cue_at_mid": cue_at,
                "expected_text": text,
                "cameras": cams_at_mid,
            },
        })
    return {
        "total_duration_frames": total_duration,
        "total_duration_seconds": round(total_duration / timebase, 3),
        "anchor_camera": anchor_cam,
        "camera_offsets": cam_offsets,
        "cuts": out,
    }


def print_verify(analysis, multicam):
    print("\n=== Verification anchors ===", file=sys.stderr)
    for cut in analysis["cuts"]:
        cams = cut["mid"]["cameras"]
        cam_str = " | ".join(f"{cid}: frame {info['mid']}" for cid, info in cams.items())
        active = cut["cam"]
        print(
            f"\nCut {cut['index']} [{active}] {cut.get('label') or ''}",
            file=sys.stderr,
        )
        print(
            f"  Timeline: {cut['timeline_in']}-{cut['timeline_out']} "
            f"({cut['duration_seconds']}s)",
            file=sys.stderr,
        )
        print(
            f"  SRT mid: {cut['mid']['srt_seconds']}s "
            f"(cue {cut['mid']['cue_at_mid']})",
            file=sys.stderr,
        )
        print(f"  Source frames: {cam_str}", file=sys.stderr)
        print(f"  Expected: \"{cut['mid']['expected_text']}\"", file=sys.stderr)
    if multicam:
        print(
            "\n[multicam] toggle V1/V2 enable at each timeline-mid; "
            "both cameras should show the same speech content.",
            file=sys.stderr,
        )
    print("", file=sys.stderr)


# =============================================================================
# IO helpers + spec validation
# =============================================================================

def read_input(path):
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def write_output(path, content):
    if path == "-":
        sys.stdout.write(content)
    else:
        Path(path).write_text(content, encoding="utf-8")


def validate_spec(spec):
    errors = []
    seq = spec.get("sequence", {})
    if "fps" not in seq:
        errors.append("sequence.fps required")
    elif str(seq["fps"]) not in FPS_PRESETS:
        errors.append(f"sequence.fps must be one of {sorted(FPS_PRESETS)}")
    fps_preset = FPS_PRESETS.get(str(seq.get("fps", "")), {})
    df = seq.get("displayformat", "NDF")
    if df not in ("NDF", "DF"):
        errors.append(f"displayformat must be NDF or DF, got {df!r}")
    if df == "DF" and "df_drop" not in fps_preset:
        errors.append(
            f"DF only valid for 29.97 or 59.94 fps (got {seq.get('fps')!r})"
        )

    cams = spec.get("cameras", {})
    if not cams:
        errors.append("cameras required (at least 1)")
    if not any("anchor" in c for c in cams.values()):
        errors.append("at least one camera must have 'anchor'")
    cam_ids = set(cams.keys())

    mode = spec.get("mode", "highlight")
    if mode not in ("highlight", "sequential"):
        errors.append(f"mode must be 'highlight' or 'sequential', got {mode!r}")

    if mode == "highlight":
        cuts = spec.get("cuts", [])
        if not cuts:
            errors.append("highlight mode requires non-empty 'cuts' list")
        for i, cut in enumerate(cuts):
            if "cam" not in cut:
                errors.append(f"cuts[{i}].cam required")
            elif cut["cam"] not in cam_ids:
                errors.append(
                    f"cuts[{i}].cam={cut['cam']!r} not in cameras {sorted(cam_ids)}"
                )
            if "cues" not in cut:
                errors.append(f"cuts[{i}].cues required")
    elif mode == "sequential":
        default_cam = spec.get("default_cam")
        if not default_cam:
            errors.append("sequential mode requires 'default_cam'")
        elif default_cam not in cam_ids:
            errors.append(
                f"default_cam={default_cam!r} not in cameras {sorted(cam_ids)}"
            )
        for i, r in enumerate(spec.get("remove", [])):
            if "cues" not in r:
                errors.append(f"remove[{i}].cues required")

    multicam = spec.get("settings", {}).get("multicam", False)
    if multicam and len(cams) < 2:
        errors.append("settings.multicam=true requires >= 2 cameras")
    if mode == "sequential" and multicam:
        errors.append("sequential mode + multicam not supported "
                      "(use highlight mode for multicam editing)")

    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


# =============================================================================
# Main
# =============================================================================

def main():
    ap = argparse.ArgumentParser(
        description="SRT + cut spec → FCP7 XML for Premiere Pro"
    )
    ap.add_argument("--srt", help="SRT file path or '-' for stdin")
    ap.add_argument("--spec", required=True, help="cuts JSON path or '-'")
    ap.add_argument("--output", help="XML/JSON output path or '-' (stdout)")
    ap.add_argument("--verify", action="store_true",
                    help="print verification anchors to stderr")
    ap.add_argument("--analyze", action="store_true",
                    help="output JSON analysis instead of XML")
    ap.add_argument("--validate", action="store_true",
                    help="validate spec only, no output")
    ap.add_argument("--target", help="target duration override "
                    "(e.g. '60s', '1min', '1h30min', '1:30')")
    ap.add_argument("--mode", choices=["highlight", "sequential"],
                    help="override spec.mode")
    args = ap.parse_args()

    spec = json.loads(read_input(args.spec))
    if args.mode:
        spec["mode"] = args.mode
    if args.target:
        spec.setdefault("settings", {})["target_duration"] = args.target
    validate_spec(spec)

    if args.validate:
        print("spec: OK", file=sys.stderr)
        return

    if not args.srt:
        raise SystemExit("--srt required (unless --validate)")

    srt = parse_srt(read_input(args.srt))

    seq = spec["sequence"]
    fps_preset = get_fps_preset(seq["fps"])
    timebase = fps_preset["timebase"]
    df = seq.get("displayformat", "NDF")
    settings = spec.get("settings", {})
    padding = settings.get("padding", 0.5)

    cam_offsets, anchor_cam = compute_offsets(spec["cameras"], fps_preset, df)

    mode = spec.get("mode", "highlight")
    if mode == "sequential":
        cut_specs = compute_sequential_cuts(
            srt, spec.get("remove", []), spec["default_cam"]
        )
        if not cut_specs:
            raise SystemExit(
                "sequential mode produced 0 cuts (every cue was removed?)"
            )
    else:
        cut_specs = spec["cuts"]

    cuts = expand_cuts(cut_specs, srt, padding)
    total_duration = compute_frames(cuts, cam_offsets, timebase)

    target_raw = settings.get("target_duration")
    target = parse_duration(target_raw) if target_raw is not None else None
    duration_sec = total_duration / timebase
    duration_status = None
    if target is not None:
        diff = duration_sec - target
        if diff > 0:
            duration_status = (
                f"OVER target by {diff:.1f}s "
                f"(actual {duration_sec:.1f}s, target {target:.1f}s = {target_raw!r})"
            )
        else:
            duration_status = (
                f"under target ({duration_sec:.1f}s / {target:.1f}s = {target_raw!r}, "
                f"slack {-diff:.1f}s)"
            )

    if args.analyze:
        analysis = make_analysis(
            cuts, srt, cam_offsets, anchor_cam, fps_preset, total_duration
        )
        write_output(args.output or "-", json.dumps(analysis, ensure_ascii=False, indent=2))
        return

    if not args.output:
        raise SystemExit("--output required (unless --analyze or --validate)")

    xml = emit_xml(spec, cuts, total_duration, cam_offsets)
    write_output(args.output, xml)

    if args.output != "-":
        multicam = settings.get("multicam", False) and len(spec["cameras"]) > 1
        print(f"Wrote {args.output}", file=sys.stderr)
        print(
            f"  mode={mode}, fps={seq['fps']} {df}, "
            f"cameras={list(cam_offsets.keys())}, multicam={multicam}",
            file=sys.stderr,
        )
        print(f"  offsets: {cam_offsets}", file=sys.stderr)
        print(
            f"  cuts={len(cuts)}, duration={total_duration} frames "
            f"({duration_sec:.2f}s)",
            file=sys.stderr,
        )
        if duration_status:
            print(f"  target: {duration_status}", file=sys.stderr)

    if args.verify:
        analysis = make_analysis(
            cuts, srt, cam_offsets, anchor_cam, fps_preset, total_duration
        )
        multicam = spec.get("settings", {}).get("multicam", False) and len(spec["cameras"]) > 1
        print_verify(analysis, multicam)


if __name__ == "__main__":
    main()
