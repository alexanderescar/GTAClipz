"""
TikTok via Chrome (publish.tiktok.mode: "chrome").

TikTok's API can't put a caption on an inbox draft, and Direct Post needs an
audit this app won't pass. So the robot doesn't post TikToks at all: it
renders a small (<10 MB) copy, queues it with its full caption, and the
hourly Claude task on Alex's computer uploads it at tiktok.com with that
caption, then submits the link on Content Rewards.

Hand-off, all through git (the only channel both sides can reach):
  robot   -> branch `tiktok-queue`   queue.json + the mp4s still waiting
  Claude  -> main `state/tiktok_posted.json`   {clip_id: {url, posted_at}}
The robot absorbs tiktok_posted.json each run, marks those clips posted and
drops them from the branch.
"""
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

from common import ROOT, WORK, log

BRANCH = "tiktok-queue"
POSTED = ROOT / "state" / "tiktok_posted.json"
QDIR = WORK / "tiktok"
MAX_MB = 9.3            # Claude's browser upload tool takes files < 10 MB
KEEP_HOURS = 24         # an entry nobody posted in a day is dropped


def _safe(clip_id):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", clip_id)


def _ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS tiktok_queue (
        clip_id    TEXT PRIMARY KEY,
        source_id  TEXT,
        file       TEXT,
        entry      TEXT,                 -- JSON written to queue.json
        state      TEXT DEFAULT 'queued',-- queued | posted | expired
        queued_at  TEXT DEFAULT CURRENT_TIMESTAMP,
        url        TEXT,
        posted_at  TEXT)""")


def _duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip() or 59)


def encode_small(src, dst):
    """720x1280, two-pass to land just under MAX_MB. TikTok re-encodes
    everything to roughly this anyway."""
    dur = max(_duration(src), 1.0)
    budget_kbps = MAX_MB * 8 * 1024 * 1024 / 1000 / dur
    v = max(500, int(budget_kbps - 96 - 40))
    for attempt in range(3):
        with tempfile.TemporaryDirectory() as td:
            log_prefix = os.path.join(td, "x264")
            base = ["ffmpeg", "-y", "-v", "error", "-i", str(src),
                    "-vf", "scale=720:-2:flags=lanczos,format=yuv420p",
                    "-c:v", "libx264", "-preset", "slow", "-b:v", f"{v}k",
                    "-passlogfile", log_prefix]
            subprocess.run(base + ["-pass", "1", "-an", "-f", "mp4", os.devnull],
                           check=True, capture_output=True)
            subprocess.run(base + ["-pass", "2", "-c:a", "aac", "-b:a", "96k",
                                   "-movflags", "+faststart", str(dst)],
                           check=True, capture_output=True)
        mb = pathlib.Path(dst).stat().st_size / 1024 / 1024
        if mb < 9.8:
            return dst
        v = int(v * 0.85)
        log.warning("tiktok copy %.1f MB, re-encoding at %dk", mb, v)
    raise RuntimeError("could not get the TikTok copy under 10 MB")


def enqueue(conn, clip, local_path, caption, src):
    """Called from stage_publish instead of the TikTok API."""
    _ensure_table(conn)
    QDIR.mkdir(parents=True, exist_ok=True)
    name = f"{_safe(clip['clip_id'])}.mp4"
    encode_small(local_path, QDIR / name)
    hashtags = src.get("hashtags") or []
    entry = {
        "clip_id": clip["clip_id"],
        "file": name,
        # one line: Enter inside TikTok's caption box picks a hashtag suggestion
        "caption": re.sub(r"\s+", " ", caption).strip(),
        "required_tag": hashtags[0] if hashtags else None,
        "campaign": src.get("campaign"),
        "campaign_url": src.get("campaign_url"),
        "source_id": src.get("id"),
        "paid_promotion": bool(src.get("paid_promotion")),
        "queued_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    conn.execute("INSERT OR REPLACE INTO tiktok_queue (clip_id, source_id, file, entry, "
                 "state) VALUES (?,?,?,?, 'queued')",
                 (clip["clip_id"], src.get("id"), name, json.dumps(entry)))
    conn.commit()
    log.info("tiktok queued for Chrome: %s", clip["clip_id"])
    return {"remote_id": None, "url": "", "status": "QUEUED_CHROME"}


def absorb_posted(conn):
    """Pick up what Claude posted (state/tiktok_posted.json on main)."""
    _ensure_table(conn)
    try:
        posted = json.loads(POSTED.read_text()) if POSTED.exists() else {}
    except ValueError:
        log.warning("tiktok_posted.json unreadable; ignoring")
        return
    for clip_id, info in posted.items():
        row = conn.execute("SELECT state FROM tiktok_queue WHERE clip_id=?",
                           (clip_id,)).fetchone()
        if not row or row[0] == "posted":
            continue
        url = (info or {}).get("url") or ""
        when = ((info or {}).get("posted_at") or "").replace("T", " ").rstrip("Z")[:19] or None
        conn.execute("UPDATE tiktok_queue SET state='posted', url=?, posted_at=? "
                     "WHERE clip_id=?", (url, when, clip_id))
        conn.execute("UPDATE posts SET status='ok', url=?, posted_at=COALESCE(?, posted_at) "
                     "WHERE clip_id=? AND target='tiktok' AND status='queued'",
                     (url, when, clip_id))
    conn.execute("UPDATE tiktok_queue SET state='expired' WHERE state='queued' AND "
                 "queued_at < datetime('now', ?)", (f"-{KEEP_HOURS} hours",))
    conn.execute("UPDATE posts SET status='expired: never posted from Chrome' WHERE "
                 "target='tiktok' AND status='queued' AND clip_id IN "
                 "(SELECT clip_id FROM tiktok_queue WHERE state='expired')")
    conn.commit()


def _git(*a, **kw):
    return subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True,
                          **kw)


def sync_branch(conn):
    """Make branch `tiktok-queue` hold exactly the clips still waiting.
    One orphan commit, force-pushed, so the branch never grows."""
    _ensure_table(conn)
    waiting = conn.execute("SELECT clip_id, file, entry FROM tiktok_queue "
                           "WHERE state='queued' ORDER BY queued_at").fetchall()
    have_remote = _git("fetch", "-q", "--depth=1", "origin",
                       f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}").returncode == 0
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td) / "q"
        d.mkdir()
        if have_remote:   # files queued by earlier runs live only on the branch
            arch = subprocess.run(["git", "-C", str(ROOT), "archive", f"origin/{BRANCH}"],
                                  capture_output=True, check=False)
            if arch.returncode == 0:
                subprocess.run(["tar", "-x", "-C", str(d)], input=arch.stdout, check=False)
        entries = []
        for clip_id, name, entry in waiting:
            local = QDIR / name
            if local.exists():
                shutil.copy(local, d / name)
            if (d / name).exists():
                entries.append(json.loads(entry))
            else:
                log.warning("tiktok queue: file for %s is gone; dropping", clip_id)
                conn.execute("UPDATE tiktok_queue SET state='expired' WHERE clip_id=?",
                             (clip_id,))
        keep = {e["file"] for e in entries} | {"queue.json", "README.md"}
        for p in d.iterdir():
            if p.name not in keep:
                p.unlink()
        (d / "queue.json").write_text(json.dumps(entries, indent=2) + "\n")
        (d / "README.md").write_text(
            "Written by the robot every run. TikToks waiting for Claude to post "
            "from Chrome. Don't edit: the next run replaces this branch.\n")
        gitdir = _git("rev-parse", "--absolute-git-dir").stdout.strip()
        env = {**os.environ, "GIT_DIR": gitdir,
               "GIT_INDEX_FILE": str(pathlib.Path(td) / "index"), "GIT_WORK_TREE": str(d)}
        subprocess.run(["git", "add", "-A", "."], env=env, cwd=d, check=True,
                       capture_output=True)
        tree = subprocess.run(["git", "write-tree"], env=env, cwd=d, check=True,
                              capture_output=True, text=True).stdout.strip()
        if have_remote:
            old = _git("rev-parse", f"origin/{BRANCH}^{{tree}}").stdout.strip()
            if old == tree:
                conn.commit()
                return
        commit = subprocess.run(
            ["git", "-C", str(ROOT), "-c", "user.name=clip-factory",
             "-c", "user.email=actions@users.noreply.github.com",
             "commit-tree", tree, "-m", f"tiktok queue: {len(entries)} waiting [skip ci]"],
            check=True, capture_output=True, text=True).stdout.strip()
        r = _git("push", "-q", "-f", "origin", f"{commit}:refs/heads/{BRANCH}")
        if r.returncode:
            log.warning("tiktok queue push failed: %s", r.stderr.strip()[:300])
        else:
            log.info("tiktok queue: %d waiting", len(entries))
    conn.commit()
