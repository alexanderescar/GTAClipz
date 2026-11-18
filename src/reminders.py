"""Reminders for the jobs only you can do.

Every reminder is a GitHub comment that @mentions you, so the GitHub phone app
pushes it and GitHub emails it. Four kinds:

  1. Clips waiting   — new clips in today's review issue (at most every 2 h,
                       only inside campaign hours).
  2. Submit now      — sent by the publisher the moment a campaign clip posts.
  3. Milestones      — e.g. a TOMO clip passes 2,000 YouTube views → post the
                       audience screenshot in their Discord.
  4. Dated tasks     — config/reminders.yml (launch-day checklist, RUCKUS stream
                       check, auto-approve switch day, Oracle move…).

Each reminder fires once; what fired is remembered in the state ledger.
"""
import datetime as dt
import os
import subprocess

import yaml

from common import ROOT, log, source_by_id

LABEL = "reminders"
TITLE = "📌 Your to-do reminders"


def _meta(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def _get(conn, key):
    row = _meta(conn).execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set(conn, key, value):
    _meta(conn).execute("INSERT INTO meta (key,value) VALUES (?,?) ON CONFLICT(key) "
                        "DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def owner():
    return (os.environ.get("GITHUB_REPOSITORY") or "/").split("/")[0]


def _now():
    return dt.datetime.now(dt.timezone.utc)


def reminders_issue():
    import review
    for iss in review._gh("GET", "/issues", params={"labels": LABEL, "state": "all"}):
        if iss["title"] == TITLE:
            if iss["state"] != "open":
                review._gh("PATCH", f"/issues/{iss['number']}", json={"state": "open"})
            return iss["number"]
    iss = review._gh("POST", "/issues", json={
        "title": TITLE, "labels": [LABEL], "assignees": [owner()],
        "body": ("The robot posts a comment here (and @mentions you) whenever "
                 "there's something only you can do. Close a reminder by "
                 "doing it — no need to reply.")})
    return iss["number"]


def notify(text, issue=None):
    """Post an @mention comment. Falls back to the reminders issue."""
    import review
    issue = issue or reminders_issue()
    review._gh("POST", f"/issues/{issue}/comments",
               json={"body": f"@{owner()} {text}"})
    log.info("reminder sent (#%s): %s", issue, text.splitlines()[0][:80])


# ── 1. clips waiting for approval ────────────────────────────────────────────
def nudge_review(conn, conf):
    from pipeline import in_campaign_hours
    rows = conn.execute("SELECT review_issue, COUNT(*) FROM clips WHERE "
                        "state='pending_review' GROUP BY review_issue").fetchall()
    waiting = sum(r[1] for r in rows)
    if not waiting or not in_campaign_hours(conf):
        return
    last = _get(conn, "nudge_review_at")
    if last and _now() - dt.datetime.fromisoformat(last) < dt.timedelta(hours=2):
        return
    issue = max(r[0] for r in rows if r[0]) if any(r[0] for r in rows) else None
    notify(f"🎬 **{waiting} clip(s) waiting for your ✅ / ❌.** Tap the boxes in "
           f"this issue; approved campaign clips post within a few minutes. "
           f"Untouched clips expire after {conf['review']['expire_hours']} h.", issue)
    _set(conn, "nudge_review_at", _now().isoformat())


# ── 3. view milestones (YouTube views are public; yt-dlp reads them) ────────
def yt_views(url):
    out = subprocess.run(["yt-dlp", "--skip-download", "--print", "view_count", url],
                         capture_output=True, text=True, timeout=90)
    try:
        return int(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def milestones(conn):
    last = _get(conn, "milestone_check_at")
    if last and _now() - dt.datetime.fromisoformat(last) < dt.timedelta(hours=1):
        return
    _set(conn, "milestone_check_at", _now().isoformat())
    rows = conn.execute(
        "SELECT p.clip_id, p.url, c.source_id FROM posts p JOIN clips c "
        "ON c.clip_id=p.clip_id WHERE p.target='youtube' AND p.status='ok' "
        "AND p.url IS NOT NULL AND p.posted_at > datetime('now','-14 days')").fetchall()
    for clip_id, url, sid in rows:
        src = source_by_id(sid) or {}
        m = src.get("milestone")
        if not m or _get(conn, f"milestone:{clip_id}"):
            continue
        v = yt_views(url)
        if v is not None and v >= m["views"]:
            notify(f"📈 **{src.get('campaign', sid)}: a clip passed "
                   f"{m['views']:,} views** ({v:,} now) — {url}\n\n{m['task']}")
            _set(conn, f"milestone:{clip_id}", str(v))


# ── 4. dated reminders ───────────────────────────────────────────────────────
def dated(conn):
    path = ROOT / "config" / "reminders.yml"
    if not path.exists():
        return
    for r in (yaml.safe_load(path.read_text()) or {}).get("reminders") or []:
        key = f"dated:{r['id']}"
        at = dt.datetime.fromisoformat(str(r["at"]))
        if _get(conn, key) or _now() < at:
            continue
        if _now() > at + dt.timedelta(hours=r.get("expires_hours", 24)):
            _set(conn, key, "expired")     # a stale checklist is just noise
            continue
        cond = r.get("only_if_no_vods_from")
        if cond and conn.execute("SELECT 1 FROM vods WHERE source_id=? AND "
                                 "state!='ineligible'", (cond,)).fetchone():
            _set(conn, key, "skipped: condition met")
            continue
        notify(f"⏰ **{r['title']}**\n\n{r['message'].strip()}")
        _set(conn, key, _now().isoformat())


def stage(conn, conf):
    for fn in (lambda: nudge_review(conn, conf), lambda: milestones(conn),
               lambda: dated(conn)):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — a reminder must never stop posting
            log.warning("reminder step failed: %s", e)
