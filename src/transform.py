"""
Turn a 16:9 clip into a platform-ready 9:16 vertical short.

Pipeline per clip:
  1. trim to the scored window
  2. 9:16 reframe (blur-pad keeps the whole gameplay frame visible —
     center-crop throws away the HUD, minimap and half the action)
  3. optional 1.2s hook card, which also pushes any graphic gameplay past
     YouTube's first-7-second ad-eligibility boundary
  4. burned-in captions from faster-whisper (word-timed, karaoke style)
  5. your own watermark (your branding is fine; a TikTok logo is not)
  6. loudnorm to -14 LUFS

Everything here is ffmpeg + faster-whisper. No paid service, no API key.
"""
import json
import pathlib
import shlex
import subprocess
import textwrap

from common import ROOT, WORK, cfg, log

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def run(cmd, **kw):
    log.debug("$ %s", " ".join(shlex.quote(c) for c in cmd))
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def probe(path):
    out = run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)]
    ).stdout
    d = json.loads(out)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    return {
        "width": int(v["width"]),
        "height": int(v["height"]),
        "duration": float(d["format"].get("duration", 0) or 0),
        "has_audio": any(s["codec_type"] == "audio" for s in d["streams"]),
    }


# ── Layouts ────────────────────────────────────────────────────────────────
def layout_filter(layout, W, H):
    """Return the ffmpeg filter chain that maps 16:9 -> 9:16."""
    if layout == "center_crop":
        # Fills the frame but discards ~45% of the image horizontally.
        return f"scale={W}:-2,crop={W}:{H}"

    if layout == "gameplay_top":
        # Gameplay pinned to the upper third, bottom two-thirds for captions.
        # Reads well for commentary formats.
        return (
            f"[0:v]scale={W}:-2[fg];"
            f"color=c=0x0a0a0f:s={W}x{H}:d=1[bg];"
            f"[bg][fg]overlay=(W-w)/2:{int(H*0.12)}"
        )

    # Default: blur_pad. Full frame visible, blurred fill top and bottom.
    # This is what nearly every successful gaming clip channel uses.
    return (
        f"[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},gblur=sigma=28,eq=brightness=-0.12[bg];"
        f"[fgsrc]scale={W}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )


def esc(text):
    """Escape text for ffmpeg drawtext."""
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "’")
        .replace("%", "\\%")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def hook_overlay(hook_text, conf, W, H):
    """Big text card over the opening beat."""
    dur = conf["render"]["hook_card"]["duration"]
    lines = textwrap.wrap(hook_text.upper(), width=18)[:3]
    parts = []
    # Vertically centre the hook block around 1/3 height,
    # whatever the line count, so it never drifts onto the gameplay band.
    base_y = int(H * 0.33) - (len(lines) - 1) * 48
    for i, line in enumerate(lines):
        y = base_y + i * 96
        parts.append(
            f"drawtext=fontfile={FONT}:text='{esc(line)}'"
            f":fontcolor=white:fontsize=78:borderw=6:bordercolor=black@0.9"
            f":x=(w-text_w)/2:y={y}"
            f":enable='lt(t,{dur})'"
        )
    # Dim the footage behind the hook so the text pops
    parts.insert(
        0,
        f"drawbox=x=0:y=0:w={W}:h={H}:color=black@0.55:t=fill"
        f":enable='lt(t,{dur})'",
    )
    return ",".join(parts)


def watermark_overlay(conf, W, H):
    wm = conf["render"]["watermark"]
    if not wm.get("enabled"):
        return None
    return (
        f"drawtext=fontfile={FONT}:text='{esc(wm['text'])}'"
        f":fontcolor=white@{wm['opacity']}:fontsize=40"
        f":borderw=3:bordercolor=black@0.5"
        f":x=w-text_w-44:y={int(H*0.055)}"
    )


# ── Captions ───────────────────────────────────────────────────────────────
def transcribe(src, conf):
    """Word-level timestamps via faster-whisper. Returns an ASS subtitle path."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log.warning("faster-whisper not installed; skipping captions")
        return None

    r = conf["render"]["captions"]
    model = WhisperModel(r["model"], device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(src), word_timestamps=True, vad_filter=True)

    H = conf["render"]["height"]
    W = conf["render"]["width"]
    y = int(H * r["y_position"])
    ass = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {W}",
        f"PlayResY: {H}",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,"
        "Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Pop,DejaVu Sans,{r['font_size']},&H00FFFFFF,&H00000000,"
        f"&H64000000,-1,0,0,0,100,100,0,0,1,5,2,2,60,60,{H-y},1",
        f"Style: Hot,DejaVu Sans,{r['font_size']},&H0000E5FF,&H00000000,"
        f"&H64000000,-1,0,0,0,100,100,0,0,1,5,2,2,60,60,{H-y},1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]

    def ts(t):
        h, rem = divmod(max(0.0, t), 3600)
        m, s = divmod(rem, 60)
        return f"{int(h)}:{int(m):02d}:{s:05.2f}"

    # Group words into short phrases so the line never overruns the frame
    buf, start = [], None
    for seg in segments:
        for w in seg.words or []:
            if start is None:
                start = w.start
            buf.append(w)
            if sum(len(x.word) for x in buf) >= r["max_chars_per_line"] or w.word.strip().endswith((".", "!", "?")):
                text = "".join(x.word for x in buf).strip()
                style = "Hot" if any(
                    k in text.lower() for k in ("insane", "wtf", "no way", "what")
                ) else "Pop"
                ass.append(
                    f"Dialogue: 0,{ts(start)},{ts(w.end)},{style},,0,0,0,,{text.upper()}"
                )
                buf, start = [], None
    if buf:
        text = "".join(x.word for x in buf).strip()
        ass.append(
            f"Dialogue: 0,{ts(start)},{ts(buf[-1].end)},Pop,,0,0,0,,{text.upper()}"
        )

    out = WORK / "render" / (pathlib.Path(src).stem + ".ass")
    out.write_text("\n".join(ass), encoding="utf-8")
    log.info("captions: %d lines -> %s", len(ass) - 14, out.name)
    return out


# ── Main render ────────────────────────────────────────────────────────────
def render(src, dst, hook_text=None, conf=None, start=0.0, duration=None):
    conf = conf or cfg()
    r = conf["render"]
    W, H = r["width"], r["height"]
    info = probe(src)
    duration = duration or min(info["duration"], cfg()["defaults"]["max_clip_seconds"]
                               if "defaults" in cfg() else 75)

    sub = transcribe(src, conf) if r["captions"]["enabled"] else None

    chain = layout_filter(r["layout"], W, H)
    post = []
    if hook_text and r["hook_card"]["enabled"]:
        post.append(hook_overlay(hook_text, conf, W, H))
    if sub:
        post.append(f"subtitles={shlex.quote(str(sub))}")
    wm = watermark_overlay(conf, W, H)
    if wm:
        post.append(wm)
    post.append(f"fps={r['fps']},format=yuv420p")

    # Stitch: layouts that already use named pads end with an overlay label
    vf = chain + "," + ",".join(post) if not chain.startswith("[0:v]") else \
         chain + "," + ",".join(post)

    cmd = [
        "ffmpeg", "-y", "-ss", str(start), "-t", str(duration), "-i", str(src),
        "-filter_complex", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", r["video_bitrate"],
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
    ]
    if info["has_audio"]:
        af = "loudnorm=I=-14:TP=-1.5:LRA=11" if r["loudnorm"] else "anull"
        cmd += ["-af", af, "-c:a", "aac", "-b:a", r["audio_bitrate"]]
    else:
        cmd += ["-an"]
    cmd += [str(dst)]

    run(cmd)
    log.info("rendered -> %s (%.1f MB)", dst, pathlib.Path(dst).stat().st_size / 1e6)
    return dst
