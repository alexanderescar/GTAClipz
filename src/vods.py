"""
Collect long videos to scan, from five sources, and record them in `vods`.

  1. approved streamers   sources.yml, permission != none, longform: true
  2. campaign footage     campaign sources + links you paste in vod_queue.yml
  3. manual link drop     vod_queue.yml (any url; source_id decides the lane)
  4. auto-discovery       the biggest live GTA 6 streams right now -> SCOUT lane
  5. launch stream        paste the YouTube x Rockstar VOD link into the queue

Each VOD gets a lane from its source's permission: "publish" or "scout".
Scout VODs produce a couple of proof-of-concept clips per streamer per day,
for outreach. They are never published.
"""
import hashlib
import os
import re

import yaml

import datetime as dt

from common import ROOT, cfg, db, footage_ok, lane_for, log, source_by_id, sources

AUTO_SCOUT = {"id": "auto_scout", "streamer": "auto", "permission": "none"}


def parse_twitch_duration(s):
    h = re.search(r"(\d+)h", s or "")
    m = re.search(r"(\d+)m", s or "")
    sec = re.search(r"(\d+)s", s or "")
    return sum(int(x.group(1)) * k for x, k in ((h, 3600), (m, 60), (sec, 1)) if x)


def upsert_vod(conn, vod_id, source, url, *, streamer=None, platform="twitch",
               title="", duration=0, is_live=False, max_clips=None, created_at=None,
               trusted=False):
    lane = lane_for(source)
    # When a source has no creation time (a channel listing), "first seen" is
    # the conservative stand-in: anything already up today predates the window.
    row = conn.execute("SELECT first_seen, created_at FROM vods WHERE vod_id=?",
                       (vod_id,)).fetchone()
    when = created_at or (row and (row[1] or row[0] + "+00:00")) or \
        dt.datetime.now(dt.timezone.utc).isoformat()
    # trusted = a VOD Alex pasted into vod_queue.yml himself (he checked the date)
    if lane == "publish" and not trusted and not footage_ok(source, when):
        conn.execute(
            """INSERT INTO vods (vod_id, source_id, streamer, platform, url, title,
                                 lane, state, created_at)
               VALUES (?,?,?,?,?,?,?, 'ineligible', ?)
               ON CONFLICT(vod_id) DO UPDATE SET state='ineligible'""",
            (vod_id, source["id"], streamer or source.get("streamer"), platform,
             url, title, lane, when))
        return
    conn.execute(
        """INSERT INTO vods (vod_id, source_id, streamer, platform, url, title,
                             lane, duration, is_live, max_clips)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(vod_id) DO UPDATE SET duration=excluded.duration,
               is_live=excluded.is_live, updated_at=CURRENT_TIMESTAMP,
               state=CASE WHEN vods.state='done' AND excluded.is_live=1
                          THEN 'new' ELSE vods.state END""",
        (vod_id, source["id"], streamer or source.get("streamer"), platform, url,
         title, lane, duration, int(is_live), max_clips),
    )
    if created_at:
        conn.execute("UPDATE vods SET created_at=? WHERE vod_id=?", (created_at, vod_id))


# ── 1 + 4: Twitch ─────────────────────────────────────────────────────────────
def from_twitch(conn, conf):
    cid, secret = os.environ.get("TWITCH_CLIENT_ID"), os.environ.get("TWITCH_CLIENT_SECRET")
    if not (cid and secret):
        log.warning("Twitch creds unset — skipping Twitch VOD sources")
        return 0
    from discover import helix, resolve_game_id, twitch_token
    tok = twitch_token(cid, secret)
    n = 0

    tracked = [s for s in sources() if s.get("platform") == "twitch"
               and s.get("longform") and s.get("twitch_login")]
    if tracked:
        users = helix("users", tok, cid,
                      login=[s["twitch_login"] for s in tracked])["data"]
        by_login = {u["login"].lower(): u["id"] for u in users}
        streams = helix("streams", tok, cid, user_id=list(by_login.values()))["data"]
        live = {x["user_id"] for x in streams}
        gid = str(conf["game"].get("twitch_game_id") or "")
        live_gta = {x["user_id"] for x in streams if str(x.get("game_id")) == gid}
        for s in tracked:
            uid = by_login.get(s["twitch_login"].lower())
            if not uid:
                log.warning("Twitch login not found: %s (fix sources.yml)",
                            s["twitch_login"])
                continue
            vids = helix("videos", tok, cid, user_id=uid, type="archive",
                         first=2)["data"]
            must = [w.lower() for w in s.get("title_must_include") or []]
            for i, v in enumerate(vids):
                is_live = uid in live and i == 0
                # GTA-only sources: a live stream counts if its category is GTA VI
                # right now; a finished VOD needs GTA in its title.
                if must and not (is_live and uid in live_gta) and \
                        not any(w in v["title"].lower() for w in must):
                    continue
                upsert_vod(conn, "tw:" + v["id"], s, v["url"],
                           title=v["title"], duration=parse_twitch_duration(v["duration"]),
                           is_live=is_live,
                           created_at=v.get("created_at"))
                n += 1

    # 4. auto-discovery: who's biggest in GTA 6 right now that we don't track
    sc = conf["scouting"]
    if sc.get("auto_discover"):
        gid = conf["game"].get("twitch_game_id") or resolve_game_id(
            tok, cid, conf["game"]["twitch_game_name"])
        if gid:
            known = {s.get("twitch_login", "").lower() for s in sources(False)}
            streams = helix("streams", tok, cid, game_id=gid, first=20)["data"]
            fresh = [x for x in streams if x["user_login"].lower() not in known]
            for x in fresh[: sc["auto_discover_top"]]:
                vids = helix("videos", tok, cid, user_id=x["user_id"],
                             type="archive", first=1)["data"]
                if vids:
                    v = vids[0]
                    upsert_vod(conn, "tw:" + v["id"], AUTO_SCOUT, v["url"],
                               streamer=x["user_name"], title=v["title"],
                               duration=parse_twitch_duration(v["duration"]),
                               is_live=True)
                    n += 1
    conn.commit()
    return n


# ── YouTube channels (e.g. a campaign streamer who streams on YouTube) ──────
def from_youtube_channels(conn):
    import subprocess
    n = 0
    for s in sources():
        if s.get("platform") != "youtube_channel" or not s.get("longform"):
            continue
        out = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--playlist-end", "6",
             "--print", "%(id)s|%(live_status)s|%(duration)s|%(title)s",
             s["youtube_channel"]],
            capture_output=True, text=True, timeout=180)
        must = [w.lower() for w in s.get("title_must_include") or []]
        never = [w.lower() for w in s.get("title_must_exclude") or []]
        for line in out.stdout.splitlines():
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            vid, live, dur, title = parts
            if must and not any(w in title.lower() for w in must):
                continue
            if any(w in title.lower() for w in never):
                continue
            if live in ("is_live", "is_upcoming"):
                continue        # chat replay only exists once the stream ends
            try:
                d = float(dur)
            except ValueError:
                d = 0
            upsert_vod(conn, "yt:" + vid, s, f"https://www.youtube.com/watch?v={vid}",
                       platform="youtube", title=title, duration=d)
            n += 1
    conn.commit()
    return n


# ── 2 + 3 + 5: the manual queue ───────────────────────────────────────────────
def from_queue(conn):
    path = ROOT / "config" / "vod_queue.yml"
    if not path.exists():
        return 0
    items = (yaml.safe_load(path.read_text()) or {}).get("queue") or []
    n = 0
    for it in items:
        url = it.get("url")
        src = source_by_id(it.get("source_id", "manual_scout"))
        if not url or not src:
            log.warning("queue entry skipped (bad url/source_id): %s", it)
            continue
        m = re.search(r"twitch\.tv/videos/(\d+)", url)
        if m:
            vid, plat = "tw:" + m.group(1), "twitch"
        elif "youtu" in url:
            vid, plat = "yt:" + hashlib.md5(url.encode()).hexdigest()[:12], "youtube"
        elif "kick.com" in url:
            vid, plat = "kk:" + hashlib.md5(url.encode()).hexdigest()[:12], "kick"
        else:
            vid, plat = "lf:" + hashlib.md5(url.encode()).hexdigest()[:12], "local"
        upsert_vod(conn, vid, src, url, platform=plat,
                   streamer=it.get("streamer") or src.get("streamer"),
                   max_clips=it.get("max_clips"),
                   created_at=it.get("recorded"),
                   trusted=not it.get("recorded"))
        n += 1
    conn.commit()
    return n


def from_kick(conn):
    """Kick channels in sources.yml with longform: true. Finished streams only
    (no chat replay on Kick, so moments come from audio peaks)."""
    import kick
    n = 0
    for s in sources():
        if s.get("platform") != "kick" or not s.get("longform"):
            continue
        slug = kick.slug_of(s)
        try:
            vs = kick.videos(slug)
        except Exception as e:  # noqa: BLE001
            log.warning("Kick %s unreachable (%s) — use vod_queue.yml", slug, e)
            continue
        for v in vs:
            if v["is_live"]:
                continue
            upsert_vod(conn, "kk:" + v["id"], s, v["url"], platform="kick",
                       title=v["title"], duration=v["duration_s"],
                       created_at=v["created_at"])
            n += 1
    conn.commit()
    return n


def collect():
    conf = cfg()
    conn = db()
    n = (from_queue(conn) + from_twitch(conn, conf) + from_youtube_channels(conn)
         + from_kick(conn))
    pend = conn.execute("SELECT lane, COUNT(*) FROM vods WHERE state IN ('new','scanning') "
                        "GROUP BY lane").fetchall()
    log.info("VOD sources: %d seen | pending by lane: %s", n, dict(pend))
    return n


if __name__ == "__main__":
    collect()
