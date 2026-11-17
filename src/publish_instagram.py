"""
Instagram Reels uploader (Graph API, container flow).

Two things people get wrong:
  1. You do NOT need Meta App Review to publish to an account you own.
     Development mode + your own account assigned an Instagram Tester role
     is enough. App Review / Advanced Access is only for publishing to
     accounts you don't control.
  2. The API cannot accept a file upload. It pulls from a PUBLIC video_url.
     That's why this pipeline ships an R2 uploader — the clip has to be
     reachable on the open internet for the ~60s Meta takes to ingest it.

Requirements: Instagram BUSINESS or Creator account (not personal),
linked to a Facebook Page.
Limit: 100 API-published posts per account per rolling 24h (Meta's own docs
contradict themselves between 50 and 100 — query content_publishing_limit
for your real number).
Reels: 3s-15min, MP4/MOV, <=300MB, 9:16.
"""
import time

import requests

from common import log

GRAPH = "https://graph.facebook.com/v21.0"


def publish(video_url, caption, ig_user_id, token, share_to_feed=True):
    # 1. create container
    r = requests.post(
        f"{GRAPH}/{ig_user_id}/media",
        data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption[:2190],
            "share_to_feed": "true" if share_to_feed else "false",
            "access_token": token,
        },
        timeout=60,
    )
    r.raise_for_status()
    container = r.json()["id"]
    log.info("  ig container %s created", container)

    # 2. poll until FINISHED (Meta is downloading + transcoding your file)
    for attempt in range(40):
        time.sleep(6)
        s = requests.get(
            f"{GRAPH}/{container}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=30,
        ).json()
        code = s.get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise RuntimeError(f"IG container error: {s.get('status')}")
        log.info("  ig container %s (%ds)", code, (attempt + 1) * 6)
    else:
        raise TimeoutError("IG container never reached FINISHED")

    # 3. publish
    p = requests.post(
        f"{GRAPH}/{ig_user_id}/media_publish",
        data={"creation_id": container, "access_token": token},
        timeout=60,
    )
    p.raise_for_status()
    mid = p.json()["id"]
    log.info("instagram OK -> media %s", mid)
    return {"remote_id": mid, "url": f"https://www.instagram.com/reel/{mid}/"}


def quota_remaining(ig_user_id, token):
    """Ask Meta for your real publishing quota rather than trusting the docs."""
    r = requests.get(
        f"{GRAPH}/{ig_user_id}/content_publishing_limit",
        params={"fields": "config,quota_usage", "access_token": token},
        timeout=30,
    ).json()
    d = (r.get("data") or [{}])[0]
    used = d.get("quota_usage", 0)
    total = (d.get("config") or {}).get("quota_total", 100)
    return total - used, total
