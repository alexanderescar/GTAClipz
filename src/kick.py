"""Kick VOD discovery — finds a streamer's past broadcasts without you pasting links.

Kick sits behind Cloudflare, which turns away plain Python requests. curl_cffi
sends the same handshake a real Chrome does, which is also what yt-dlp uses to
download Kick videos. If Kick still refuses, the robot falls back to the manual
paste in config/vod_queue.yml and says so in the log.
"""
from common import log

API = "https://kick.com/api/v2/channels/{slug}/videos"


def _get(url):
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", timeout=30)
    except ImportError:
        import requests
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code != 200:
        raise RuntimeError(f"Kick API {r.status_code}")
    return r.json()


def slug_of(source):
    link = source.get("kick_slug") or source.get("link", "")
    return link.rstrip("/").split("/")[-1]


def videos(slug):
    """Past broadcasts, newest first: id, url, title, created_at, duration_s, is_live."""
    out = []
    for v in _get(API.format(slug=slug)) or []:
        uuid = (v.get("video") or {}).get("uuid")
        if not uuid:
            continue
        created = (v.get("start_time") or v.get("created_at") or "").replace(" ", "T")
        if created and "+" not in created and not created.endswith("Z"):
            created += "+00:00"          # Kick reports UTC without saying so
        out.append({
            "id": uuid,
            "url": f"https://kick.com/{slug}/videos/{uuid}",
            "title": v.get("session_title") or "",
            "created_at": created,
            "duration_s": (v.get("duration") or 0) / 1000,
            "is_live": bool(v.get("is_live")),
        })
    return out


def selftest():
    from common import sources
    for s in sources():
        if s.get("platform") != "kick":
            continue
        slug = slug_of(s)
        try:
            vs = videos(slug)
            log.info("Kick %s: OK — %d past broadcasts visible", slug, len(vs))
            for v in vs[:5]:
                log.info("   %s | %s | %.0f min | %s", v["created_at"][:16],
                         v["title"][:50], v["duration_s"] / 60, v["url"])
        except Exception as e:  # noqa: BLE001
            log.error("Kick %s: FAILED (%s) — paste the VOD link into "
                      "config/vod_queue.yml instead", slug, e)
