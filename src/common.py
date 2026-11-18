"""Shared config, state, and logging for the clip factory."""
import json
import logging
import os
import pathlib
import sqlite3
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
STATE_DB = ROOT / "state" / "factory.db"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("factory")


def cfg(name="settings"):
    with open(ROOT / "config" / f"{name}.yml") as fh:
        return yaml.safe_load(fh)


def sources(enabled_only=True):
    data = cfg("sources")
    out = []
    for s in data["sources"]:
        merged = {**data.get("defaults", {}), **s}
        if enabled_only and not merged.get("enabled"):
            continue
        out.append(merged)
    return out


PUBLISHABLE = {"campaign", "granted", "published", "own"}


def is_publishable(source):
    """The single gate that keeps this operation alive."""
    if not cfg()["safety"]["require_permission"]:
        return True
    return source.get("permission") in PUBLISHABLE


def db():
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE_DB)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS clips (
            clip_id      TEXT PRIMARY KEY,
            source_id    TEXT,
            streamer     TEXT,
            platform     TEXT,
            title        TEXT,
            url          TEXT,
            views        INTEGER,
            duration     REAL,
            created_at   TEXT,
            score        REAL,
            phash        TEXT,
            state        TEXT DEFAULT 'discovered',
            render_path  TEXT,
            note         TEXT,
            first_seen   TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS posts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_id     TEXT,
            target      TEXT,
            remote_id   TEXT,
            url         TEXT,
            posted_at   TEXT DEFAULT CURRENT_TIMESTAMP,
            status      TEXT
        );
        CREATE TABLE IF NOT EXISTS outreach (
            streamer    TEXT PRIMARY KEY,
            platform    TEXT,
            contacted_at TEXT,
            channel     TEXT,
            status      TEXT DEFAULT 'queued',
            note        TEXT
        );
        CREATE TABLE IF NOT EXISTS vods (
            vod_id        TEXT PRIMARY KEY,
            source_id     TEXT,
            streamer      TEXT,
            platform      TEXT,
            url           TEXT,
            title         TEXT,
            lane          TEXT,
            duration      REAL DEFAULT 0,
            processed_to  REAL DEFAULT 0,   -- seconds of VOD already scanned
            is_live       INTEGER DEFAULT 0,
            max_clips     INTEGER,
            clips_made    INTEGER DEFAULT 0,
            state         TEXT DEFAULT 'new',
            first_seen    TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS decisions (
            clip_id      TEXT PRIMARY KEY,
            decision     TEXT,               -- approved | rejected | expired | auto_*
            decided_at   TEXT DEFAULT CURRENT_TIMESTAMP,
            source_kind  TEXT,               -- twitch_clip | vod_chat | vod_audio | local
            score        REAL,
            chat_ratio   REAL,
            audio_lift   REAL,
            duration     REAL,
            streamer     TEXT,
            hook         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_clips_state ON clips(state);
        CREATE INDEX IF NOT EXISTS idx_clips_phash ON clips(phash);
        """
    )
    # Columns added after v1 — add them to existing databases in place.
    for col, decl in [("lane", "TEXT DEFAULT 'publish'"),
                      ("source_kind", "TEXT DEFAULT 'twitch_clip'"),
                      ("chat_ratio", "REAL"), ("audio_lift", "REAL"),
                      ("vod_id", "TEXT"), ("vod_start", "REAL"),
                      ("chat_top", "TEXT"), ("preview_url", "TEXT"),
                      ("review_issue", "INTEGER"), ("queued_at", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE clips ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass  # already there
    try:
        conn.execute("ALTER TABLE vods ADD COLUMN created_at TEXT")
    except sqlite3.OperationalError:
        pass
    try:   # 1 = live AND in the GTA VI category right now, 0 = live in another game
        conn.execute("ALTER TABLE vods ADD COLUMN in_game INTEGER")
    except sqlite3.OperationalError:
        pass
    # Twitch clips used to be stored as "twitch:<login>". Everything that
    # matches posts to campaigns (milestones, the Content Rewards submit task)
    # looks them up by the config id, so rewrite old rows to it.
    try:
        for s in sources(enabled_only=False):
            login = (s.get("twitch_login") or "").lower()
            if login:
                conn.execute("UPDATE clips SET source_id=? WHERE lower(source_id)=?",
                             (s["id"], "twitch:" + login))
    except Exception:  # noqa: BLE001 — never block startup on a cosmetic fix
        pass
    conn.commit()
    return conn


def footage_ok(source, created_iso):
    """Campaign briefs limit WHICH footage counts (e.g. 'marathon only').
    created_iso: when the footage was recorded/published (ISO 8601)."""
    import datetime as _dt
    after, before = source.get("footage_after"), source.get("footage_before")
    if not (after or before):
        return True
    try:
        t = _dt.datetime.fromisoformat(str(created_iso).replace("Z", "+00:00"))
    except ValueError:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    if after and t < _dt.datetime.fromisoformat(str(after)):
        return False
    if before and t > _dt.datetime.fromisoformat(str(before)):
        return False
    return True


def lane_for(source):
    """publish = may go out; scout = proof-of-concept for outreach only."""
    return "publish" if is_publishable(source) else "scout"


def source_by_id(sid):
    for s in sources(enabled_only=False):
        if s["id"] == sid:
            return s
    if sid == "manual_scout":
        return {"id": "manual_scout", "streamer": "manual", "permission": "none",
                "platform": "manual"}
    return None


def posts_today(conn, target):
    row = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE target=? AND status IN ('ok','queued') "
        "AND date(posted_at)=date('now')",
        (target,),
    ).fetchone()
    return row[0] if row else 0


def env(key, required=True, default=None):
    v = os.environ.get(key, default)
    if required and not v:
        raise SystemExit(f"Missing required environment variable: {key}")
    return v


def ensure_dirs():
    for p in ["raw", "render", "out", "raw/own"]:
        (WORK / p).mkdir(parents=True, exist_ok=True)
    (ROOT / "state").mkdir(exist_ok=True)
