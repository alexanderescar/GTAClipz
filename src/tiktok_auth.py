"""
TikTok token keeper.

TikTok access tokens die after 24 hours. The refresh token lasts 365 days,
but TikTok may hand back a NEW refresh token on each refresh, and the old one
then stops working. GitHub Actions can't rewrite its own secrets, so the
latest tokens are stored encrypted in state/tiktok.enc (committed with the
rest of the state). The encryption key is derived from TIKTOK_CLIENT_SECRET,
which only exists in GitHub's secret vault, so the file is useless to anyone
reading the public repo.

Secrets needed: TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, TIKTOK_REFRESH_TOKEN
(the refresh token from tiktok_auth_local.py; only used to seed the first run).
"""
import base64
import hashlib
import json
import os
import time

import requests
from cryptography.fernet import Fernet, InvalidToken

from common import ROOT, log

TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
STATE = ROOT / "state" / "tiktok.enc"


def _fernet(secret):
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(
        ("gtaclipz-tiktok:" + secret).encode()).digest()))


def _load(secret):
    if not STATE.exists():
        return {}
    try:
        return json.loads(_fernet(secret).decrypt(STATE.read_bytes()))
    except (InvalidToken, ValueError):
        log.warning("tiktok.enc unreadable (client secret changed?) — reseeding")
        return {}


def _save(secret, data):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_bytes(_fernet(secret).encrypt(json.dumps(data).encode()))


def access_token():
    key, secret = os.environ.get("TIKTOK_CLIENT_KEY"), os.environ.get("TIKTOK_CLIENT_SECRET")
    if not (key and secret):
        raise RuntimeError("TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET not set")
    data = _load(secret)
    if data.get("access_token") and data.get("expires_at", 0) > time.time() + 1800:
        return data["access_token"]

    seed = os.environ.get("TIKTOK_REFRESH_TOKEN", "")
    candidates = [t for t in (data.get("refresh_token"), seed) if t]
    if not candidates:
        raise RuntimeError("No TikTok refresh token — run tiktok_auth_local.py")
    last = None
    for rt in dict.fromkeys(candidates):         # stored one first, then the seed
        r = requests.post(TOKEN_URL, timeout=30, data={
            "client_key": key, "client_secret": secret,
            "grant_type": "refresh_token", "refresh_token": rt},
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        j = r.json()
        if r.ok and j.get("access_token"):
            _save(secret, {
                "access_token": j["access_token"],
                "expires_at": time.time() + int(j.get("expires_in", 86400)),
                "refresh_token": j.get("refresh_token", rt),
                "refresh_expires_at": time.time() + int(j.get("refresh_expires_in", 31536000)),
                "open_id": j.get("open_id"), "scope": j.get("scope"),
            })
            log.info("tiktok token refreshed")
            return j["access_token"]
        last = j
    raise RuntimeError(f"TikTok refresh failed: {last.get('error_description') or last}")


def selftest():
    try:
        tok = access_token()
    except RuntimeError as e:
        print(f"  {e}")
        return
    secret = os.environ["TIKTOK_CLIENT_SECRET"]
    d = _load(secret)
    days = (d.get("refresh_expires_at", 0) - time.time()) / 86400
    print(f"  token refresh: OK | scopes: {d.get('scope')} | sign-in valid ~{days:.0f} more days")
    u = requests.get("https://open.tiktokapis.com/v2/user/info/",
                     params={"fields": "open_id,display_name"},
                     headers={"Authorization": f"Bearer {tok}"}, timeout=30).json()
    print(f"  account: {(u.get('data') or {}).get('user', {}).get('display_name')} "
          f"| error: {(u.get('error') or {}).get('code')}")
    c = requests.post("https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
                      headers={"Authorization": f"Bearer {tok}",
                               "Content-Type": "application/json; charset=UTF-8"},
                      timeout=30).json()
    cd = c.get("data") or {}
    print(f"  posting check: {(c.get('error') or {}).get('code')} | "
          f"privacy options: {cd.get('privacy_level_options')} | "
          f"max video length: {cd.get('max_video_post_duration_sec')}s")
