"""
Discover candidate GTA 6 clips, score them, and write them to state.

Sources:
  * Twitch Helix  /clips?game_id=...  (free, official, no approval needed)
  * Local folder  (your own console captures)

Nothing is downloaded here. This stage is cheap and runs often.
"""
import datetime as dt
import pathlib
import re

import requests

from common import WORK, cfg, db, log, sources

HELIX = "https://api.twitch.tv/helix"


# ── Twitch auth: app access token, client-credentials. No user login. ───────
def twitch_token(client_id, client_secret):
    r = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def helix(path, token, client_id, **params):
    r = requests.get(
        f"{HELIX}/{path}",
        headers={"Client-Id": client_id, "Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    if r.status_code == 429:
        log.warning("Twitch rate limited; backing off")
        return {"data": []}
    r.raise_for_status()
    return r.json()


def resolve_game_id(token, client_id, name):
    data = helix("games", token, client_id, name=name)
    if not data["data"]:
        log.error("Twitch does not yet have a category named %r", name)
        return None
    gid = data["data"][0]["id"]
    log.info("Resolved %r -> game_id %s", name, gid)
    return gid


# ── Scoring ────────────────────────────────────────────────────────────────
def score_clip(clip, conf):
    """views-per-hour × duration fit × title signal. Higher is better."""
    s = conf["scoring"]
    created = dt.datetime.fromisoformat(clip["created_at"].replace("Z", "+00:00"))
    age_h = max(
        0.25, (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 3600
    )
    velocity = clip.get("view_count", 0) / age_h

    lo, hi = s["duration_sweet_spot"]
    d = clip.get("duration", 0)
    if lo <= d <= hi:
        fit = 1.0
    elif d < lo:
        fit = max(0.2, d / lo)
    else:
        fit = max(0.3, hi / d)

    title = (clip.get("title") or "").lower()
    signal = 1.0
    for kw in s["title_boost_keywords"]:
        if kw in title:
            signal += 0.18
    for kw in s["title_penalty_keywords"]:
        if kw in title:
            # Spoilers / cheats / isolated cutscenes get removed by Rockstar.
            # Don't spend a render slot on something that gets taken down.
            return -1.0, "blocked_keyword:" + kw

    return round(velocity * fit * signal, 2), ""


def discover_twitch(conn, conf, client_id, client_secret):
    token = twitch_token(client_id, client_secret)
    gid = conf["game"].get("twitch_game_id") or resolve_game_id(
        token, client_id, conf["game"]["twitch_game_name"]
    )
    if not gid:
        return 0

    # Permissioned Twitch streamers only, unless you've whitelisted the game.
    allow = {
        (s.get("twitch_login") or "").lower()
        for s in sources()
        if s["platform"] == "twitch"
    }
    allow.discard("")

    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        hours=conf["discovery"]["lookback_hours"]
    )
    data = helix(
        "clips",
        token,
        client_id,
        game_id=gid,
        started_at=start.isoformat().replace("+00:00", "Z"),
        first=100,
    )

    found = 0
    per_source = {}
    cap = conf["discovery"]["per_source_cap"]

    for c in data.get("data", []):
        login = (c.get("broadcaster_name") or "").lower()
        if allow and login not in allow:
            continue
        if per_source.get(login, 0) >= cap:
            continue
        if c.get("view_count", 0) < 500:
            continue

        src = next((x for x in sources()
                    if (x.get("twitch_login") or "").lower() == login), None)
        if src is not None:
            from common import footage_ok, is_publishable
            if is_publishable(src) and not footage_ok(src, c.get("created_at")):
                continue          # outside the campaign's footage window
        score, note = score_clip(c, conf)
        if score < 0:
            log.info("skip %s (%s)", c["id"], note)
            continue

        conn.execute(
            """INSERT OR IGNORE INTO clips
               (clip_id, source_id, streamer, platform, title, url, views,
                duration, created_at, score, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                c["id"],
                "twitch:" + login,
                c.get("broadcaster_name"),
                "twitch",
                c.get("title"),
                c.get("url"),
                c.get("view_count", 0),
                c.get("duration", 0),
                c.get("created_at"),
                score,
                note,
            ),
        )
        per_source[login] = per_source.get(login, 0) + 1
        found += 1

    conn.commit()
    log.info("Twitch: %d new candidates", found)
    return found


def discover_local(conn, conf):
    """Your own console captures dropped into work/raw/own."""
    d = WORK / "raw" / "own"
    found = 0
    for p in sorted(d.glob("*.mp4")) + sorted(d.glob("*.mov")):
        cid = "own:" + re.sub(r"\W+", "_", p.stem)[:60]
        conn.execute(
            """INSERT OR IGNORE INTO clips
               (clip_id, source_id, streamer, platform, title, url, views,
                duration, created_at, score, state)
               VALUES (?,?,?,?,?,?,?,?,?,?,'downloaded')""",
            (
                cid,
                "own_capture",
                "You",
                "local",
                p.stem.replace("_", " "),
                str(p),
                0,
                0,
                dt.datetime.now(dt.timezone.utc).isoformat(),
                999.0,  # your own footage always goes first: zero risk
            ),
        )
        found += 1
    conn.commit()
    if found:
        log.info("Local: %d own captures queued", found)
    return found


def main():
    import os

    conf = cfg()
    conn = db()
    total = discover_local(conn, conf)

    cid = os.environ.get("TWITCH_CLIENT_ID")
    secret = os.environ.get("TWITCH_CLIENT_SECRET")
    if cid and secret:
        try:
            total += discover_twitch(conn, conf, cid, secret)
        except Exception as e:  # noqa: BLE001
            log.error("Twitch discovery failed: %s", e)
    else:
        log.warning("TWITCH_CLIENT_ID/SECRET unset — skipping Twitch discovery")

    pending = conn.execute(
        "SELECT COUNT(*) FROM clips WHERE state='discovered'"
    ).fetchone()[0]
    log.info("Discovery done. %d new, %d awaiting download", total, pending)


if __name__ == "__main__":
    main()


def selftest():
    """Check Twitch keys, the logins in sources.yml, and the GTA categories."""
    import os
    cid, secret = os.environ.get("TWITCH_CLIENT_ID"), os.environ.get("TWITCH_CLIENT_SECRET")
    if not (cid and secret):
        print("  TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET not set")
        return
    tok = twitch_token(cid, secret)
    print("  keys: OK (got an app token)")

    tracked = [s for s in sources(enabled_only=False) if s.get("twitch_login")]
    users = helix("users", tok, cid, login=[s["twitch_login"] for s in tracked])["data"]
    found = {u["login"].lower(): u for u in users}
    live = {x["user_login"].lower(): x for x in helix(
        "streams", tok, cid, user_id=[u["id"] for u in users])["data"]} if users else {}
    for s in tracked:
        u = found.get(s["twitch_login"].lower())
        if not u:
            print(f"  login {s['twitch_login']:<14} NOT FOUND  <- fix in sources.yml")
            continue
        st = live.get(u["login"].lower())
        now = f"LIVE now: {st['game_name']} ({st['viewer_count']:,} viewers)" if st else "offline"
        print(f"  login {s['twitch_login']:<14} OK  {u['display_name']:<14} {now}")

    for name in ("Grand Theft Auto VI", "Grand Theft Auto V"):
        g = helix("games", tok, cid, name=name)["data"]
        if not g:
            print(f"  category {name!r}: not on Twitch yet")
            continue
        streams = helix("streams", tok, cid, game_id=g[0]["id"], first=5)["data"]
        top = ", ".join(f"{x['user_name']} ({x['viewer_count']:,})" for x in streams)
        print(f"  category {name!r}: id {g[0]['id']} | top live: {top or 'none'}")
