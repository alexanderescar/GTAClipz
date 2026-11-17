"""
TikTok uploader.

The gate nobody warns you about: TikTok's Content Posting API has a SEPARATE
audit on top of normal app review. Until your client passes it:
  * Direct Post is forced to SELF_ONLY (private) — the call returns 200 and
    publishes something literally nobody can see
  * max 5 users per 24h, and those accounts must be set to private

So there are exactly two honest options:

  mode: "inbox_draft"  (DEFAULT — works today, no audit)
      Pushes the finished clip to your TikTok inbox as a draft. You open the
      app and tap post. ~10 seconds of human work per clip. This is the only
      route that reaches a public audience without the audit, and inbox
      drafts are explicitly exempt from the private-only restriction.

  mode: "direct_post"
      Requires TikTok's Direct Post audit. TikTok's content sharing
      guidelines exclude apps that upload to accounts you manage or that copy
      content from other platforms, so GTAClipz should NOT apply. Stay on
      inbox_draft (sandbox), with manual posting from the review page as the
      fallback.
"""
import os
import time

import requests

from common import log

API = "https://open.tiktokapis.com/v2"


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
    }


def upload(path, caption, token, mode="inbox_draft", title=None):
    size = os.path.getsize(path)
    # Single-chunk upload is allowed up to 64MB; our clips are far under that.
    init_url = (
        f"{API}/post/publish/inbox/video/init/"
        if mode == "inbox_draft"
        else f"{API}/post/publish/video/init/"
    )

    body = {
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": size,
            "total_chunk_count": 1,
        }
    }
    if mode == "direct_post":
        body["post_info"] = {
            "title": (title or caption)[:2200],
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "disable_duet": False,
            "disable_stitch": False,
            "disable_comment": False,
        }

    r = requests.post(init_url, headers=_headers(token), json=body, timeout=60)
    if r.status_code == 403 and "unaudited_client" in r.text:
        raise RuntimeError(
            "TikTok rejected direct_post: your app has not passed the Direct Post "
            "audit. Switch publish.tiktok.mode to 'inbox_draft' in settings.yml."
        )
    r.raise_for_status()
    data = r.json()["data"]
    publish_id, upload_url = data["publish_id"], data["upload_url"]

    with open(path, "rb") as fh:
        put = requests.put(
            upload_url,
            headers={
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes 0-{size - 1}/{size}",
            },
            data=fh,
            timeout=300,
        )
    put.raise_for_status()

    # poll status
    for _ in range(20):
        time.sleep(5)
        s = requests.post(
            f"{API}/post/publish/status/fetch/",
            headers=_headers(token),
            json={"publish_id": publish_id},
            timeout=30,
        ).json()
        st = s.get("data", {}).get("status")
        if st in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            log.info("tiktok OK (%s) publish_id=%s", st, publish_id)
            return {"remote_id": publish_id, "url": "", "status": st}
        if st == "FAILED":
            raise RuntimeError(f"TikTok publish failed: {s}")

    raise TimeoutError("TikTok publish status never settled")
