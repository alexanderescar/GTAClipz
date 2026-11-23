"""Catch nudity and sexual content before a clip reaches Alex's review list.

Why: GTA 6 has strip clubs, sex scenes and nude NPCs. TikTok and YouTube gave
the account strikes for them (Nov 2026), and Alex spent a lot of time rejecting
them by hand. Every clip is checked; a flagged clip never reaches review.

Two checks:
  1. Picture: NudeNet (an open-source nudity detector) looks at 2 frames per
     second of the clip.
       - exposed breasts / genitals / buttocks in any frame -> drop
       - lingerie / bikini-level content (covered breasts, buttocks) in a
         sizeable share of frames -> drop (strip-club dancing)
  2. Words: what the streamer says (from the caption transcript), e.g.
     "strip club", "lap dance", "naked".

Thresholds live in settings.yml under safety.nsfw so they can be tuned.
"""
import re
import subprocess

from common import log

EXPOSED = {"FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED",
           "BUTTOCKS_EXPOSED", "ANUS_EXPOSED"}
SUGGESTIVE = {"FEMALE_BREAST_COVERED", "BUTTOCKS_COVERED", "FEMALE_GENITALIA_COVERED"}
WORDS = re.compile(
    r"\b(strip ?clubs?|strippers?|lap ?dances?|twerk\w*|titt\w*|tits|boob\w*|naked|nudes?|"
    r"nudity|porn\w*|sex\w*|hookers?|prostitut\w*|horny|thong|lingerie)\b", re.I)

_detector = None


def _det():
    global _detector
    if _detector is None:
        from nudenet import NudeDetector
        _detector = NudeDetector()
    return _detector


def _frames(src, start, duration, fps, long_side=640):
    """Yield BGR frames (numpy) from the clip window, longest side scaled to 640."""
    import numpy as np
    from transform import probe
    info = probe(src)
    sw, sh = info.get("width") or 1920, info.get("height") or 1080
    k = long_side / max(sw, sh)
    w, h = int(sw * k) // 2 * 2, int(sh * k) // 2 * 2
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{duration:.2f}", "-i", str(src),
         "-vf", f"fps={fps},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        capture_output=True, timeout=300)
    buf = p.stdout
    n = len(buf) // (w * h * 3)
    for i in range(n):
        yield np.frombuffer(buf, np.uint8, w * h * 3, i * w * h * 3).reshape(h, w, 3)


def scan_picture(src, start=0.0, duration=None, conf=None):
    """Return (flag_reason or None, stats)."""
    s = ((conf or {}).get("safety") or {}).get("nsfw") or {}
    fps = s.get("fps", 2)
    hard_min = s.get("exposed_min_score", 0.45)
    soft_min = s.get("suggestive_min_score", 0.5)
    soft_share = s.get("suggestive_frame_share", 0.2)
    if duration is None:
        from transform import probe
        duration = probe(src)["duration"] - start
    frames = exposed = suggestive = 0
    worst = ("", 0.0)
    det = _det()
    for img in _frames(src, start, duration, fps):
        frames += 1
        found = det.detect(img)
        hit_soft = False
        for d in found:
            c, sc = d["class"], d["score"]
            if c in EXPOSED and sc >= hard_min:
                exposed += 1
                if sc > worst[1]:
                    worst = (c, sc)
            elif c in SUGGESTIVE and sc >= soft_min:
                hit_soft = True
        suggestive += hit_soft
    stats = {"frames": frames, "exposed": exposed, "suggestive": suggestive,
             "worst": f"{worst[0]} {worst[1]:.2f}" if worst[0] else ""}
    if exposed:
        return f"nudity ({exposed} frames, worst {stats['worst']})", stats
    if frames and suggestive / frames >= soft_share:
        return f"sexual/suggestive content in {suggestive}/{frames} frames", stats
    return None, stats


def scan_words(text):
    hits = sorted({m.group(0).lower() for m in WORDS.finditer(text or "")})
    return (f"said: {', '.join(hits)}" if hits else None), hits


def check(src, start, duration, transcript, conf):
    """Return a reason string if the clip should be dropped, else None."""
    if not ((conf.get("safety") or {}).get("nsfw") or {}).get("enabled", True):
        return None
    why, hits = scan_words(transcript)
    if why:
        return why
    try:
        why, stats = scan_picture(src, start, duration, conf)
    except Exception as e:  # noqa: BLE001  -- a broken check must not block the factory
        log.warning("nsfw: picture check failed (%s); letting the clip through", e)
        return None
    log.info("nsfw: %s", stats)
    return why
