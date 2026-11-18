"""
Orchestrator.

  discover  -> new Twitch clips + your own captures            (cheap, every run)
  vods      -> collect long videos from the five sources        (cheap)
  longform  -> scan VODs for chat/audio spikes, cut windows     (medium)
  download  -> fetch Twitch clips                               (medium)
  render    -> 9:16, hook, captions, watermark; dedupe          (expensive)
  review    -> approval queue (GitHub issue) or learned auto    (cheap)
  publish   -> approved clips only, respecting daily caps       (medium)

Lanes: "publish" clips flow all the way through. "scout" clips stop after
render as proof-of-concept clips for outreach — they are never published.

  python src/pipeline.py run        # everything (what Actions runs)
  python src/pipeline.py <stage>    # one stage
  python src/pipeline.py status
"""
import datetime as dt
import os
import pathlib
import re
import sqlite3
import subprocess
import sys

from common import (WORK, cfg, db, ensure_dirs, is_publishable, lane_for, log,
                    posts_today, source_by_id, sources)
import metadata
import transform


def safe(clip_id):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", clip_id)


def rows(conn, sql, *a):
    conn.row_factory = sqlite3.Row
    return conn.execute(sql, a).fetchall()


def source_for(c):
    sid = c["source_id"] or ""
    if sid.startswith("twitch:"):
        login = sid.split(":", 1)[1]
        for s in sources(enabled_only=False):
            if (s.get("twitch_login") or "").lower() == login:
                return s
        return {"id": sid, "permission": "none", "streamer": c["streamer"]}
    return source_by_id(sid) or {"id": sid, "permission": "none"}


def r2_preview(path, key):
    st = cfg()["storage"]
    if not st.get("r2_public_base"):
        return None
    try:
        import r2
        return r2.put_public(path, key, st["r2_bucket"], st["r2_public_base"])
    except Exception as e:  # noqa: BLE001
        log.warning("preview upload failed: %s", e)
        return None


# ── download (Twitch clips) ──────────────────────────────────────────────────
def stage_download(conn, conf):
    quota = conf["scouting"]["clips_per_streamer_per_day"]
    for c in rows(conn, "SELECT * FROM clips WHERE state='discovered' "
                        "ORDER BY score DESC LIMIT 25"):
        src = source_for(c)
        lane = lane_for(src)
        if lane == "scout":
            done = conn.execute("SELECT COUNT(*) FROM clips WHERE lane='scout' AND "
                                "streamer=? AND state!='discovered' AND "
                                "date(first_seen)=date('now')",
                                (c["streamer"],)).fetchone()[0]
            if done >= quota:
                conn.execute("UPDATE clips SET state='blocked_no_permission', "
                             "lane='scout' WHERE clip_id=?", (c["clip_id"],))
                continue
        dst = WORK / "raw" / f"{safe(c['clip_id'])}.mp4"
        try:
            subprocess.run(["yt-dlp", "-q", "-f", "bv*+ba/b", "--merge-output-format",
                            "mp4", "-o", str(dst), c["url"]],
                           check=True, capture_output=True, text=True, timeout=300)
            conn.execute("UPDATE clips SET state='downloaded', lane=? WHERE clip_id=?",
                         (lane, c["clip_id"]))
        except Exception as e:  # noqa: BLE001
            log.error("download failed %s: %s", c["clip_id"], e)
            conn.execute("UPDATE clips SET state='download_failed' WHERE clip_id=?",
                         (c["clip_id"],))
    conn.commit()


# ── render ───────────────────────────────────────────────────────────────────
def stage_render(conn, conf):
    import dedupe
    import time
    cap = cfg("sources").get("defaults", {}).get("max_clip_seconds", 59)
    done, t0 = 0, time.time()
    budget = conf["render"].get("budget_s", 1080)
    for c in rows(conn, "SELECT * FROM clips WHERE state='downloaded' "
                        "ORDER BY CASE lane WHEN 'publish' THEN 0 ELSE 1 END, "
                        "score DESC LIMIT 12"):
        if time.time() - t0 > budget:
            log.warning("render budget used; %s waits", c["clip_id"])
            break
        src_path = (pathlib.Path(c["url"]) if c["platform"] in ("local", "vod")
                    else WORK / "raw" / f"{safe(c['clip_id'])}.mp4")
        if not src_path.exists():
            conn.execute("UPDATE clips SET state='missing_file' WHERE clip_id=?",
                         (c["clip_id"],))
            continue
        src = source_for(c)
        hook = metadata.make_hook(dict(c), templates=src.get("hooks"))
        rconf = conf
        if src.get("watermark") is False:          # e.g. Lacy: "no logos"
            import copy
            rconf = copy.deepcopy(conf)
            rconf["render"]["watermark"]["enabled"] = False
        out = WORK / "out" / f"{safe(c['clip_id'])}.mp4"
        try:
            dur = min(transform.probe(src_path)["duration"], cap)
            vocab = [x for x in (c["streamer"], src.get("streamer")) if x]
            transform.render(src_path, out, hook_text=hook, conf=rconf, duration=dur,
                             facecam=src.get("facecam"), vocab=vocab)
        except Exception as e:  # noqa: BLE001
            log.error("render failed %s: %s", c["clip_id"], e)
            conn.execute("UPDATE clips SET state='render_failed' WHERE clip_id=?",
                         (c["clip_id"],))
            continue

        sig = ""
        if conf["safety"]["skip_if_phash_seen"]:
            conn.commit()   # dedupe reads through its own connection
            try:
                sig = dedupe.signature(out)
                if dedupe.is_duplicate(sig):
                    log.info("duplicate moment, skipping %s", c["clip_id"])
                    conn.execute("UPDATE clips SET state='duplicate', phash=? "
                                 "WHERE clip_id=?", (sig, c["clip_id"]))
                    conn.commit()
                    out.unlink(missing_ok=True)
                    continue
            except Exception as e:  # noqa: BLE001
                log.warning("dedupe skipped: %s", e)

        if c["lane"] == "scout":
            prefix = conf["scouting"]["r2_prefix"]
            state = "scouted"
        else:
            prefix, state = "review/", "rendered"
        preview = r2_preview(out, prefix + out.name)
        conn.execute("UPDATE clips SET state=?, render_path=?, phash=?, note=?, "
                     "preview_url=?, duration=? WHERE clip_id=?",
                     (state, str(out), sig, hook, preview, dur, c["clip_id"]))
        done += 1
    conn.commit()
    log.info("rendered %d", done)


# ── publish ──────────────────────────────────────────────────────────────────
def ensure_local(c, conf):
    """The run that rendered a clip is long gone by the time it's approved;
    the file lives on in R2, so bring it back before uploading."""
    path = pathlib.Path(c["render_path"] or "")
    if path.is_file():
        return str(path)
    if not c["preview_url"]:
        raise RuntimeError("render missing and no R2 copy")
    import r2
    st = conf["storage"]
    dest = WORK / "render" / f"{safe(c['clip_id'])}.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    r2.fetch(c["preview_url"], st["r2_public_base"], st["r2_bucket"], dest)
    return str(dest)


def in_campaign_hours(conf, now=None):
    from zoneinfo import ZoneInfo
    h = conf["publish"].get("campaign_hours")
    if not h:
        return True
    local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(h["timezone"]))
    t = local.strftime("%H:%M")
    return h["start"] <= t < h["end"] if h["start"] < h["end"] else \
        (t >= h["start"] or t < h["end"])


def campaign_slot_open(conn, conf, now=None):
    """Campaign clips go out only inside campaign hours AND inside the posting
    slice of the hour, and at most N per hour, so the hourly submit task in
    your Chrome always finds them inside the 30-minute window."""
    if not in_campaign_hours(conf, now):
        return False
    pub = conf["publish"]
    lo, hi = pub.get("campaign_minutes") or [0, 60]
    now = now or dt.datetime.now(dt.timezone.utc)
    if not (lo <= now.minute < hi):
        return False
    cap = pub.get("campaign_clips_per_hour")
    if cap:
        start = now.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
        n = conn.execute("SELECT COUNT(DISTINCT clip_id) FROM posts WHERE status IN ('ok','queued') "
                         "AND posted_at >= ?", (start,)).fetchone()[0]
        if n >= cap:
            return False
    return True


LIMIT_MARKERS = ("exceeded the number of videos", "quotaexceeded", "rate limit",
                 "too many", "spam_risk", "application request limit", "(#4)",
                 "(#9)", "(#32)", "(#613)",
                 # Meta blocked the app / token died: every retry fails the
                 # same way, and hammering it every few minutes looks like abuse.
                 "api access blocked", "5258005", "error validating access token",
                 "session has expired")


BLOCK_MARKERS = ("api access blocked", "5258005", "error validating access token",
                 "session has expired")


def is_limit_error(e):
    t = str(e).lower()
    return any(m in t for m in LIMIT_MARKERS)


def is_block_error(e):
    """Access problems get fixed by a human, often within the hour — so they
    pause the platform for an hour, not the whole day."""
    t = str(e).lower()
    return any(m in t for m in BLOCK_MARKERS)


def pause_platform(conn, target, hours=None):
    """hours=None: 'no more today' — skip it until midnight UTC."""
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    until = ("date('now')" if hours is None
             else f"datetime('now', '+{int(round(hours * 60))} minutes')")
    conn.execute(f"INSERT INTO meta (key,value) VALUES (?, {until}) "
                 f"ON CONFLICT(key) DO UPDATE SET value={until}", (f"paused:{target}",))
    log.warning("%s refused — pausing it %s", target,
                "until tomorrow" if hours is None else f"for {hours}h")


def platform_paused(conn, target):
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM meta WHERE key=?", (f"paused:{target}",)).fetchone()
    if not row or not row[0]:
        return False
    v = row[0]
    if len(v) == 10:                       # a date: paused for that whole day
        if v != conn.execute("SELECT date('now')").fetchone()[0]:
            return False
        # A day-long pause caused by blocked access (older code did that) is
        # lifted: access problems only pause for an hour now.
        last = conn.execute("SELECT status FROM posts WHERE target=? AND status!='ok' "
                            "ORDER BY id DESC LIMIT 1", (target,)).fetchone()
        return not (last and is_block_error(last[0]))
    return conn.execute("SELECT datetime('now') < ?", (v,)).fetchone()[0] == 1


def requeue_failed(conn):
    """One-time-per-clip rescue: clips that failed only because a platform
    hit a limit (or a transient Instagram 400) go back to 'approved'."""
    rows = conn.execute(
        "SELECT clip_id FROM clips WHERE state='publish_failed'").fetchall()
    for (cid,) in rows:
        errs = [r[0] for r in conn.execute(
            "SELECT status FROM posts WHERE clip_id=? AND status!='ok'", (cid,))]
        # limit/blocked errors aren't the clip's fault: don't count them
        tries = len([e for e in errs if not is_limit_error(e)])
        if errs and tries <= 6 and all(
                is_limit_error(e) or "instagram" in e.lower() or "400 client error" in e.lower()
                for e in errs):
            conn.execute("UPDATE clips SET state='approved' WHERE clip_id=?", (cid,))
            log.info("requeued %s (failed only on platform limits)", cid)
    conn.commit()


def stage_publish(conn, conf):
    import tiktok_queue
    chrome_tiktok = conf["publish"]["tiktok"].get("mode") == "chrome"
    if chrome_tiktok:
        tiktok_queue.absorb_posted(conn)
    try:
        _publish_approved(conn, conf, chrome_tiktok)
    finally:
        if chrome_tiktok:
            try:
                tiktok_queue.sync_branch(conn)
            except Exception as e:  # noqa: BLE001
                log.error("tiktok queue sync failed: %s", e)


def _publish_approved(conn, conf, chrome_tiktok):
    import tiktok_queue
    requeue_failed(conn)
    # Approved clips that couldn't post within N hours are stale: drop them so
    # fresh ones go first (the loop below already posts highest score first).
    keep_h = conf["publish"].get("approved_max_age_h", 48)
    n = conn.execute(
        "UPDATE clips SET state='expired_approved' WHERE state='approved' AND clip_id IN "
        "(SELECT clip_id FROM decisions WHERE decision='approved' AND "
        "decided_at < datetime('now', ?))", (f"-{keep_h} hours",)).rowcount
    if n:
        log.info("publish: %d approved clips older than %dh expired", n, keep_h)
    conn.commit()
    caps = conf["safety"]["max_posts_per_platform_per_day"]
    for c in rows(conn, "SELECT * FROM clips WHERE state='approved' AND "
                        "lane='publish' ORDER BY score DESC LIMIT 20"):
        src = source_for(c)
        if not is_publishable(src):       # belt and braces: never trust the lane alone
            conn.execute("UPDATE clips SET state='blocked_no_permission' "
                         "WHERE clip_id=?", (c["clip_id"],))
            continue
        now = dt.datetime.now(dt.timezone.utc)
        after, before = src.get("publish_after"), src.get("publish_before")
        if after and now < dt.datetime.fromisoformat(str(after)):
            continue                    # campaign not open yet: stays approved
        if before and now > dt.datetime.fromisoformat(str(before)):
            conn.execute("UPDATE clips SET state='campaign_closed' WHERE clip_id=?",
                         (c["clip_id"],))
            continue
        if src.get("permission") == "campaign" and not campaign_slot_open(conn, conf):
            continue                    # wait for the next submit slot
        try:
            local = ensure_local(c, conf)
        except Exception as e:  # noqa: BLE001
            log.error("can't fetch render for %s: %s", c["clip_id"], e)
            continue
        credit = (src.get("credit_template")
                  or cfg("sources")["defaults"]["credit_template"]).format(
            streamer=c["streamer"] or src.get("streamer", ""),
            link=src.get("link", ""))
        allowed = set(src.get("platforms_allowed") or ["youtube", "tiktok", "instagram"])
        any_ok = False
        attempted = False
        limited = False
        posted = []
        for target in ("youtube", "instagram", "tiktok"):
            if target not in allowed or not conf["publish"][target]["enabled"]:
                continue
            if posts_today(conn, target) >= caps[target] or platform_paused(conn, target):
                continue
            attempted = True
            m = metadata.build(dict(c), target, credit, hook=c["note"],
                               extra_tags=src.get("hashtags"))
            try:
                if target == "tiktok" and chrome_tiktok:
                    # Claude posts it from Chrome with the full caption (:41 task)
                    res = tiktok_queue.enqueue(conn, c, local, m["caption"], src)
                else:
                    res = _send(target, local, m, conf, c,
                                paid=bool(src.get("paid_promotion")))
                conn.execute("INSERT INTO posts (clip_id,target,remote_id,url,status) "
                             "VALUES (?,?,?,?,?)",
                             (c["clip_id"], target, res.get("remote_id"), res.get("url"),
                              "queued" if res.get("status") == "QUEUED_CHROME" else "ok"))
                any_ok = True
                posted.append((target, res.get("url") or "", res.get("status")))
            except Exception as e:  # noqa: BLE001
                log.error("%s publish failed for %s: %s", target, c["clip_id"], e)
                conn.execute("INSERT INTO posts (clip_id,target,status) VALUES (?,?,?)",
                             (c["clip_id"], target, f"error: {e}"[:300]))
                if is_limit_error(e):
                    pause_platform(conn, target, hours=1 if is_block_error(e) else None)
                    limited = True
                else:
                    # Unknown error: cool down instead of firing the same failing
                    # call for every queued clip every 2.5 min. On Nov 18 that burst
                    # (11 failures in 8 s, then 12 per loop) got Meta to block the
                    # app's API access for about an hour.
                    pause_platform(conn, target, hours=0.5)
                    limited = True
        if not attempted or (limited and not any_ok):
            conn.commit()
            continue    # capped / platform limit hit: stays approved, retried later
        conn.execute("UPDATE clips SET state=? WHERE clip_id=?",
                     ("published" if any_ok else "publish_failed", c["clip_id"]))
        conn.commit()
        if (posted and src.get("permission") == "campaign"
                and conf["publish"].get("submit_now_alerts", not chrome_tiktok)):
            submit_now_alert(c, src, posted, credit)


def submit_now_alert(c, src, posted, credit=None):
    """Campaigns only pay if each post link is submitted within 30 minutes.
    Comment on the clip's review issue so GitHub pushes it to your phone."""
    owner = (os.environ.get("GITHUB_REPOSITORY") or "/").split("/")[0]
    lines = [f"### 🚨 SUBMIT NOW — {src.get('campaign', src['id'])}",
             f"@{owner}" if owner else "",
             f"Posted just now. Submit within **30 minutes**: {src.get('campaign_url', '')}",
             ""]
    tiktok_caption = None
    try:
        import metadata
        # Same hook + credit the robot used for YouTube/Instagram, so the
        # TikTok caption matches and always carries the campaign's tag first.
        hook = c["note"] or metadata.make_hook(dict(c), templates=src.get("hooks"))
        tiktok_caption = metadata.build(dict(c), "tiktok",
                                        credit or src.get("credit_template", ""),
                                        hook=hook, extra_tags=src.get("hashtags"))["caption"]
    except Exception:  # noqa: BLE001
        pass
    for target, url, status in posted:
        if target == "tiktok":
            lines.append("- **TikTok:** open the TikTok app → **Inbox** → tap the "
                         "notification \"Your video … is ready\" (not the Drafts "
                         "folder). Post it from there"
                         + (" — turn on *Content disclosure → Paid partnership* first"
                            if src.get("paid_promotion") else "")
                         + ", then submit that link."
                         + ("\n  **Paste this as the TikTok caption** (TikTok doesn't "
                            "receive it from the robot; the campaign needs it):\n"
                            "```\n" + tiktok_caption + "\n```" if tiktok_caption else ""))
        else:
            lines.append(f"- **{target.title()}:** {url}")
    for extra in src.get("also_submit_to") or []:
        lines.append(f"\n➕ **Also submit the same links to {extra['campaign']}:** "
                     f"{extra['url']}" + (f" — {extra['note']}" if extra.get("note") else ""))
    lines.append(f"\n`{c['clip_id']}`")
    try:
        import review
        if c["review_issue"]:
            review._gh("POST", f"/issues/{c['review_issue']}/comments",
                       json={"body": "\n".join(lines)})
    except Exception as e:  # noqa: BLE001
        log.warning("submit-now alert failed: %s", e)


def _send(target, path, m, conf, clip, paid=False):
    if target == "youtube":
        import publish_youtube
        return publish_youtube.upload(path, m["title"], m["description"], m["tags"],
                                      conf, paid=paid)
    if target == "instagram":
        import publish_instagram
        import r2
        st = conf["storage"]
        if not st.get("r2_public_base"):
            raise RuntimeError("storage.r2_public_base unset — Instagram needs a public URL")
        url = clip["preview_url"] or r2.put_public(
            path, f"{safe(clip['clip_id'])}.mp4", st["r2_bucket"], st["r2_public_base"])
        return publish_instagram.publish(url, m["caption"], publish_instagram.ig_user_id(),
                                         os.environ["IG_ACCESS_TOKEN"],
                                         conf["publish"]["instagram"]["share_to_feed"])
    if target == "tiktok":
        import publish_tiktok
        import tiktok_auth
        return publish_tiktok.upload(path, m["caption"], tiktok_auth.access_token(),
                                     mode=conf["publish"]["tiktok"]["mode"])
    raise ValueError(target)


# ── housekeeping ─────────────────────────────────────────────────────────────
def janitor(conf):
    cutoff = dt.datetime.now().timestamp() - conf["storage"]["keep_renders_days"] * 86400
    for d in ("raw", "out", "render"):
        for p in (WORK / d).glob("*"):
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()


def status(conn):
    print("\n  clips by lane / state")
    print("  " + "-" * 40)
    for lane, state, n in conn.execute(
            "SELECT lane, state, COUNT(*) FROM clips GROUP BY 1,2 ORDER BY 1,3 DESC"):
        print(f"  {lane or '-':<8} {state:<22} {n:>5}")
    print("\n  VODs")
    print("  " + "-" * 40)
    for lane, state, n in conn.execute(
            "SELECT lane, state, COUNT(*) FROM vods GROUP BY 1,2"):
        print(f"  {lane:<8} {state:<22} {n:>5}")
    print("\n  posts today")
    print("  " + "-" * 40)
    for t in ("youtube", "instagram", "tiktok"):
        print(f"  {t:<31} {posts_today(conn, t):>5}")
    print()


STAGES = ["discover", "vods", "longform", "download", "render", "review", "publish", "remind"]


def main():
    ensure_dirs()
    conf, conn = cfg(), db()
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "status":
        return status(conn)
    if mode == "e2etest":
        import e2etest
        return e2etest.run()
    if mode == "tiktoktest":
        import tiktok_auth
        return tiktok_auth.selftest()
    if mode == "yttest":
        import publish_youtube
        return publish_youtube.selftest()
    if mode == "igtest":
        import publish_instagram
        return publish_instagram.selftest()
    if mode == "twitchtest":
        import discover
        return discover.selftest()
    if mode == "kicktest":
        import kick
        return kick.selftest()
    if mode == "r2test":
        import r2
        return r2.selftest()
    # "approve" = the quick check between full runs: read your ticks, post
    # A full run starts by posting what you approved since the last cycle, so
    # ticks never wait behind 10-30 min of cutting/rendering.
    want = (["review", "publish"] + STAGES if mode == "run" else
            ["review", "publish", "remind"] if mode == "approve" else [mode])
    for st in want:
        try:
            if st == "discover":
                import discover
                discover.main()
            elif st == "vods" and conf["longform"]["enabled"]:
                import vods
                vods.collect()
            elif st == "longform" and conf["longform"]["enabled"]:
                import longform
                longform.run()
            elif st == "download":
                stage_download(conn, conf)
            elif st == "render":
                stage_render(conn, conf)
            elif st == "review":
                import review
                review.stage(conn, conf)
            elif st == "publish":
                stage_publish(conn, conf)
            elif st == "remind":
                import reminders
                reminders.stage(conn, conf)
        except Exception as e:  # noqa: BLE001  — one bad stage must not kill the run
            log.error("stage %s crashed: %s", st, e)
    janitor(conf)
    status(conn)


if __name__ == "__main__":
    main()
