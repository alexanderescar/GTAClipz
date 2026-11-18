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
import re
import pathlib
import shlex
import subprocess
import textwrap

from common import ROOT, WORK, cfg, log

FONT_FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT = next((str(f) for f in [pathlib.Path.home() / ".fonts" / "Anton-Regular.ttf",
                              pathlib.Path("/usr/share/fonts/truetype/anton/Anton-Regular.ttf")]
             if f.exists()), FONT_FALLBACK)

# Words viewers shouldn't see spelled out (audio is untouched). Keeps reach and
# ad-eligibility up and stays clear of "negative light" campaign rules.
CENSOR = {
    "fuck": "F**K", "fucking": "F**KING", "fucked": "F**KED", "fuckin": "F**KIN",
    "motherfucker": "MOTHERF**KER", "shit": "SH*T", "bullshit": "BULLSH*T",
    "bitch": "B*TCH", "bitches": "B*TCHES", "asshole": "A**HOLE", "dick": "D*CK",
    "pussy": "P*SSY", "cunt": "C*NT", "nigga": "N***A", "niggas": "N***AS",
    "nigger": "N****R", "faggot": "F****T", "retard": "R****D", "damn": "DAMN",
}
FILLERS = {"uh", "um", "uhh", "umm", "hmm", "mm", "erm", "ah"}
# Words muted in the AUDIO too: strong profanity and slurs are what get Shorts
# age-restricted (Nov 18: a Lacy Short was). Milder words (shit, damn) stay.
BLEEP = {"fuck", "fucking", "fucked", "fuckin", "fucker", "fucks", "motherfucker",
         "motherfucking", "cunt", "nigga", "niggas", "nigger", "faggot", "fag",
         "retard", "retarded"}


def is_bleep(raw):
    core = raw.strip().lower()
    bare = re.sub(r"[^a-z']", "", core).strip("'")
    if bare in BLEEP:
        return True
    # Whisper sometimes writes "f***ing" itself
    return "*" in core and re.sub(r"[^a-z*]", "", core)[:1] in ("f", "c", "n")


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
def facecam_geometry(facecam, src_w, src_h, W, H):
    """Top panel height for the facecam, from the cam box's real aspect."""
    fx, fy, fw, fh = facecam
    cam_aspect = (fw * src_w) / (fh * src_h)
    top = int(round(W / cam_aspect / 2) * 2)
    return max(480, min(760, top))


def split_filter(facecam, src_w, src_h, W, H):
    """Streamer on top, gameplay big underneath (the standard clip layout)."""
    fx, fy, fw, fh = facecam
    top = facecam_geometry(facecam, src_w, src_h, W, H)
    bottom = H - top
    gw = min(src_w, int(src_h * W / bottom) // 2 * 2)      # gameplay crop width
    gx = (src_w - gw) // 2
    return (
        f"[0:v]split=2[camsrc][gamesrc];"
        f"[camsrc]crop={int(src_w*fw)//2*2}:{int(src_h*fh)//2*2}:"
        f"{int(src_w*fx)}:{int(src_h*fy)},"
        f"scale={W}:{top}:force_original_aspect_ratio=increase,crop={W}:{top}[cam];"
        f"[gamesrc]crop={gw}:{src_h}:{gx}:0,scale={W}:{bottom}[game];"
        f"[cam][game]vstack=inputs=2"
    ), top


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


def hook_overlay(hook_text, conf, W, H, center_y=None):
    """Big text card over the opening beat."""
    dur = conf["render"]["hook_card"]["duration"]
    # Anton is condensed, so more characters fit per line than with DejaVu
    lines = textwrap.wrap(hook_text.upper(), width=24 if "Anton" in FONT else 18)[:3]
    parts = []
    block = len(lines) * 96 - 18
    if center_y is not None:                       # split layout: on the seam
        base_y = max(60, center_y - block - 70)    # sits just above the seam
    else:
        # Sit the whole block just ABOVE the 16:9 gameplay band, whatever the
        # line count, so it never covers the action (a 2-line hook used to spill).
        band_top = (H - W * 9 // 16) // 2
        base_y = max(60, band_top - 40 - block)
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
def clean_word(raw):
    """C: censor, drop fillers. Returns '' to drop the word."""
    core = raw.strip()
    bare = re.sub(r"[^a-z']", "", core.lower())
    if bare in FILLERS:
        return ""
    if bare in CENSOR:
        trail = re.sub(r"^.*?([.,!?]*)$", r"\1", core)
        return CENSOR[bare] + trail
    return core.upper()


def transcribe(src, conf, caption_y=None, vocab=None, bleeps=None):
    """Word-by-word captions (current word highlighted) as an ASS file.

    A: a bigger Whisper model plus a vocabulary prompt (streamer names, GTA
       places) so names come out right.
    B: 1-3 words on screen, the spoken word in yellow, heavy outlined font.
    C: swears censored, filler words dropped.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log.warning("faster-whisper not installed; skipping captions")
        return None

    r = conf["render"]["captions"]
    model = WhisperModel(r["model"], device="cpu", compute_type="int8")
    words_hint = ", ".join(dict.fromkeys(
        [*(vocab or []), *r.get("vocabulary", [])]))
    segments, _ = model.transcribe(
        str(src), word_timestamps=True, vad_filter=True, beam_size=5,
        initial_prompt=(f"GTA 6 stream. {words_hint}." if words_hint else None))

    H = conf["render"]["height"]
    W = conf["render"]["width"]
    y = caption_y if caption_y is not None else int(H * r["y_position"])
    font = r.get("font", "Anton")
    size = r["font_size"]
    hi = r.get("highlight_colour", "&H0000E5FF")      # BGR: yellow
    ass = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {W}",
        f"PlayResY: {H}",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
        "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,"
        "BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Cap,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,"
        f"-1,0,0,0,100,100,1,0,1,7,3,5,40,40,0,1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]

    def ts(t):
        h, rem = divmod(max(0.0, t), 3600)
        m, s_ = divmod(rem, 60)
        return f"{int(h)}:{int(m):02d}:{s_:05.2f}"

    words = []
    for seg in segments:
        for w in seg.words or []:
            if bleeps is not None and is_bleep(w.word):
                bleeps.append((w.start, w.end))
            text = clean_word(w.word)
            if text:
                words.append((w.start, w.end, text))

    # group into short phrases: max N words / chars, break on pauses + punctuation
    max_words = r.get("max_words", 3)
    phrases, cur = [], []
    for i, wd in enumerate(words):
        cur.append(wd)
        nxt = words[i + 1] if i + 1 < len(words) else None
        chars = sum(len(x[2]) + 1 for x in cur)
        if (len(cur) >= max_words or chars >= r["max_chars_per_line"]
                or wd[2].endswith((".", "!", "?")) or nxt is None
                or nxt[0] - wd[1] > 0.6):
            phrases.append(cur)
            cur = []

    pos = f"{{\\an5\\pos({W // 2},{y})}}"
    n = 0
    for pi, ph in enumerate(phrases):
        nxt_start = phrases[pi + 1][0][0] if pi + 1 < len(phrases) else None
        for j, (st, en, _) in enumerate(ph):
            end = ph[j + 1][0] if j + 1 < len(ph) else en + 0.15
            if nxt_start is not None:
                end = min(end, nxt_start)      # never two lines on screen at once
            text = " ".join(
                (f"{{\\c{hi}&\\fscx110\\fscy110}}{t}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}"
                 if k == j else t)
                for k, (_, _, t) in enumerate(ph))
            ass.append(f"Dialogue: 0,{ts(st)},{ts(end)},Cap,,0,0,0,,{pos}{text}")
            n += 1

    out = WORK / "render" / (pathlib.Path(src).stem + ".ass")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(ass), encoding="utf-8")
    log.info("captions: %d words in %d phrases -> %s", n, len(phrases), out.name)
    return out


# ── Main render ────────────────────────────────────────────────────────────
def render(src, dst, hook_text=None, conf=None, start=0.0, duration=None,
           facecam=None, vocab=None):
    conf = conf or cfg()
    r = conf["render"]
    W, H = r["width"], r["height"]
    info = probe(src)
    duration = duration or min(info["duration"], cfg()["defaults"]["max_clip_seconds"]
                               if "defaults" in cfg() else 75)

    if facecam:                                    # D: cam on top, game below
        chain, seam = split_filter(facecam, info["width"], info["height"], W, H)
        cap_y, hook_center = seam, seam
    else:
        chain = layout_filter(r["layout"], W, H)
        band_bottom = (H + W * 9 // 16) // 2
        cap_y, hook_center = band_bottom + 90, None

    bleeps = []
    sub = transcribe(src, conf, caption_y=cap_y, vocab=vocab, bleeps=bleeps) \
        if r["captions"]["enabled"] else None

    post = []
    if hook_text and r["hook_card"]["enabled"]:
        post.append(hook_overlay(hook_text, conf, W, H, center_y=hook_center))
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
        if bleeps and r.get("bleep", True):
            # times are from the source; the output starts at `start`
            mutes = ",".join(
                f"volume=enable='between(t,{max(0.0, a - start - 0.05):.2f},"
                f"{b - start + 0.05:.2f})':volume=0" for a, b in bleeps
                if b - start > 0)
            if mutes:
                af = mutes + "," + af
                log.info("audio: muted %d strong words", len(bleeps))
        cmd += ["-af", af, "-c:a", "aac", "-b:a", r["audio_bitrate"]]
    else:
        cmd += ["-an"]
    cmd += [str(dst)]

    run(cmd)
    log.info("rendered -> %s (%.1f MB)", dst, pathlib.Path(dst).stat().st_size / 1e6)
    return dst
