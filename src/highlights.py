"""
Find the moments worth clipping inside a long video.

Two detectors, both free and both CPU-cheap:

1. CHAT SPIKES (primary). When something wild happens on stream, chat explodes.
   We count messages in 10-second bins, compare each bin with the median of
   the previous ~5 minutes, and flag bins several times above that baseline.
   Messages carrying hype tokens (KEKW, LMAO, W, ???, 💀 ...) count extra.
   Chat reacts AFTER the moment, so each window starts ~30s before the spike.

2. AUDIO PEAKS (fallback + confirmation). For videos with no chat replay
   (Kick, edited YouTube videos), we measure loudness once per second and look
   for bursts — screaming, laughing, explosions. For chat spikes we also
   measure the audio of the cut window, and a real moment gets a score boost.

Output for both: a list of Window(start, end, score, ratio, top_chat).
"""
import collections
import dataclasses
import math
import re
import statistics
import subprocess

HYPE = {
    "kekw", "lul", "lulw", "omegalul", "lmao", "lmfao", "lol", "w", "ww", "www",
    "wtf", "pog", "pogu", "poggers", "icant", "nah", "nahh", "nahhh", "clip",
    "clip it", "no way", "noway", "bro", "holy", "omg", "aware", "monkas",
    "💀", "😭", "😂", "🔥",
}
RE_TOKEN = re.compile(r"[\w']+|[^\w\s]", re.UNICODE)


@dataclasses.dataclass
class Window:
    start: float
    end: float
    score: float
    ratio: float = 0.0
    msgs: int = 0
    top_chat: str = ""
    kind: str = "vod_chat"          # vod_chat | vod_audio
    audio_lift: float = 0.0

    @property
    def length(self):
        return self.end - self.start


def _hype_weight(text):
    t = text.lower().strip()
    if not t:
        return 0.0
    if "?" * 3 in t or t in HYPE:
        return 1.0
    toks = set(RE_TOKEN.findall(t))
    return 0.6 if toks & HYPE else 0.0


def _top_phrase(msgs, n=1):
    """Most repeated short message in a window: what chat was SAYING."""
    c = collections.Counter(
        m.strip().upper() for m in msgs if 0 < len(m.strip()) <= 24
    )
    if not c:
        return ""
    phrase, k = c.most_common(1)[0]
    return f"{phrase} ×{k}" if k > 2 else ""


def chat_spikes(comments, duration, *, bin_s=10, baseline_s=300,
                min_ratio=3.0, min_msgs=12, top_n=12, min_gap_s=120,
                pre_roll=30, clip_len=45, max_len=59, offset=0.0):
    """
    comments: iterable of (seconds_into_vod, message_text)
    Returns Windows sorted by score, best first, non-overlapping.
    """
    nbins = max(1, int(math.ceil(duration / bin_s)))
    weight = [0.0] * nbins
    count = [0] * nbins
    texts = collections.defaultdict(list)
    for t, msg in comments:
        if t < 0 or t >= duration:
            continue
        b = int(t // bin_s)
        count[b] += 1
        weight[b] += 1.0 + _hype_weight(msg)
        texts[b].append(msg)

    look = max(3, baseline_s // bin_s)
    cands = []
    for b in range(nbins):
        prev = weight[max(0, b - look):b]
        if len(prev) < 3:
            continue
        base = max(1.0, statistics.median(prev))
        ratio = weight[b] / base
        if ratio >= min_ratio and count[b] >= min_msgs:
            cands.append((ratio * math.log1p(weight[b]), ratio, b))

    cands.sort(reverse=True)
    picked = []
    for score, ratio, b in cands:
        t = b * bin_s
        if any(abs(t - (p.start - offset + pre_roll)) < min_gap_s for p in picked):
            continue
        start = max(0.0, t - pre_roll)
        end = min(duration, start + min(clip_len, max_len))
        around = texts[b] + texts.get(b + 1, [])
        picked.append(Window(start + offset, end + offset, round(score, 2),
                             round(ratio, 2), count[b], _top_phrase(around)))
        if len(picked) >= top_n:
            break
    return picked


# ── audio ────────────────────────────────────────────────────────────────────
def loudness_per_second(path_or_url, start=None, duration=None):
    """RMS level (dB) for each second of audio. Works on files or stream URLs."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start is not None:
        cmd += ["-ss", str(start)]
    if duration is not None:
        cmd += ["-t", str(duration)]
    cmd += ["-i", str(path_or_url), "-vn", "-af",
            "aresample=8000,asetnsamples=n=8000,astats=metadata=1:reset=1,"
            "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
            "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    levels = []
    for line in out.splitlines():
        if "RMS_level=" in line:
            v = line.split("=", 1)[1].strip()
            try:
                levels.append(max(-90.0, float(v)))
            except ValueError:
                levels.append(-90.0)
    return levels


def audio_peaks(levels, *, baseline_s=120, min_lift_db=9.0, top_n=10,
                min_gap_s=120, pre_roll=12, clip_len=40, max_len=59, offset=0.0):
    """Bursts of loudness well above the surrounding couple of minutes."""
    n = len(levels)
    cands = []
    for i in range(n):
        prev = levels[max(0, i - baseline_s):i]
        if len(prev) < 20:
            continue
        base = statistics.median(prev)
        # require ~3s of sustained loudness so a single bang doesn't trigger
        burst = levels[i:i + 3]
        if len(burst) < 3:
            continue
        lift = min(burst) - base
        if lift >= min_lift_db:
            cands.append((lift, i))
    cands.sort(reverse=True)
    picked = []
    for lift, i in cands:
        if any(abs(i - (p.start - offset + pre_roll)) < min_gap_s for p in picked):
            continue
        start = max(0, i - pre_roll)
        end = min(n, start + min(clip_len, max_len))
        picked.append(Window(start + offset, end + offset, round(lift, 2),
                             kind="vod_audio", audio_lift=round(lift, 2)))
        if len(picked) >= top_n:
            break
    return picked


def audio_lift(path):
    """For an already-cut window: how far its loud part rises above its quiet
    part. Real moments usually spike; a flat window is a weaker clip."""
    lv = [x for x in loudness_per_second(path) if x > -80]
    if len(lv) < 5:
        return 0.0
    lv.sort()
    return round(lv[int(len(lv) * 0.9)] - lv[len(lv) // 2], 2)
