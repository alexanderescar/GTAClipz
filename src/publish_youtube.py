"""
YouTube Shorts uploader.

IMPORTANT (changed 2026-11-02): Google reversed the 2020 rule that forced
videos uploaded from unverified API projects to private. Per the YouTube Data
API revision history, videos.insert from an unverified project is no longer
restricted to private viewing, and privacyStatus now defaults to public when
unspecified. Some older doc pages still carry the stale warning — the revision
history is authoritative and newer.

Quota: videos.insert sits in its own bucket — 100 uploads/day per project,
1 unit each — separate from the 10,000-unit general pool. You will hit
YouTube's own per-channel daily upload limit long before you hit the API's.

A Short is just a video that is vertical and <= 3 minutes. There is no
"shorts" flag; put #Shorts in the title or description as a hint.
"""
import pathlib

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from common import ROOT, log

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN = ROOT / "state" / "youtube_token.json"


def client():
    if not TOKEN.exists():
        raise SystemExit(
            "No YouTube token. Run: python scripts/auth_youtube.py  (once, locally)"
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload(path, title, description, tags, conf, publish_at=None):
    yt = conf["publish"]["youtube"]
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": tags[:40],
            "categoryId": yt["category_id"],
        },
        "status": {
            "privacyStatus": yt["privacy"],
            "selfDeclaredMadeForKids": yt["made_for_kids"],
        },
    }
    if publish_at:
        # Scheduling requires privacyStatus=private until publishAt fires
        body["status"]["privacyStatus"] = "private"
        body["status"]["publishAt"] = publish_at

    media = MediaFileUpload(str(path), chunksize=4 * 1024 * 1024, resumable=True,
                            mimetype="video/mp4")
    req = client().videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = req.next_chunk()
        if status:
            log.info("  youtube upload %d%%", int(status.progress() * 100))

    vid = response["id"]
    url = f"https://youtube.com/shorts/{vid}"
    log.info("youtube OK -> %s", url)
    return {"remote_id": vid, "url": url}
