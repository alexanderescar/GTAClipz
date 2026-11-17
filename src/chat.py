"""
Download and parse chat replays.

Twitch  -> TwitchDownloaderCLI `chatdownload` (free, open source). Supports
           -b/-e trimming, so for a VOD that's still growing (a marathon
           stream) we only fetch the new part each run.
YouTube -> yt-dlp `--sub-langs live_chat`, which saves the live-chat replay of
           a finished stream as JSON lines.
Kick    -> no public chat replay. Long-form falls back to audio peaks.
"""
import gzip
import json
import os
import pathlib
import shutil
import subprocess

from common import WORK, log

TDCLI = os.environ.get("TDCLI", shutil.which("TwitchDownloaderCLI") or
                       str(pathlib.Path.home() / "bin" / "TwitchDownloaderCLI"))


def twitch_chat(vod_id, begin=None, end=None):
    out = WORK / "render" / f"chat_{vod_id}_{int(begin or 0)}.json"
    cmd = [TDCLI, "chatdownload", "-u", str(vod_id), "-o", str(out),
           "--collision", "Overwrite", "-t", "4"]
    if begin is not None:
        cmd += ["-b", str(int(begin))]
    if end is not None:
        cmd += ["-e", str(int(end))]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=1500)
    comments = parse_twitch_json(out)
    out.unlink(missing_ok=True)
    return comments


def parse_twitch_json(path):
    raw = pathlib.Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    data = json.loads(raw)
    out = []
    for c in data.get("comments", []):
        t = c.get("content_offset_seconds")
        msg = (c.get("message") or {}).get("body") or ""
        if t is not None:
            out.append((float(t), msg))
    log.info("twitch chat: %d messages", len(out))
    return out


def youtube_chat(url):
    stem = WORK / "render" / "ytchat"
    subprocess.run(
        ["yt-dlp", "--skip-download", "--write-subs", "--sub-langs", "live_chat",
         "-o", str(stem) + ".%(ext)s", url],
        check=True, capture_output=True, text=True, timeout=1500,
    )
    files = list((WORK / "render").glob("ytchat*.live_chat.json"))
    if not files:
        log.warning("no YouTube live chat replay for %s", url)
        return []
    comments = parse_youtube_jsonl(files[0])
    for f in files:
        f.unlink(missing_ok=True)
    return comments


def parse_youtube_jsonl(path):
    out = []
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
            r = d["replayChatItemAction"]
            t = int(r["videoOffsetTimeMsec"]) / 1000.0
            for a in r.get("actions", []):
                item = (a.get("addChatItemAction") or {}).get("item") or {}
                m = item.get("liveChatTextMessageRenderer")
                if not m:
                    continue
                runs = (m.get("message") or {}).get("runs", [])
                text = "".join(x.get("text") or
                               (x.get("emoji") or {}).get("shortcuts", [""])[0]
                               for x in runs)
                out.append((t, text))
        except (KeyError, ValueError, TypeError):
            continue
    log.info("youtube chat: %d messages", len(out))
    return out
