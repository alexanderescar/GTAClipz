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

Requirements: Instagram Creator (recommended) or Business account, not
personal. Creator gets monetization and the full music library; both can
publish Reels through the API.
Limit: 100 API-published posts per account per rolling 24h (Meta's own docs
contradict themselves between 50 and 100 — query content_publishing_limit
for your real number).
Reels: 3s-15min, MP4/MOV, <=300MB, 9:16.
"""
import time

import requests

from common import log

GRAPH_FB = "https://graph.facebook.com/v21.0"
GRAPH_IG = "https://graph.instagram.com/v21.0"


def graph_for(token):
    """Meta issues two kinds of token, each tied to its own API host:
      EAA...  Facebook/Unified-login token (all new apps since late Oct 2026)
              -> graph.facebook.com
      IG...   Instagram-login user token (older apps) -> graph.instagram.com
    Sending a token to the wrong host fails with 'Cannot parse access token'."""
    return GRAPH_FB if token.startswith("EAA") else GRAPH_IG


def publish(video_url, caption, ig_user_id, token, share_to_feed=True):
    GRAPH = graph_for(token)
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
    GRAPH = graph_for(token)
    r = requests.get(
        f"{GRAPH}/{ig_user_id}/content_publishing_limit",
        params={"fields": "config,quota_usage", "access_token": token},
        timeout=30,
    ).json()
    d = (r.get("data") or [{}])[0]
    used = d.get("quota_usage", 0)
    total = (d.get("config") or {}).get("quota_total", 100)
    return total - used, total


def ig_user_id():
    """The account ID is public, so it lives in settings.yml. The IG_USER_ID
    secret is only used if the config is empty and the secret is a number."""
    import os
    from common import cfg
    v = str(cfg()["publish"]["instagram"].get("ig_user_id") or "").strip()
    if v.isdigit():
        return v
    env = (os.environ.get("IG_USER_ID") or "").strip()
    return env if env.isdigit() else None


def selftest():
    """Read-only check: token valid, account reachable, daily post quota."""
    import os
    uid, tok = ig_user_id(), os.environ.get("IG_ACCESS_TOKEN")
    if not (uid and tok):
        print("  IG_USER_ID / IG_ACCESS_TOKEN not set")
        return
    host = graph_for(tok)
    uid = uid.strip()
    print(f"  IG_USER_ID length {len(uid)}, digits only: {uid.isdigit()}")
    if tok.startswith("EAA"):
        dbg = requests.get("https://graph.facebook.com/debug_token", params={
            "input_token": tok, "access_token": tok}, timeout=30).json()
        info = dbg.get("data") or {}
        print(f"  token valid: {info.get('is_valid')} | type: {info.get('type')} | "
              f"app: {info.get('application')}")
        for g in info.get("granular_scopes", []):
            print(f"  scope {g.get('scope'):<32} assets: {g.get('target_ids', 'all')}")
        if dbg.get("error"):
            print(f"  debug_token error: {dbg['error'].get('message')}")
        me = requests.get(f"{host}/me", params={"fields": "id,name",
                          "access_token": tok}, timeout=30).json()
        print(f"  /me -> {me}")
    print(f"  token type: {'Unified/Facebook (EAA)' if tok.startswith('EAA') else 'Instagram login'}"
          f" -> {host.split('/')[2]}")
    r = requests.get(f"{host}/{uid}", params={
        "fields": "username,followers_count,media_count",
        "access_token": tok}, timeout=30)
    if r.status_code != 200:
        print(f"  account lookup FAILED: {r.text[:400]}")
        return
    d = r.json()
    print(f"  account: @{d.get('username')} | type {d.get('account_type', '?')} | "
          f"{d.get('followers_count', '?')} followers | {d.get('media_count', '?')} posts")
    try:
        left, total = quota_remaining(uid, tok)
        print(f"  publishing quota: {left}/{total} posts left in the rolling 24h")
    except Exception as e:  # noqa: BLE001
        print(f"  quota check failed (may be missing publish permission): {e}")
    dbg = requests.get("https://graph.facebook.com/debug_token", params={
        "input_token": tok, "access_token": tok}, timeout=30)
    if dbg.ok and tok.startswith("EAA"):
        info = dbg.json().get("data", {})
        exp = info.get("expires_at")
        print(f"  token expires: {'never' if not exp else __import__('datetime').datetime.utcfromtimestamp(exp).date()}")
        print(f"  scopes: {', '.join(info.get('scopes', []))}")
